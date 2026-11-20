"""Taste steering: the edition OWNER's reactions shape that owner's next editions.

Only reactions whose by_name is the owner's name (case-insensitive) or NULL
(legacy rows) count. Everyone else's reactions are stored (schema v5 by_name)
but never steer this reader — they will steer their own editions later.

Signals (docs/EDITION.md):
- story-level: more +2.0, less −2.5, hide → excluded entirely. Implicit
  'open' (a link in the article was opened) carries weight +0.6 into the
  similarity transfer only — reason 'Abriste algo parecido (+x)' — and never
  resurfaces the opened story itself. Legacy kinds
  from old rows are still accepted: up +2.0, down −3.0 (the served bar now
  only sends more/less, but history keeps steering).
- similarity transfer: candidate centroid vs each reacted story's centroid
  (embeddings model from reader [embed] config); each reacted story contributes
  weight * max(0, sim − 0.6) / 0.4, total capped at ±2.0
- author affinity: per author, net = (#up+#more) − (#down+#less) over reacted
  stories carrying that author; negative → −0.5 per net down (cap −1.5),
  positive → +0.3 per net up (cap +1.0)

Every non-zero adjustment produces one short Spanish reason (score_reasons).
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

import numpy as np

from .. import i18n
from ..providers.embed import model_name

KIND_WEIGHTS = {"up": 2.0, "more": 2.0, "down": -3.0, "less": -2.5, "open": 0.6,
                "agent": 2.5, "save": 3.0}
KIND_EMOJI = {"up": "👍", "down": "👎"}  # other kinds: taste.kind.<kind> in the catalog


def kind_label(lang: str, kind: str) -> str:
    return KIND_EMOJI.get(kind) or i18n.t(lang, f"taste.kind.{kind}")
SIM_FLOOR, SIM_SPAN, SIM_CAP = 0.6, 0.4, 2.0
AUTHOR_NEG_STEP, AUTHOR_NEG_CAP = 0.5, 1.5
AUTHOR_POS_STEP, AUTHOR_POS_CAP = 0.3, 1.0


STORY_FULL, STORY_ZERO = 2.0, 7.0    # direct boosts fade inside a week
TASTE_FULL, TASTE_ZERO = 7.0, 28.0   # topic/author affinity lasts a month


def _freshness(at: int, now: float, full_days: float, zero_days: float) -> float:
    """1.0 while the reaction is fresh, linear to 0 at zero_days. A reaction is
    taste, not a standing order: 'more like this' steers hard for days, then the
    story has to earn its front-page spot on its own news value again."""
    age = max(0.0, (now - at) / 86400.0)
    if age <= full_days:
        return 1.0
    return max(0.0, (zero_days - age) / (zero_days - full_days))


@dataclass
class Adjustment:
    hidden: set[str] = field(default_factory=set)
    delta: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def add(self, story_id: str, amount: float, reason: str) -> None:
        self.delta[story_id] = self.delta.get(story_id, 0.0) + amount
        self.reasons.setdefault(story_id, []).append(reason)


def _owner_reactions(conn: sqlite3.Connection, reader_id: str, owner_name: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT story_id, kind, at FROM reactions "
        "WHERE reader_id = ? AND story_id IS NOT NULL "
        "AND (by_name IS NULL OR lower(by_name) = lower(?)) "
        # handing a story to your agent (schema v8) is the strongest "this matters"
        "UNION ALL SELECT story_id, 'agent' AS kind, at FROM agent_sends "
        "WHERE reader_id = ? AND lower(by_name) = lower(?) "
        # the owner saving a story (schema v9, local) is the strongest positive
        "UNION ALL SELECT story_id, 'save' AS kind, at FROM saves "
        "WHERE reader_id = ? AND story_id IS NOT NULL AND removed_at IS NULL "
        "AND origin = 'gaceta' AND lower(by_name) = lower(?)",
        (reader_id, owner_name, reader_id, owner_name, reader_id, owner_name),
    ).fetchall()


def _story_authors(conn: sqlite3.Connection, story_ids) -> dict[str, set[str]]:
    ids = list(story_ids)
    out: dict[str, set[str]] = {}
    if not ids:
        return out
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = conn.execute(
            "SELECT si.story_id, i.author FROM story_items si "
            "JOIN items i ON i.id = si.item_id "
            f"WHERE si.story_id IN ({','.join('?' * len(chunk))}) AND i.author IS NOT NULL",
            chunk,
        )
        for r in rows:
            a = str(r["author"]).strip()
            if a:
                out.setdefault(r["story_id"], set()).add(a)
    return out


def _centroids(conn: sqlite3.Connection, model: str, story_ids) -> dict[str, np.ndarray]:
    """Mean of member-item embeddings per story (cosine space)."""
    ids = list(story_ids)
    vecs: dict[str, list[np.ndarray]] = {}
    if not ids:
        return {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = conn.execute(
            "SELECT si.story_id, e.vec FROM story_items si "
            "JOIN embeddings e ON e.item_id = si.item_id AND e.model = ? "
            f"WHERE si.story_id IN ({','.join('?' * len(chunk))})",
            [model, *chunk],
        )
        for r in rows:
            v = np.frombuffer(r["vec"], dtype=np.float32)
            if v.size:
                vecs.setdefault(r["story_id"], []).append(v)
    out: dict[str, np.ndarray] = {}
    for sid, vs in vecs.items():
        c = np.mean(np.stack(vs), axis=0)
        n = float(np.linalg.norm(c))
        out[sid] = c / n if n > 0 else c
    return out


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a)) * float(np.linalg.norm(b))
    return float(np.dot(a, b)) / (denom + 1e-12)


def adjustments(conn: sqlite3.Connection, reader_id: str, cfg: dict,
                items_by_story: dict[str, list] | None = None,
                now: float | None = None) -> Adjustment:
    """Owner-reaction adjustments for the reader's candidate stories.

    items_by_story maps story_id → member item rows (as built in edition.build);
    when omitted, similarity/author signals are skipped (story-level still applies).
    Reactions decay with age (_freshness): direct boosts last days, affinity weeks.
    """
    adj = Adjustment()
    lang = i18n.reader_lang(cfg)
    owner_name = str(cfg.get("name") or reader_id)
    ts = time.time() if now is None else float(now)
    reactions = _owner_reactions(conn, reader_id, owner_name)
    if not reactions:
        return adj

    for r in reactions:
        if r["kind"] == "hide":
            adj.hidden.add(r["story_id"])

    # --- story level (direct boosts decay fastest: STORY_FULL → STORY_ZERO days)
    # per_story: sid -> kind -> [direct_weight, taste_weight, count]
    per_story: dict[str, dict[str, list[float]]] = {}
    for r in reactions:
        w = KIND_WEIGHTS.get(r["kind"])
        if w is None:
            continue
        direct = 0.0 if r["kind"] == "open" else w * _freshness(r["at"], ts, STORY_FULL, STORY_ZERO)
        taste = w * _freshness(r["at"], ts, TASTE_FULL, TASTE_ZERO)
        kinds = per_story.setdefault(r["story_id"], {})
        if r["kind"] in kinds:
            kinds[r["kind"]][0] += direct
            kinds[r["kind"]][1] += taste
            kinds[r["kind"]][2] += 1
        else:
            kinds[r["kind"]] = [direct, taste, 1.0]
    for sid, kinds in per_story.items():
        for kind, (direct, _taste, n) in sorted(kinds.items(), key=lambda kv: -abs(kv[1][0])):
            if kind == "open":
                continue  # 'open' steers similarity only, never the story itself
            if abs(direct) < 0.05:
                continue  # fully faded reaction: no boost, no stale reason line
            word = i18n.t(lang, "taste.times", 2 if n > 1.5 else 1)
            adj.add(sid, direct, i18n.t(lang, "taste.gave", label=kind_label(lang, kind),
                                       count=f"{n:.0f}", times=word, v=f"{direct:+.1f}"))

    # --- author affinity (over reacted stories' authors; applied to candidates)
    authors_by_story = _story_authors(conn, [r["story_id"] for r in reactions])
    author_net: dict[str, float] = {}
    for r in reactions:
        w = KIND_WEIGHTS.get(r["kind"])
        if w is None or r["kind"] == "open":
            continue
        for a in authors_by_story.get(r["story_id"], ()):  # reactions can repeat across days
            author_net[a] = author_net.get(a, 0.0) + (1.0 if w > 0 else -1.0)
    author_delta: dict[str, tuple[float, str]] = {}
    for a, net in author_net.items():
        if net < 0:
            d = max(AUTHOR_NEG_STEP * net, -AUTHOR_NEG_CAP)  # net negative → −0.5 each, cap −1.5
            author_delta[a] = (d, i18n.t(lang, "taste.author_neg", v=f"{d:+.1f}"))
        elif net > 0:
            d = min(AUTHOR_POS_STEP * net, AUTHOR_POS_CAP)
            author_delta[a] = (d, i18n.t(lang, "taste.author_pos", v=f"{d:+.1f}"))
    if author_delta and items_by_story:
        cand_authors = _story_authors(conn, list(items_by_story.keys()))
        for sid, authors in cand_authors.items():
            seen: set[str] = set()
            for a in authors:
                if a in author_delta and a not in seen:
                    seen.add(a)
                    d, reason = author_delta[a]
                    adj.add(sid, d, reason)

    # --- similarity transfer
    if items_by_story:
        try:
            model = model_name(cfg)
        except ValueError:
            model = ""
        if model:
            reacted_ids = [sid for sid in per_story if sid not in adj.hidden]
            candidates = [sid for sid in items_by_story if sid not in per_story]
            centroids = _centroids(conn, model, set(reacted_ids) | set(candidates))
            for cand in candidates:
                c = centroids.get(cand)
                if c is None:
                    continue
                transfer = 0.0
                open_part = 0.0
                for rsid in reacted_ids:
                    rc = centroids.get(rsid)
                    if rc is None:
                        continue
                    sim = _cos(c, rc)
                    if sim > SIM_FLOOR:
                        transfer += sum(v[1] for v in per_story[rsid].values()) * (sim - SIM_FLOOR) / SIM_SPAN
                        ow = per_story[rsid].get("open", [0.0, 0.0, 0.0])[1]
                        if ow > 0:
                            open_part += ow * (sim - SIM_FLOOR) / SIM_SPAN
                transfer = max(-SIM_CAP, min(SIM_CAP, transfer))
                if abs(transfer) >= 0.05:
                    if open_part >= 0.05 and abs(transfer - open_part) < 0.05:
                        label = i18n.t(lang, "taste.sim_open")
                    elif transfer > 0:
                        label = i18n.t(lang, "taste.sim_pos")
                    else:
                        label = i18n.t(lang, "taste.sim_neg")
                    adj.add(cand, transfer, f"{label} ({transfer:+.1f})")
    return adj
