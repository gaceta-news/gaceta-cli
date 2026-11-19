"""Ingest: pull RawItems from an adapter into gaceta's store, with privacy labels.

Shared content (sources, items) is reader-agnostic; annotations are per reader.
Idempotent: a second run over the same data creates no duplicates and reports 0 new.
"""
from __future__ import annotations

import sqlite3
import time

from . import privacy
from .sources.base import RawItem


def _upsert_reader(conn, cfg: dict, now: int) -> None:
    conn.execute(
        "INSERT INTO readers (id, name, langs, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name, langs = excluded.langs",
        (cfg["id"], cfg.get("name", cfg["id"]), cfg.get("langs", "en"), now),
    )


def _upsert_source(conn, item: RawItem) -> None:
    s = item.source
    conn.execute(
        "INSERT INTO sources (id, kind, name, url, content_public) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, name = excluded.name, "
        "url = excluded.url, content_public = excluded.content_public",
        (s.id, s.kind, s.name, s.url, 1 if s.content_public else 0),
    )


def _upsert_item(conn: sqlite3.Connection, item: RawItem, now: int, origin: str = "adapter") -> str:
    """Insert or update the shared item row. Returns 'new', 'updated' or 'same'.

    Invariant: unproven evidence never overwrites proven-public shared content.
    Item ids are shared across readers while provenance varies per fetch (the same
    video, anonymous or cookies-fetched transcript); without this guard a
    cookie-fetched body would silently replace the text another reader's 'public'
    annotation points at. Upgrading unproven → public is allowed.

    Second invariant (nostr_wot, roadmap 11a): an item another adapter already
    ingested is never overwritten by the WoT pull — a note the reader's follows
    produced keeps the first adapter's metadata (body, url, library link). The
    WoT run only enriches author_pubkey when missing and counts the item as
    present."""
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item.id,)).fetchone()
    new_vals = {
        "source_id": item.source.id, "author": item.author, "title": item.title,
        "url": item.url, "published": item.published, "body": item.body,
    }
    if row is None:
        conn.execute(
            "INSERT INTO items (id, source_id, author, title, url, published, body, ingested_at, origin, author_pubkey) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item.id, *new_vals.values(), now, origin, item.author_pubkey),
        )
        return "new"
    if origin == "nostr_wot" and row["origin"] != "nostr_wot":
        if item.author_pubkey and not row["author_pubkey"]:
            conn.execute("UPDATE items SET author_pubkey = ? WHERE id = ?",
                         (item.author_pubkey, item.id))
        return "same"
    diff = any(row[f] != v for f, v in new_vals.items()) or (
        item.author_pubkey is not None and row["author_pubkey"] != item.author_pubkey
    )
    if diff:
        old_public = conn.execute(
            "SELECT content_public FROM sources WHERE id = ?", (row["source_id"],)
        ).fetchone()
        if old_public is not None and bool(old_public[0]) and not item.source.content_public:
            return "same"
        conn.execute(
            "UPDATE items SET source_id = ?, author = ?, title = ?, url = ?, published = ?, body = ?, "
            "author_pubkey = COALESCE(?, author_pubkey) WHERE id = ?",
            (*new_vals.values(), item.author_pubkey, item.id),
        )
        return "updated"
    return "same"


def _upsert_annotation(conn, reader_id: str, item: RawItem, label: str, reason: str, now: int) -> None:
    row = conn.execute(
        "SELECT verdict, note, library_path, item_privacy, privacy_reason FROM annotations "
        "WHERE reader_id = ? AND item_id = ?",
        (reader_id, item.id),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO annotations (reader_id, item_id, verdict, note, library_path, item_privacy, privacy_reason, annotated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (reader_id, item.id, item.verdict, item.note, item.library_path, label, reason, now),
        )
        return
    # An adapter with no reader-side layer (nostr_wot) never erases what another
    # adapter recorded: the verdict, note and library pointer of a note already
    # read through the follows stay; only the privacy label may move.
    if item.verdict is None and item.note is None and item.library_path is None:
        verdict, note, library = row["verdict"], row["note"], row["library_path"]
    else:
        verdict, note, library = item.verdict, item.note, item.library_path
    if (verdict, note, library) != (row["verdict"], row["note"], row["library_path"]) or (
        label, reason
    ) != (row["item_privacy"], row["privacy_reason"]):
        conn.execute(
            "UPDATE annotations SET verdict = ?, note = ?, library_path = ?, item_privacy = ?, privacy_reason = ?, annotated_at = ? "
            "WHERE reader_id = ? AND item_id = ?",
            (verdict, note, library, label, reason, now, reader_id, item.id),
        )


def _record_told(conn, reader_id: str, item_id: str, now: int) -> None:
    seen = conn.execute(
        "SELECT 1 FROM told WHERE reader_id = ? AND item_id = ? AND story_id IS NULL",
        (reader_id, item_id),
    ).fetchone()
    if seen is None:
        conn.execute(
            "INSERT INTO told (reader_id, item_id, story_id, channel, at) VALUES (?, ?, NULL, NULL, ?)",
            (reader_id, item_id, now),
        )


def ingest(conn: sqlite3.Connection, adapter, reader_cfg: dict) -> dict:
    """Run one ingest pass. Returns {'new': n, 'updated': n, 'told': n}."""
    now = int(time.time())
    reader_id = reader_cfg["id"]
    cur = conn.execute(
        "INSERT INTO ingest_runs (adapter, reader_id, started_at) VALUES (?, ?, ?)",
        (adapter.name, reader_id, now),
    )
    run_id = cur.lastrowid

    _upsert_reader(conn, reader_cfg, now)

    n_new = n_updated = 0
    for item in adapter.items():
        _upsert_source(conn, item)
        outcome = _upsert_item(conn, item, now, origin=adapter.name)
        if outcome == "new":
            n_new += 1
        elif outcome == "updated":
            n_updated += 1
        label, reason = privacy.label_item(
            reader_cfg, item.source.content_public, item.title, item.body, item.url, item.author,
        )
        _upsert_annotation(conn, reader_id, item, label, reason, now)
        if item.verdict == "told":
            _record_told(conn, reader_id, item.id, now)

    finished = int(time.time())
    n_told = conn.execute("SELECT COUNT(*) FROM told WHERE reader_id = ?", (reader_id,)).fetchone()[0]
    conn.execute(
        "UPDATE ingest_runs SET finished_at = ?, items_new = ?, items_updated = ? WHERE id = ?",
        (finished, n_new, n_updated, run_id),
    )
    conn.commit()
    return {"new": n_new, "updated": n_updated, "told": n_told}
