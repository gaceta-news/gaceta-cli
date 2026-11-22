"""SQLite access for gaceta's own store."""
from __future__ import annotations

import sqlite3
from importlib import resources

SCHEMA_VERSION = 11

# v4 (additive): story_versions.members — member count tracked per version so
# write-ups only rerun when a story gained members. ALTER-based because schema.sql
# only creates tables that don't exist yet.
MIGRATIONS = (
    ("ALTER TABLE story_versions ADD COLUMN members INTEGER", "duplicate column"),
    # v5: who physically reacted (a shared paper is read by several people; only the
    # edition owner's reactions steer that owner's ranking). Free text, e.g. 'alice', 'bob'.
    ("ALTER TABLE reactions ADD COLUMN by_name TEXT", "duplicate column"),
    # v6: per-person seen table is pure CREATE IF NOT EXISTS (schema.sql) — no ALTER.
    # v7: source_decisions (incl. exported_at) is pure CREATE IF NOT EXISTS — no ALTER.
    # v8: agent_sends is pure CREATE IF NOT EXISTS — no ALTER.
    # v10 (additive, nostr-wot roadmap 11a): items.author_pubkey — the hex pubkey of a
    # nostr item's author, so later code can tell followed (origin 'sqlite') from
    # WoT-discovered (origin 'nostr_wot') authors without a name lookup.
    ("ALTER TABLE items ADD COLUMN author_pubkey TEXT", "duplicate column"),
    # v11 (additive): story_versions.what_changed — what this version adds over the
    # previous one, so a story that resurfaces after 'Visto' says why. NULL = not
    # computed yet; '' = computed, nothing material (only reworded).
    ("ALTER TABLE story_versions ADD COLUMN what_changed TEXT", "duplicate column"),
)


# How long a connection waits for another one's lock before raising
# "database is locked". An edition build writes for minutes while `gaceta serve`
# answers the page; both share this file.
BUSY_TIMEOUT_S = 30.0


def connect(path) -> sqlite3.Connection:
    """Open gaceta's store.

    WAL journal: readers (the served page, /api/state) never block the edition
    writer and the writer never blocks them. With the old rollback journal a
    long build plus the server's own writes deadlocked the file and every
    /api/* call failed with OperationalError (the reaction bar vanished).
    journal_mode=WAL is persistent in the file; setting it again is a no-op.
    """
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
    if str(path) != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # Locked by a pre-WAL writer this instant: keep the current mode,
            # the next connection switches it.
            pass
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Apply schema.sql idempotently (additive migrations) and stamp meta.schema_version.

    The stamp only ever moves up: an existing v1 db migrated by this code becomes v2;
    a db already stamped higher (never downgraded) keeps its version.
    """
    schema = (resources.files("gaceta") / "schema.sql").read_text()
    conn.executescript(schema)
    for sql, already in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            if already not in str(e).lower():
                raise
    conn.execute(
        "INSERT INTO meta (k, v) VALUES ('schema_version', ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v "
        "WHERE CAST(meta.v AS INTEGER) < CAST(excluded.v AS INTEGER)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
