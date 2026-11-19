"""The sqlite source db must be read-only: byte hashes before/after ingest, and writes must fail."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from gaceta import ingest
from gaceta.sources.sqlite import SqliteAdapter


def tree_hashes(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "items.db-wal" and p.name != "items.db-shm":
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_fixture_untouched_by_ingest(env):
    before = tree_hashes(env.src_root)
    assert "items.db" in before
    ingest.ingest(env.conn, SqliteAdapter(env.src_root), env.cfg)
    after = tree_hashes(env.src_root)
    assert before == after


def test_library_files_untouched(env):
    before = tree_hashes(env.src_root / "library")
    ingest.ingest(env.conn, SqliteAdapter(env.src_root), env.cfg)
    assert before == tree_hashes(env.src_root / "library")
    assert len(before) == len(env.info["library_ids"])


def test_adapter_connection_is_read_only(env):
    adapter = SqliteAdapter(env.src_root)
    conn = adapter.connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE evil(x)")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE items SET note = 'tampered' WHERE id = ?", (env.info["pinned_ids"][0],))
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO kv (k, v) VALUES ('evil', '1')")
    finally:
        conn.close()
