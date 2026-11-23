"""Deterministic incremental story clustering for one reader (ARCHITECTURE § Editor).

Pipeline: embed (source text only, never the annotation note) → cluster in published
order against open-story centroids → write stories/story_items. Re-runnable; with
--rebuild the reader's stories are wiped and rebuilt from the same evidence.

Params come from reader config [cluster] threshold / window_days (no paths in src/).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import numpy as np

from .. import budget as budget_mod
from .. import privacy
from ..editor import prompts
from ..providers import embed as embedders
from ..providers import llm as llm_providers

VERDICTS = ("noted", "told")
DEFAULT_THRESHOLD = 0.70  # sweep 2026-11-17 vs golden-150: best pairwise F1 0.57 (P .81 R .44)
DEFAULT_WINDOW_DAYS = 21
DEFAULT_CONFIRM_LOW = 0.55  # below this an item can never join a story (anti-chaining)
DEFAULT_MAX_LLM_CALLS = 400  # per run; exhausted → embeddings-only fallback
BATCH = 32


# ---------------------------------------------------------------- embed pass

def embed_text(title: str | None, body: str | None) -> str:
    """What gets embedded: title + first 1500 chars of body. SOURCE CONTENT ONLY."""
    return f"{title or ''}\n{(body or '')[:1500]}"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _reader_items(conn, reader_id: str, model: str | None = None) -> list[sqlite3.Row]:
    """Noted/told items for a reader in published order (NULLs last, then id)."""
    sql = (
        "SELECT i.id, i.title, i.body, i.published, a.item_privacy"
        " FROM annotations a JOIN items i ON i.id = a.item_id"
        " WHERE a.reader_id = ? AND a.verdict IN (?, ?)"
    )
    params: list = [reader_id, *VERDICTS]
    if model is not None:
        sql = (
            "SELECT i.id, i.title, i.body, i.published, a.item_privacy, e.vec"
            " FROM annotations a JOIN items i ON i.id = a.item_id"
            " JOIN embeddings e ON e.item_id = i.id AND e.model = ?"
            " WHERE a.reader_id = ? AND a.verdict IN (?, ?)"
        )
        params = [model, reader_id, *VERDICTS]
    sql += " ORDER BY i.published IS NULL, i.published, i.id"
    return conn.execute(sql, params).fetchall()


def embed_items(conn: sqlite3.Connection, reader_id: str, embedder, progress=None) -> dict:
    """Embed noted/told items with `embedder`, skipping unchanged text (incremental).

    Returns {'model', 'embedded', 'skipped'}. progress(done, total) is called every
    100 embeddings.
    """
    model = embedder.name
    rows = _reader_items(conn, reader_id)
    texts = [(r["id"], embed_text(r["title"], r["body"])) for r in rows]
    done = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT item_id, text_hash FROM embeddings WHERE model = ?", (model,)
        )
    }
    todo = [
        (iid, text, h)
        for (iid, text), h in zip(texts, (text_hash(t) for _, t in texts))
        if done.get(iid) != h
    ]
    n_embedded = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        vecs = embedder.embed([text for _, text, _ in chunk])
        for (iid, _, h), vec in zip(chunk, vecs):
            arr = np.asarray(vec, dtype=np.float32)
            conn.execute(
                "INSERT INTO embeddings (item_id, model, dim, vec, text_hash) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(item_id, model) DO UPDATE SET dim = excluded.dim, vec = excluded.vec, "
                "text_hash = excluded.text_hash",
                (iid, model, arr.size, arr.tobytes(), h),
            )
        n_embedded += len(chunk)
        if progress:
            progress(n_embedded, len(todo))
    conn.commit()
    return {"model": model, "embedded": n_embedded, "skipped": len(rows) - n_embedded}


# ---------------------------------------------------------------- clustering core

def _cos(v, vnorm, w) -> float:
    w = np.asarray(w, dtype=np.float32)
    return float(np.dot(v, w)) / (vnorm * float(np.linalg.norm(w)) + 1e-12)


class Confirmer:
    """LLM confirmation of borderline joins, with a decision cache and a hard budget.

    Every decision is cached in llm_decisions by sha256(model + prompt): a rerun
    of the same decision costs 0 calls. When the call budget is exhausted the
    answer is False (embeddings-only behaviour: no borderline join) and
    budget_exhausted is reported. Cache hits stay free even past the budget.
    """

    def __init__(self, model, conn: sqlite3.Connection, desc_fn, *,
                 confirm_low: float, max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
                 budget: budget_mod.DailyBudget | None = None):
        self.model = model
        self.conn = conn
        self.desc_fn = desc_fn
        self.confirm_low = confirm_low
        self.max_llm_calls = max_llm_calls
        self.budget = budget
        self.calls = 0
        self.cache_hits = 0
        self.tokens = {"prompt": 0, "completion": 0}
        self.budget_exhausted = False
        self.refused = 0
        self.served_models: set[str] = set()

    def ask(self, item_id: str, origin_id: str, latest_id: str) -> bool:
        items = [self.desc_fn(i) for i in (item_id, origin_id, latest_id)]
        if not self.model.allow_private and any(
            not privacy.allowed(it.get("item_privacy"), "public") for it in items
        ):
            return False
        messages = prompts.confirm_messages(*items, allow_private=self.model.allow_private)
        text = prompts.messages_text(messages)
        key = hashlib.sha256((self.model.name + text).encode()).hexdigest()
        row = self.conn.execute(
            "SELECT output FROM llm_decisions WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            self.cache_hits += 1
            return prompts.parse_same(row[0] or "")
        if self.calls >= self.max_llm_calls or (
                self.budget is not None and self.budget.reached()):
            self.budget_exhausted = True
            return False
        try:
            out, usage = self.model.complete(messages, max_tokens=300, temperature=0.0)
        except llm_providers.LLMRefused as e:
            self.refused += 1
            out, usage = "", {"refused": e.code, "detail": e.detail}
        self.calls += 1
        if self.budget is not None:
            self.budget.count()  # a real call: counts against [llm] daily_max_calls too
        self.tokens["prompt"] += int(usage.get("prompt_tokens") or 0)
        self.tokens["completion"] += int(usage.get("completion_tokens") or 0)
        if usage.get("served_model"):
            self.served_models.add(str(usage["served_model"]))
        self.conn.execute(
            "INSERT INTO llm_decisions (key, model, input_hash, output, usage, at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "output = excluded.output, usage = excluded.usage, at = excluded.at",
            (key, self.model.name,
             hashlib.sha256(text.encode()).hexdigest(), out, json.dumps(usage), int(time.time())),
        )
        self.conn.commit()
        return prompts.parse_same(out)

    def report(self) -> dict:
        return {"llm_calls": self.calls, "llm_cache_hits": self.cache_hits,
                "llm_prompt_tokens": self.tokens["prompt"],
                "llm_completion_tokens": self.tokens["completion"],
                "budget_exhausted": self.budget_exhausted,
                "refused": self.refused,
                "served_models": sorted(self.served_models)}


def _desc_fn(conn: sqlite3.Connection, reader_id: str):
    """item_id -> prompt-side description (title/body/author/source kind/date/note),
    looked up lazily and memoised per run."""
    cache: dict[str, dict] = {}

    def get(item_id: str) -> dict:
        if item_id not in cache:
            r = conn.execute(
                "SELECT i.title, i.body, i.author, i.published, s.kind AS source_kind, a.note, a.item_privacy "
                "FROM items i LEFT JOIN sources s ON s.id = i.source_id "
                "LEFT JOIN annotations a ON a.item_id = i.id AND a.reader_id = ? "
                "WHERE i.id = ?",
                (reader_id, item_id),
            ).fetchone()
            cache[item_id] = dict(r) if r is not None else {}
        return cache[item_id]

    return get


def _assign(items, threshold: float, window_days: int, stories: list[dict],
            confirmer: Confirmer | None = None) -> list[dict]:
    """Assign items (published order) to open stories or new ones. Mutates `stories`.

    items: (item_id, published, vec, item_privacy). Without a confirmer, a story
    joins when cosine(item, mean-of-member-vecs) >= threshold and story.last_seen
    is within window_days (unchanged phase-2a behaviour).

    With a confirmer (config [cluster] confirm):
    - borderline: best sim in [confirm_low, threshold) → the LLM is asked whether
      the item is the same specific story as the best story's ORIGIN item and its
      most recent member; join only on {"same": true} (unparseable → false);
    - anti-chaining: a join (even above threshold) additionally requires
      cosine(item, story ORIGIN vec) >= confirm_low, so centroid drift cannot
      chain A~B~C when C is far from A.
    """
    for item_id, published, vec, label in items:
        v = np.asarray(vec, dtype=np.float32)
        vnorm = float(np.linalg.norm(v))
        cutoff = None if published is None else published - window_days * 86400
        floor = confirmer.confirm_low if confirmer is not None else threshold
        best_i, best_sim = -1, -1.0
        for i, s in enumerate(stories):
            if cutoff is not None and s["last_seen"] is not None and s["last_seen"] < cutoff:
                continue
            sim = _cos(v, vnorm, s["sum"] / s["n"])
            if sim >= floor and (best_i < 0 or sim > best_sim):
                best_i, best_sim = i, sim
        join = False
        s = stories[best_i] if best_i >= 0 else None
        if s is not None:
            if confirmer is not None and _cos(v, vnorm, s["origin_vec"]) < confirmer.confirm_low:
                join = False  # anti-chaining: too far from where the story started
            elif best_sim >= threshold:
                join = True
            elif confirmer is not None:
                join = confirmer.ask(item_id, s["origin_id"], s["latest_id"])
        if s is None or not join:
            stories.append(
                {"ids": [item_id], "sum": v.copy(), "n": 1, "labels": [label],
                 "first_seen": published, "last_seen": published, "added": [item_id],
                 "story_id": None, "origin_id": item_id, "origin_vec": v.copy(),
                 "latest_id": item_id}
            )
        else:
            s["ids"].append(item_id)
            s["labels"].append(label)
            s["sum"] += v
            s["n"] += 1
            s["latest_id"] = item_id  # items arrive in published order
            if published is not None:
                s["last_seen"] = max(s["last_seen"] or published, published)
                if s["first_seen"] is None:
                    s["first_seen"] = published
            s["added"].append(item_id)
    return stories


def partition(items, threshold: float, window_days: int,
              confirmer: Confirmer | None = None) -> list[list[str]]:
    """Pure in-memory clustering (used by the eval; no story writes). With a
    confirmer, LLM decisions are cached to llm_decisions — nothing else is written."""
    return [s["ids"] for s in _assign(list(items), threshold, window_days, [], confirmer)]


# ---------------------------------------------------------------- DB layer

def _load_stories(conn, reader_id: str, model: str) -> list[dict]:
    stories = []
    for row in conn.execute(
        "SELECT id, first_seen, last_seen FROM stories WHERE reader_id = ? ORDER BY first_seen, id",
        (reader_id,),
    ):
        members = conn.execute(
            "SELECT si.item_id, si.role, i.published, e.vec FROM story_items si "
            "JOIN items i ON i.id = si.item_id "
            "LEFT JOIN embeddings e ON e.item_id = si.item_id AND e.model = ? "
            "WHERE si.story_id = ? "
            "ORDER BY i.published IS NULL, i.published, si.item_id",
            (model, row["id"]),
        ).fetchall()
        if not members:
            continue
        vecs = [np.frombuffer(m["vec"], dtype=np.float32) for m in members if m["vec"]]
        if not vecs:
            continue  # no embeddings for this model → can never match
        origin_m = next((m for m in members if m["role"] == "origin"), members[0])
        latest_m = next((m for m in reversed(members) if m["vec"]), members[0])
        stories.append(
            {"ids": [m["item_id"] for m in members], "sum": np.sum(vecs, axis=0),
             "n": len(vecs), "labels": [m["role"] for m in members],
             "first_seen": row["first_seen"], "last_seen": row["last_seen"],
             "added": [], "story_id": row["id"],
             "origin_id": origin_m["item_id"],
             "origin_vec": np.frombuffer(origin_m["vec"], dtype=np.float32)
             if origin_m["vec"] else vecs[0],
             "latest_id": latest_m["item_id"]}
        )
    return stories


def _next_story_id(conn, reader_id: str) -> str:
    prefix = f"{reader_id}-"
    n = 0
    for (sid,) in conn.execute(
        "SELECT id FROM stories WHERE reader_id = ? AND id LIKE ?", (reader_id, prefix + "%")
    ):
        try:
            n = max(n, int(sid[len(prefix):]) + 1)
        except ValueError:
            continue
    return f"{prefix}{n:04d}"


def cluster(conn: sqlite3.Connection, reader_id: str, cfg: dict, rebuild: bool = False) -> dict:
    """Cluster the reader's noted/told (embedded) items into stories.

    rebuild=True wipes this reader's stories/story_items/story_versions (and any
    corrections hanging off them, else the FK would dangle) and reclusters from
    scratch. Without rebuild, only items not already in a story are assigned.
    """
    c = cfg.get("cluster") or {}
    threshold = float(c.get("threshold", DEFAULT_THRESHOLD))
    window_days = int(c.get("window_days", DEFAULT_WINDOW_DAYS))
    model = embedders.model_name(cfg)

    confirmer = None
    if bool(c.get("confirm", False)):
        chat = llm_providers.from_config((cfg.get("llm") or {}).get("editor") or {})
        confirmer = Confirmer(
            chat, conn, _desc_fn(conn, reader_id),
            confirm_low=float(c.get("confirm_low", DEFAULT_CONFIRM_LOW)),
            max_llm_calls=int(c.get("max_llm_calls", DEFAULT_MAX_LLM_CALLS)),
            budget=budget_mod.DailyBudget(conn, reader_id, cfg),
        )

    if rebuild:
        ids = [r[0] for r in conn.execute("SELECT id FROM stories WHERE reader_id = ?", (reader_id,))]
        for sid in ids:
            conn.execute("DELETE FROM corrections WHERE story_id = ?", (sid,))
            conn.execute("DELETE FROM story_items WHERE story_id = ?", (sid,))
            conn.execute("DELETE FROM story_versions WHERE story_id = ?", (sid,))
        conn.execute("DELETE FROM stories WHERE reader_id = ?", (reader_id,))
        conn.commit()

    stories = _load_stories(conn, reader_id, model)
    already = {iid for s in stories for iid in s["ids"]}
    rows = _reader_items(conn, reader_id, model=model)
    items = [
        (r["id"], r["published"], np.frombuffer(r["vec"], dtype=np.float32), r["item_privacy"])
        for r in rows if r["id"] not in already
    ]
    _assign(items, threshold, window_days, stories, confirmer=confirmer)

    now = int(time.time())
    n_new = n_joined = 0
    for s in stories:
        if not s["added"]:
            continue
        labels = conn.execute(
            f"SELECT item_privacy FROM annotations WHERE reader_id = ? AND item_id IN "
            f"({','.join('?' * len(s['ids']))})", (reader_id, *s["ids"])
        ).fetchall()
        s_privacy = privacy.story_privacy([r[0] for r in labels])
        if s["story_id"] is None:
            sid = _next_story_id(conn, reader_id)
            first = conn.execute(
                "SELECT title, published FROM items WHERE id = ?", (s["ids"][0],)
            ).fetchone()
            conn.execute(
                "INSERT INTO stories (id, reader_id, title, summary, why_it_matters, status, "
                "confidence, section, privacy, first_seen, last_seen, updated_at) "
                "VALUES (?, ?, ?, NULL, NULL, 'developing', NULL, NULL, ?, ?, ?, ?)",
                (sid, reader_id, first["title"], s_privacy, s["first_seen"], s["last_seen"], now),
            )
            s["story_id"] = sid
            n_new += 1
        else:
            sid = s["story_id"]
            conn.execute(
                "UPDATE stories SET last_seen = ?, privacy = ?, updated_at = ? WHERE id = ?",
                (s["last_seen"], s_privacy, now, sid),
            )
            n_joined += 1
        for iid in s["ids"]:
            conn.execute(
                "INSERT INTO story_items (story_id, item_id, role) VALUES (?, ?, ?) "
                "ON CONFLICT(story_id, item_id) DO NOTHING",
                (sid, iid, "origin" if iid == s["ids"][0] else "update"),
            )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM stories WHERE reader_id = ?", (reader_id,)).fetchone()[0]
    result = {"n_items": len(items), "n_new_stories": n_new, "n_joined_stories": n_joined,
              "n_stories": total, "threshold": threshold, "window_days": window_days,
              "model": model, "confirm": confirmer is not None}
    if confirmer is not None:
        result.update(confirmer.report())
    return result
