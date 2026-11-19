"""Ingest + privacy canary tests. The canary assertions are never deleted or skipped."""
from __future__ import annotations

from gaceta import ingest, views
from gaceta.sources.sqlite import SqliteAdapter

CANARIES = ("zorblax", "qq-secret-farm", "glimmerhouse")


def run_ingest(env):
    return ingest.ingest(env.conn, SqliteAdapter(env.src_root), env.cfg)


def test_all_items_ingested(env):
    r = run_ingest(env)
    assert r["new"] == env.info["n_items"]
    assert r["updated"] == 0
    n = env.conn.execute("SELECT COUNT(*) FROM annotations WHERE reader_id = ?", (env.cfg["id"],)).fetchone()[0]
    assert n == env.info["n_items"]


def test_pinned_items_private(env):
    run_ingest(env)
    for item_id in env.info["pinned_ids"]:
        row = env.annotation(item_id)
        assert row["item_privacy"] == "private", item_id
        assert row["privacy_reason"].startswith("pinned:")
        assert row["privacy_reason"] != "pinned:"  # an index is present


def test_household_items(env):
    run_ingest(env)
    for item_id in env.info["household_ids"]:
        row = env.annotation(item_id)
        assert row["item_privacy"] == "household", item_id
        assert row["privacy_reason"].startswith("household:")


def test_innocent_items_public(env):
    run_ingest(env)
    special = set(env.info["pinned_ids"]) | set(env.info["household_ids"])
    rows = env.conn.execute(
        "SELECT item_id, item_privacy FROM annotations WHERE reader_id = ?", (env.cfg["id"],)
    ).fetchall()
    assert len(rows) == env.info["n_items"]
    for row in rows:
        if row["item_id"] not in special:
            assert row["item_privacy"] == "public", row["item_id"]


def test_note_canary_labelled_public_from_content(env):
    """The note-only canary item's content is clean → public label, note stays private."""
    run_ingest(env)
    assert len(env.info["note_canary_ids"]) == 1
    item_id = env.info["note_canary_ids"][0]
    row = env.annotation(item_id)
    assert row["item_privacy"] == "public", item_id
    assert "zorblax" in row["note"].lower()  # the note really does carry the canary


def test_public_view_never_leaks_canaries(env):
    run_ingest(env)
    for row in views.public_items(env.conn, env.cfg["id"], "public"):
        blob = " ".join(str(v) for v in row).casefold()
        for canary in CANARIES:
            assert canary not in blob, (row["id"], canary)
    # same at household level (household terms must not appear either)
    for row in views.public_items(env.conn, env.cfg["id"], "household"):
        blob = " ".join(str(v) for v in row).casefold()
        assert "zorblax" not in blob and "qq-secret-farm" not in blob


def test_public_view_excludes_private_and_household(env):
    run_ingest(env)
    ids = {r["id"] for r in views.public_items(env.conn, env.cfg["id"], "public")}
    for item_id in env.info["pinned_ids"] + env.info["household_ids"]:
        assert item_id not in ids


def test_idempotent(env):
    r1 = run_ingest(env)
    counts1 = {
        t: env.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("items", "annotations", "sources", "told")
    }
    r2 = run_ingest(env)
    assert r2["new"] == 0
    counts2 = {
        t: env.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("items", "annotations", "sources", "told")
    }
    assert counts1 == counts2
    assert r1["new"] == env.info["n_items"]


def test_told_rows(env):
    run_ingest(env)
    n = env.conn.execute(
        "SELECT COUNT(*) FROM told WHERE reader_id = ?", (env.cfg["id"],)
    ).fetchone()[0]
    assert n == len(env.info["told_ids"])


def test_verdict_normalisation(env):
    run_ingest(env)
    verdicts = dict(
        env.conn.execute(
            "SELECT item_id, verdict FROM annotations WHERE reader_id = ?", (env.cfg["id"],)
        ).fetchall()
    )
    assert set(verdicts.values()) <= {"told", "noted", "skip", "pending"}
    # the fixture has items with verdict '' → pending
    assert "pending" in verdicts.values()


def test_source_kinds(env):
    run_ingest(env)
    kinds = dict(
        env.conn.execute(
            "SELECT s.id, s.kind FROM sources s"
        ).fetchall()
    )
    assert any(k.startswith("youtube:") for k in kinds)
    assert kinds.get("feed:hn") == "hn"
    assert kinds.get("feed:trial-gazette") == "rss"
    assert kinds.get("feed:orbital-releases") == "release"


def test_library_paths_linked(env):
    run_ingest(env)
    for item_id in env.info["library_ids"]:
        row = env.annotation(item_id)
        assert row["library_path"] and row["library_path"].endswith(
            item_id.replace(":", "_") + ".md"
        ), item_id
