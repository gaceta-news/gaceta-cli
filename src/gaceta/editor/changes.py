"""What changed between story versions (story_versions.what_changed, schema v11).

A story the reader marked 'Visto' resurfaces with a 'Novedad' badge when it is
rewritten. The badge says *that* it changed; this pass says *what*: for every
version >= 2 whose what_changed is NULL, ask the editor model to compare the
previous card with the current one, with the items that joined in between as
evidence. One short sentence, or '' when nothing material changed (only reworded).

Separate from the write-up call on purpose: it never touches title/summary (a
backfill must not change a story's rev and resurface it again), and it covers
versions written before this column existed. Cached in llm_decisions like the
write-ups; budgeted; same model boundary (no reader profile, no notes; private
items only reach an allow_private model).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

from .. import privacy
from ..editor import prompts, quotes
from ..providers import llm as llm_providers

DEFAULT_DAYS = 14
DEFAULT_MAX_LLM_CALLS = 40
MAX_NEW_ITEMS = 6
MAX_CHARS = 400

_SYSTEM = (
    "You compare two versions of one story card in a personal newspaper. The reader "
    "already read the PREVIOUS card. Tell them what is NEW or DIFFERENT in the CURRENT "
    "card, using the NEW ITEMS as evidence. Reply with JSON only, no markdown fences: "
    '{"what_changed": str or null}. what_changed: one or two short sentences, at most '
    "~40 words. Lead with the new fact or development; say who reports it (outlet or "
    "author) when the item gives it. Only facts that appear in the NEW ITEMS or in the "
    "CURRENT card but not in the PREVIOUS card; never invent numbers, names or dates. "
    "Write about the news itself: never mention cards, summaries, versions or what "
    "'is added' to them. "
    "If the current card only rewords the previous one, return null."
) + prompts.QUOTE_RULE + prompts.UNTRUSTED_RULE


def changes_messages(prev_summary: str, cur_summary: str, new_items: list[dict], *,
                     langs: str) -> list[dict]:
    first = ([l.strip() for l in str(langs or "en").split(",") if l.strip()] or ["en"])[0].lower()
    lang = prompts._LANG_NAMES.get(first, first)
    user = [
        f"Write what_changed in {lang}.",
        "PREVIOUS CARD (what the reader read):\n" + (prev_summary or "").strip(),
        "CURRENT CARD:\n" + (cur_summary or "").strip(),
    ]
    if new_items:
        user.append("NEW ITEMS (joined the story between the two cards, oldest first):\n" + "\n\n".join(
            f"ITEM {n}:\n" + prompts.describe_item(it, include_note=False)
            for n, it in enumerate(new_items, 1)
        ))
    else:
        user.append("NEW ITEMS: none (the story was rewritten from the same items).")
    user.append('Return JSON only: {"what_changed"}.')
    return [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "\n\n".join(user)}]


def parse_changes(text: str) -> str | None:
    """'' = nothing material; str = the change; None = unparseable reply."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "what_changed" not in obj:
        return None
    val = obj["what_changed"]
    if val is None:
        return ""
    if not isinstance(val, str):
        return None
    val = " ".join(val.split())
    if len(val) > MAX_CHARS:
        val = val[:MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return val


def _new_items(conn: sqlite3.Connection, reader_id: str, story_id: str,
               prev: sqlite3.Row, cur: sqlite3.Row) -> list[dict]:
    """Items that joined between prev and cur. story_items has no join time, so
    ingested_at stands in; when that undercounts the member delta (an older item
    clustered late), top up with the most recently ingested earlier members."""
    rows = [dict(r) for r in conn.execute(
        """
        SELECT i.id, i.title, i.body, i.author, i.published, i.ingested_at,
               s.kind AS source_kind, a.item_privacy
        FROM story_items si
        JOIN items i ON i.id = si.item_id
        LEFT JOIN sources s ON s.id = i.source_id
        LEFT JOIN annotations a ON a.item_id = i.id AND a.reader_id = ?
        WHERE si.story_id = ? AND i.ingested_at <= ?
        ORDER BY i.ingested_at DESC, i.id
        """,
        (reader_id, story_id, cur["written_at"]),
    )]
    new = [r for r in rows if r["ingested_at"] > prev["written_at"]]
    need = (cur["members"] or 0) - (prev["members"] or 0)
    for r in rows:
        if len(new) >= need:
            break
        if r not in new:
            new.append(r)
    new = new[:MAX_NEW_ITEMS]
    new.sort(key=lambda r: (r["published"] is None, r["published"] or 0, r["id"]))
    return new


def _candidates(conn: sqlite3.Connection, reader_id: str, start: int) -> list[sqlite3.Row]:
    """Versions >= 2 without what_changed, of stories in the window. Versions a
    reader already resurfaced on (seen before the version was written) go first."""
    return conn.execute(
        """
        SELECT v.story_id, v.version, v.summary, v.written_at, v.members,
          EXISTS (SELECT 1 FROM seen se WHERE se.story_id = v.story_id
                    AND se.at < v.written_at) AS resurfaced
        FROM story_versions v
        JOIN stories s ON s.id = v.story_id
        WHERE s.reader_id = ? AND s.last_seen >= ? AND v.version >= 2
          AND v.what_changed IS NULL
        ORDER BY resurfaced DESC, v.written_at DESC
        """,
        (reader_id, start),
    ).fetchall()


def note_changes(conn: sqlite3.Connection, reader_id: str, cfg: dict, *,
                 days: int = DEFAULT_DAYS, max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
                 chat=None, now: int | None = None) -> dict:
    chat = chat or llm_providers.from_config((cfg.get("llm") or {}).get("editor") or {})
    now = int(now if now is not None else time.time())
    start = now - int(days) * 86400
    langs = str(cfg.get("langs") or "en")
    calls = cache_hits = noted = unchanged = failed = skipped = 0
    budget_exhausted = False

    cands = _candidates(conn, reader_id, start)
    for cur in cands:
        prev = conn.execute(
            "SELECT version, summary, written_at, members FROM story_versions "
            "WHERE story_id = ? AND version < ? ORDER BY version DESC LIMIT 1",
            (cur["story_id"], cur["version"]),
        ).fetchone()
        if prev is None:
            continue
        items = _new_items(conn, reader_id, cur["story_id"], prev, cur)
        if not chat.allow_private and any(
            not privacy.allowed(it["item_privacy"], "public") for it in items
        ):
            skipped += 1
            continue
        messages = changes_messages(prev["summary"], cur["summary"], items, langs=langs)
        text = prompts.messages_text(messages)
        key = hashlib.sha256((chat.name + text).encode()).hexdigest()
        row = conn.execute("SELECT output FROM llm_decisions WHERE key = ?", (key,)).fetchone()
        if row is not None:
            cache_hits += 1
            out = row[0] or ""
        else:
            if calls >= max_llm_calls:
                budget_exhausted = True
                break
            try:
                out, usage = chat.complete(messages, max_tokens=200, temperature=0.0)
            except llm_providers.LLMRefused as e:
                out, usage = "", {"refused": e.code, "detail": e.detail}
            calls += 1
            conn.execute(
                "INSERT INTO llm_decisions (key, model, input_hash, output, usage, at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "output = excluded.output, usage = excluded.usage, at = excluded.at",
                (key, chat.name, hashlib.sha256(text.encode()).hexdigest(),
                 out, json.dumps(usage), now),
            )
        changed = parse_changes(out)
        if changed is None:
            failed += 1
            conn.commit()
            continue
        if changed:
            changed, _ = quotes.enforce(changed, [
                f"{it.get('title') or ''} {it.get('body') or ''}" for it in items
            ] + [cur["summary"] or ""])
            noted += 1
        else:
            unchanged += 1
        conn.execute(
            "UPDATE story_versions SET what_changed = ? WHERE story_id = ? AND version = ?",
            (changed, cur["story_id"], cur["version"]),
        )
        conn.commit()
    return {"candidates": len(cands), "noted": noted, "unchanged": unchanged,
            "failed": failed, "skipped": skipped, "llm_calls": calls,
            "llm_cache_hits": cache_hits, "budget_exhausted": budget_exhausted}
