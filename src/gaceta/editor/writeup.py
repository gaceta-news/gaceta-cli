"""LLM story write-ups: incremental, cached, budgeted (ARCHITECTURE § Editor).

For each story with last_seen inside the window that has no story_versions row
or gained members since its last version (member count tracked in
story_versions.members, schema v4), ask the LLM for a strict-JSON story card
and store it as a new version. Decisions are cached in llm_decisions (same key
scheme as Confirmer: sha256(model + messages)), so reruns cost 0 calls until a
story actually changes. Invalid JSON keeps the story unchanged (counted as a
failure). The run stops at the call budget and reports what is left pending.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .. import config, i18n, privacy
from ..editor import prompts, quotes
from ..providers import llm as llm_providers

DEFAULT_DAYS = 14
DEFAULT_MAX_LLM_CALLS = 150


def _reader_profile(cfg: dict, reader_id: str) -> str | None:
    """Reader profile text from config [reader].profile_path (relative to the
    reader dir). Only ever enters a prompt when the model is allow_private."""
    rel = (cfg.get("reader") or {}).get("profile_path")
    if not rel:
        return None
    base = cfg.get("_reader_dir")
    path = Path(base) / str(rel) if base else config.reader_config_path(reader_id).parent / str(rel)
    try:
        return path.read_text()
    except OSError:
        return None


def _on_paper(conn: sqlite3.Connection, reader_id: str) -> set[str]:
    """Story ids in the reader's latest edition snapshot. Those are what the reader
    actually sees, so an unwritten one shows a raw source title (early 'Menos
    así' taps land mostly on such raw cards) → write them first."""
    row = conn.execute(
        "SELECT path FROM editions WHERE reader_id = ? ORDER BY built_at DESC LIMIT 1", (reader_id,)
    ).fetchone()
    if row is None:
        return set()
    try:
        return set(json.loads(Path(row[0]).read_text()).get("stories", {}))
    except (OSError, ValueError):
        return set()


def _candidates(conn: sqlite3.Connection, reader_id: str, start: int, end: int) -> list[dict]:
    """Stories in the window that need a write: no version yet, or the latest
    version's tracked member count differs from the current member count."""
    rows = conn.execute(
        """
        SELECT s.id, s.last_seen,
          (SELECT COUNT(*) FROM story_items si WHERE si.story_id = s.id) AS n_items,
          (SELECT COUNT(*) FROM story_items si JOIN annotations a
             ON a.item_id = si.item_id AND a.reader_id = s.reader_id
             WHERE si.story_id = s.id AND a.verdict = 'told') AS n_told,
          (SELECT v.members FROM story_versions v
             WHERE v.story_id = s.id ORDER BY v.version DESC LIMIT 1) AS last_members
        FROM stories s
        WHERE s.reader_id = ? AND s.last_seen IS NOT NULL
          AND s.last_seen >= ? AND s.last_seen < ?
        """,
        (reader_id, start, end),
    ).fetchall()
    out = []
    for r in rows:
        if r["n_items"] == 0:
            continue
        if r["last_members"] is not None and r["last_members"] == r["n_items"]:
            continue  # unchanged since the last version
        out.append(dict(r))
    # prioritise: on the reader's current paper, then has a told item, then size desc, then recency
    paper = _on_paper(conn, reader_id)
    out.sort(key=lambda s: (s["id"] in paper, s["n_told"] > 0, s["n_items"], s["last_seen"] or 0),
             reverse=True)
    return out


def _story_items(conn: sqlite3.Connection, reader_id: str, story_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT i.title, i.body, i.author, i.published, i.ingested_at,
               s.kind AS source_kind, a.note, a.item_privacy
        FROM story_items si
        JOIN items i ON i.id = si.item_id
        LEFT JOIN sources s ON s.id = i.source_id
        LEFT JOIN annotations a ON a.item_id = i.id AND a.reader_id = ?
        WHERE si.story_id = ?
        ORDER BY i.published IS NULL, i.published, i.id
        """,
        (reader_id, story_id),
    ).fetchall()
    return [dict(r) for r in rows]


def write_stories(conn: sqlite3.Connection, reader_id: str, cfg: dict, *,
                  days: int = DEFAULT_DAYS, max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
                  chat=None, now: int | None = None) -> dict:
    """Write (or rewrite) the story cards of the reader's changed stories."""
    chat = chat or llm_providers.from_config((cfg.get("llm") or {}).get("editor") or {})
    now = int(now if now is not None else time.time())
    end = now
    start = now - int(days) * 86400
    langs = str(cfg.get("langs") or "en")
    profile = _reader_profile(cfg, reader_id)

    candidates = _candidates(conn, reader_id, start, end)
    calls = cache_hits = written = failed = 0
    pending = 0
    tokens = {"prompt": 0, "completion": 0}
    budget_exhausted = False
    served_models: set[str] = set()
    refused = 0
    unquoted = 0
    relanguaged = 0
    want_lang = (i18n.langs_list(langs) or ["en"])[0].lower()

    def ask(messages: list[dict]):
        """One cached completion → (out, usage), or None when the budget is spent."""
        nonlocal calls, cache_hits, refused
        text = prompts.messages_text(messages)
        key = hashlib.sha256((chat.name + text).encode()).hexdigest()
        row = conn.execute("SELECT output FROM llm_decisions WHERE key = ?", (key,)).fetchone()
        if row is not None:
            cache_hits += 1
            return row[0] or "", {}
        if calls >= max_llm_calls:
            return None
        try:
            out, usage = chat.complete(messages, max_tokens=400, temperature=0.0)
        except llm_providers.LLMRefused as e:
            refused += 1
            out, usage = "", {"refused": e.code, "detail": e.detail}
        calls += 1
        tokens["prompt"] += int(usage.get("prompt_tokens") or 0)
        tokens["completion"] += int(usage.get("completion_tokens") or 0)
        if usage.get("served_model"):
            served_models.add(str(usage["served_model"]))
        conn.execute(
            "INSERT INTO llm_decisions (key, model, input_hash, output, usage, at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "output = excluded.output, usage = excluded.usage, at = excluded.at",
            (key, chat.name, hashlib.sha256(text.encode()).hexdigest(),
             out, json.dumps(usage), now),
        )
        return out, usage

    for cand in candidates:
        story_id = cand["id"]
        items = _story_items(conn, reader_id, story_id)
        if not items or (not chat.allow_private and any(
            not privacy.allowed(it["item_privacy"], "public") for it in items
        )):
            continue
        previous = conn.execute(
            "SELECT written_at FROM story_versions WHERE story_id = ? ORDER BY version DESC LIMIT 1",
            (story_id,),
        ).fetchone()
        if previous:
            for item in items:
                item["new_coverage"] = (item["ingested_at"] or 0) > previous["written_at"]
        messages = prompts.writeup_messages(
            items, langs=langs, profile=profile, allow_private=chat.allow_private,
            followup=any(item.get("new_coverage") for item in items),
        )
        got = ask(messages)
        if got is None:
            budget_exhausted = True
            break
        out, usage = got

        card = prompts.parse_writeup(out)
        wrong = card and i18n.mismatch(card["summary"], want_lang)
        if wrong:
            # small models answer in the source's language: ask once more, in budget
            retry = ask(messages + [{"role": "assistant", "content": out},
                                    prompts.language_fix_message(wrong, want_lang)])
            fixed = prompts.parse_writeup(retry[0]) if retry else None
            if fixed and not i18n.mismatch(fixed["summary"], want_lang):
                card, usage = fixed, (retry[1] or usage)
                relanguaged += 1
        if card is None:
            failed += 1  # invalid JSON → story stays unchanged, cached reply kept
            conn.commit()
            continue
        unquoted += quotes.enforce_card(card, items)  # 11b: no unverifiable quotes

        served_model = usage.get("served_model") or chat.name
        ver = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM story_versions WHERE story_id = ?",
            (story_id,),
        ).fetchone()[0]
        n_members = conn.execute(
            "SELECT COUNT(*) FROM story_items WHERE story_id = ?", (story_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO story_versions (story_id, version, title, summary, why_it_matters, "
            "confidence, model, written_at, members) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (story_id, ver, card["title"], card["summary"], card["why_it_matters"],
             card["confidence"], served_model, now, n_members),
        )
        labels = [
            r[0] for r in conn.execute(
                "SELECT a.item_privacy FROM story_items si JOIN annotations a "
                "ON a.item_id = si.item_id AND a.reader_id = ? WHERE si.story_id = ?",
                (reader_id, story_id),
            )
        ]
        conn.execute(
            "UPDATE stories SET title = ?, summary = ?, why_it_matters = ?, confidence = ?, "
            "privacy = ?, updated_at = ? WHERE id = ?",
            (card["title"], card["summary"], card["why_it_matters"], card["confidence"],
             privacy.story_privacy(labels, has_why=bool(card["why_it_matters"])), now, story_id),
        )
        written += 1
        conn.commit()

    pending = len(candidates) - (written + failed)
    return {
        "days": days, "window_stories": len(candidates),
        "written": written, "failed": failed, "pending": pending,
        "llm_calls": calls, "llm_cache_hits": cache_hits,
        "llm_prompt_tokens": tokens["prompt"], "llm_completion_tokens": tokens["completion"],
        "budget_exhausted": budget_exhausted,
        "refused": refused, "quotes_unquoted": unquoted, "relanguaged": relanguaged,
        "model": chat.name, "served_models": sorted(served_models),
    }
