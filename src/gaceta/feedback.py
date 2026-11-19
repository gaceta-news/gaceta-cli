"""Reader verdicts → feedback file for an upstream collector (roadmap 6).

The paper's verdict bar (Más así / Menos así / Visto, DECISIONS 12) is the reader's
clearest taste signal. Collectors that read on the reader's behalf
should learn from it too. gaceta never writes into a collector's files (DECISIONS 6/11):
it writes a self-contained report under the reader's own data dir, and the collector
reads it. Deterministic, no LLM.

Only the edition OWNER's verdicts count (by_name = owner or NULL), same rule as taste.py.
Verdict per story = the owner's current more/less reaction (reactions toggle, so a row
that exists is the current state); otherwise "visto" if the owner marked it seen.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

VERDICT_KINDS = {"more": "mas", "up": "mas", "less": "menos", "down": "menos"}
LABELS = {"mas": "Más así", "menos": "Menos así", "visto": "Visto"}
MAX_ITEMS_PER_STORY = 6


def default_out(cfg: dict) -> Path:
    """Configured path, else <reader_dir>/feedback-export.md."""
    fb = cfg.get("feedback") or {}
    if fb.get("out"):
        return Path(fb["out"]).expanduser()
    return Path(cfg["_reader_dir"]) / "feedback-export.md"


def _verdicts(conn: sqlite3.Connection, reader_id: str, owner: str, since: int) -> dict[str, dict]:
    """story_id → {verdict, at}. A more/less reaction wins over a bare seen mark."""
    out: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT story_id, by_name, at FROM seen WHERE reader_id = ? AND lower(by_name) = lower(?) AND at >= ?",
        (reader_id, owner, since),
    ):
        out[r["story_id"]] = {"verdict": "visto", "at": r["at"]}
    for r in conn.execute(
        "SELECT story_id, kind, at FROM reactions WHERE reader_id = ? AND story_id IS NOT NULL "
        "AND (by_name IS NULL OR lower(by_name) = lower(?)) AND at >= ? ORDER BY at",
        (reader_id, owner, since),
    ):
        v = VERDICT_KINDS.get(r["kind"])
        if v:  # later rows override earlier ones
            out[r["story_id"]] = {"verdict": v, "at": r["at"]}
    return out


def build(conn: sqlite3.Connection, reader_id: str, cfg: dict, days: int = 30,
          now: int | None = None) -> dict:
    now = int(now if now is not None else time.time())
    owner = str(cfg.get("name") or reader_id)
    verdicts = _verdicts(conn, reader_id, owner, now - days * 86400)

    stories: list[dict] = []
    tally: dict[str, dict] = {}
    for sid, v in verdicts.items():
        st = conn.execute("SELECT title FROM stories WHERE id = ? AND reader_id = ?",
                          (sid, reader_id)).fetchone()
        if st is None:
            continue
        rows = conn.execute(
            "SELECT i.id, i.title, s.id AS source_id, COALESCE(s.name, s.id, i.author) AS source, "
            "s.kind AS kind, (SELECT COUNT(*) FROM told t WHERE t.reader_id = ? AND t.item_id = i.id) AS told "
            "FROM story_items si JOIN items i ON i.id = si.item_id "
            "LEFT JOIN sources s ON s.id = i.source_id "
            "WHERE si.story_id = ? ORDER BY i.published DESC",
            (reader_id, sid),
        ).fetchall()
        sources = sorted({(r["source"] or "?", r["kind"] or "other") for r in rows})
        told = [r["id"] for r in rows if r["told"]]
        stories.append({
            "story_id": sid, "title": st["title"], "verdict": v["verdict"], "at": v["at"],
            "sources": [s for s, _ in sources],
            "items": [r["id"] for r in rows[:MAX_ITEMS_PER_STORY]],
            "n_items": len(rows), "told_items": told,
        })
        solo = len(sources) == 1
        for name, kind in sources:
            t = tally.setdefault(name, {"source": name, "kind": kind, "mas": 0, "menos": 0,
                                        "visto": 0, "solo": 0})
            t[v["verdict"]] += 1
            if solo:
                t["solo"] += 1

    stories.sort(key=lambda s: -s["at"])
    sources = sorted(tally.values(),
                     key=lambda t: (-(t["mas"] - t["menos"]), -t["mas"], t["menos"], t["source"].lower()))
    totals = {k: sum(1 for s in stories if s["verdict"] == k) for k in LABELS}
    ping_check = [s for s in stories if s["told_items"] and s["verdict"] == "menos"]
    return {"version": 1, "reader": reader_id, "owner": owner, "generated_at": now, "days": days,
            "totals": totals, "sources": sources, "stories": stories,
            "pinged_but_menos": [s["story_id"] for s in ping_check]}


def _ts(t: int) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


def render_md(rep: dict) -> str:
    o = rep["owner"]
    t = rep["totals"]
    lines = [
        f"# gaceta feedback export: {o}'s verdicts",
        "",
        f"Generated {_ts(rep['generated_at'])} by `gaceta feedback` (do not edit; rewritten every run). "
        f"Window: last {rep['days']} days. Only {o}'s own taps.",
        "",
        f"How to read: these are {o}'s taps on stories in their paper — same weight as "
        "the paper's verdict bar. **Más así** = wants more like this; **Menos así** = less; **Visto** = read it, "
        "neutral. A story can merge several sources, so per-source counts are hints; the `solo` column "
        "counts stories with that source alone (clearest attribution). Use them for the interest model, "
        "channel calibration, gem digging and discovery — never mute or follow on a single tap.",
        "",
        f"Totals: más {t['mas']} · menos {t['menos']} · visto {t['visto']}",
        "",
    ]
    if rep["pinged_but_menos"]:
        lines += ["## Pings they marked Menos así", ""]
        for s in rep["stories"]:
            if s["story_id"] in rep["pinged_but_menos"]:
                lines.append(f"- {s['title']} — told items: {', '.join(s['told_items'])}")
        lines.append("")
    lines += ["## Sources", "", "| source | kind | más | menos | visto | solo |", "|---|---|---|---|---|---|"]
    for s in rep["sources"]:
        name = s["source"].replace("|", "\\|")  # channel names can contain pipes
        lines.append(f"| {name} | {s['kind']} | {s['mas']} | {s['menos']} | {s['visto']} | {s['solo']} |")
    lines += ["", "## Verdicts (newest first)", ""]
    for s in rep["stories"]:
        more = f" (+{s['n_items'] - len(s['items'])} more)" if s["n_items"] > len(s["items"]) else ""
        told = f" · told items: {', '.join(s['told_items'])}" if s["told_items"] else ""
        lines.append(f"- {_ts(s['at'])} · **{LABELS[s['verdict']]}** · {s['title']} · "
                     f"sources: {', '.join(s['sources'])} · items: {', '.join(s['items'])}{more}{told}")
    if not rep["stories"]:
        lines.append("(no verdicts yet)")
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)  # verdicts are about the reader → private
    os.replace(tmp, path)


def export(conn: sqlite3.Connection, reader_id: str, cfg: dict, out: Path | None = None,
           days: int = 30) -> tuple[Path, dict]:
    """Write <out>.md and a sibling .json. Returns (md path, report)."""
    out = Path(out) if out else default_out(cfg)
    rep = build(conn, reader_id, cfg, days=days)
    _atomic_write(out, render_md(rep))
    _atomic_write(out.with_suffix(".json"), json.dumps(rep, ensure_ascii=False, indent=1))
    return out, rep
