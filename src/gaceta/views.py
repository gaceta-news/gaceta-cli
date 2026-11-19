"""Read views: row sets safe to show at a max privacy level.

These never select annotation notes — notes are about the reader and are always private,
even when the item itself is labelled public. Verdict and library_path are reader
signals too (what he cared about / pointer to his private note), so they are excluded.
"""
from __future__ import annotations

import sqlite3

from . import privacy


def public_items(conn: sqlite3.Connection, reader_id: str, max_level: str = "public") -> list[sqlite3.Row]:
    """Items (with their per-reader annotation labels) at or below max_level. No note text."""
    levels = [l for l in privacy.LEVELS if privacy.allowed(l, max_level)]
    placeholders = ",".join("?" * len(levels))
    return conn.execute(
        f"""
        SELECT i.id, i.source_id, i.author, i.title, i.url, i.published, i.body,
               a.item_privacy
        FROM annotations a
        JOIN items i ON i.id = a.item_id
        WHERE a.reader_id = ? AND a.item_privacy IN ({placeholders})
        ORDER BY i.published
        """,
        (reader_id, *levels),
    ).fetchall()


def public_stories(conn: sqlite3.Connection, reader_id: str, max_level: str = "public") -> list[sqlite3.Row]:
    """Stories at or below max_level: id, title, summary, section only. No item text,
    no why_it_matters (reader-specific), no notes."""
    levels = [l for l in privacy.LEVELS if privacy.allowed(l, max_level)]
    placeholders = ",".join("?" * len(levels))
    return conn.execute(
        f"""
        SELECT s.id, s.title, s.summary, s.section
        FROM stories s
        WHERE s.reader_id = ? AND s.privacy IN ({placeholders})
        ORDER BY s.last_seen
        """,
        (reader_id, *levels),
    ).fetchall()
