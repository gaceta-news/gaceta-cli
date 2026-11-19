"""Clustering: determinism, incremental joins, rebuild wipe, story privacy canaries."""
from __future__ import annotations

import numpy as np

from gaceta import views
from gaceta.editor.cluster import cluster, embed_items
from gaceta.providers.embed import FakeEmbedder

T0 = 1_700_000_000
DAY = 86400


def add_item(env, iid, title, body, verdict="noted", privacy="public", published=T0):
    env.conn.execute(
        "INSERT OR IGNORE INTO readers (id, name, langs, created_at) "
        "VALUES (?, 'Test Reader', 'en', 0)", (env.cfg["id"],)
    )
    env.conn.execute(
        "INSERT OR IGNORE INTO sources (id, kind) VALUES ('test:direct', 'other')"
    )
    env.conn.execute(
        "INSERT INTO items (id, source_id, author, title, url, published, body, ingested_at, origin) "
        "VALUES (?, 'test:direct', NULL, ?, NULL, ?, ?, 0, 'test')",
        (iid, title, published, body),
    )
    env.conn.execute(
        "INSERT INTO annotations (reader_id, item_id, verdict, note, library_path, "
        "item_privacy, privacy_reason, annotated_at) VALUES (?, ?, ?, NULL, NULL, ?, 'test', 0)",
        (env.cfg["id"], iid, verdict, privacy),
    )


def stories_of(env):
    """reader's stories as {story_id: [(item_id, role), ...]} + row fields."""
    out = {}
    for s in env.conn.execute(
        "SELECT id, title, status, privacy, section, first_seen, last_seen "
        "FROM stories WHERE reader_id = ? ORDER BY id", (env.cfg["id"],)
    ):
        members = env.conn.execute(
            "SELECT item_id, role FROM story_items WHERE story_id = ? ORDER BY item_id", (s["id"],)
        ).fetchall()
        out[s["id"]] = {k: s[k] for k in s.keys()} | {"members": [(m["item_id"], m["role"]) for m in members]}
    return out


def test_deterministic_same_input_same_output(env):
    add_item(env, "a1", "A one", "alpha beta gamma", published=T0)
    add_item(env, "a2", "A two", "alpha beta gamma", published=T0 + DAY)
    add_item(env, "a3", "A three", "alpha beta gamma", published=T0 + 2 * DAY)
    add_item(env, "b1", "B one", "delta epsilon zeta", published=T0 + 3 * DAY)
    add_item(env, "b2", "B two", "delta epsilon zeta", published=T0 + 4 * DAY)
    add_item(env, "c1", "C one", "eta theta iota", published=T0 + 5 * DAY)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())

    r1 = cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)
    s1 = stories_of(env)
    r2 = cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)
    s2 = stories_of(env)

    assert s1 == s2  # identical story ids and membership across rebuilds
    assert r1["n_stories"] == r2["n_stories"] == 3
    sizes = sorted(len(v["members"]) for v in s1.values())
    assert sizes == [1, 2, 3]

    for s in s1.values():
        assert s["status"] == "developing"
        assert s["privacy"] == "public"
        assert s["section"] is None
        roles = [r for _, r in s["members"]]
        assert roles[0] == "origin" and all(r == "update" for r in roles[1:])
        assert s["first_seen"] <= s["last_seen"]
    # story title = earliest item's title
    by_members = {tuple(sorted(m for m, _ in v["members"])): v for v in s1.values()}
    assert by_members[("a1", "a2", "a3")]["title"] == "A one"
    assert by_members[("b1", "b2")]["title"] == "B one"
    assert by_members[("c1",)]["title"] == "C one"


def test_incremental_join_existing_story(env):
    add_item(env, "a1", "A one", "alpha beta gamma", published=T0)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    r1 = cluster(env.conn, env.cfg["id"], env.cfg)
    assert r1["n_new_stories"] == 1 and r1["n_stories"] == 1

    add_item(env, "a2", "A two", "alpha beta gamma", published=T0 + DAY)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    r2 = cluster(env.conn, env.cfg["id"], env.cfg)  # no rebuild
    assert r2["n_new_stories"] == 0 and r2["n_joined_stories"] == 1
    assert r2["n_stories"] == 1
    s = stories_of(env)
    assert len(next(iter(s.values()))["members"]) == 2


def test_window_closes_story(env):
    add_item(env, "a1", "A one", "alpha beta gamma", published=T0)
    add_item(env, "a2", "A far later", "alpha beta gamma", published=T0 + 60 * DAY)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    r = cluster(env.conn, env.cfg["id"], env.cfg)
    assert r["n_new_stories"] == 2  # identical text, but outside the 21-day window


def test_rebuild_wipes_versions_and_reclusters(env):
    add_item(env, "a1", "A one", "alpha beta gamma", published=T0)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    cluster(env.conn, env.cfg["id"], env.cfg)
    sid = env.conn.execute(
        "SELECT id FROM stories WHERE reader_id = ?", (env.cfg["id"],)
    ).fetchone()[0]
    env.conn.execute(
        "INSERT INTO story_versions (story_id, version, written_at) VALUES (?, 1, 0)", (sid,)
    )
    env.conn.commit()
    cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)
    assert env.conn.execute("SELECT COUNT(*) FROM story_versions").fetchone()[0] == 0
    assert env.conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 1
    assert env.conn.execute("SELECT COUNT(*) FROM story_items").fetchone()[0] == 1


def test_story_privacy_pinned_canary_is_private_and_hidden(env):
    """A story containing a pinned (private) item is private; the public story view
    can never emit its text."""
    add_item(env, "p1", "zorblax quiet routine notes", "quiet routine words",
             privacy="private", published=T0)
    add_item(env, "p2", "zorblax quiet routine notes", "quiet routine words",
             privacy="private", published=T0 + DAY)
    add_item(env, "g1", "garden tour", "garden tour words", published=T0 + 2 * DAY)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    cluster(env.conn, env.cfg["id"], env.cfg)

    rows = env.conn.execute(
        "SELECT id, privacy FROM stories WHERE reader_id = ?", (env.cfg["id"],)
    ).fetchall()
    by_id = {r["id"]: r["privacy"] for r in rows}
    assert "private" in by_id.values() and "public" in by_id.values()

    pub = views.public_stories(env.conn, env.cfg["id"], "public")
    assert [r["id"] for r in pub if by_id[r["id"]] != "public"] == []
    for r in pub:  # canary text never reaches a public render
        blob = " ".join(str(v) for v in r).casefold()
        assert "zorblax" not in blob
    keys = {k for r in pub for k in r.keys()}
    assert keys <= {"id", "title", "summary", "section"}
    # at private level the pinned story is visible to the reader himself
    mine = views.public_stories(env.conn, env.cfg["id"], "private")
    assert len(mine) == len(rows)


def test_household_item_lifts_story_to_household(env):
    add_item(env, "h1", "shared kitchen plan", "shared kitchen plan", privacy="household",
             published=T0)
    add_item(env, "h2", "shared kitchen plan two", "shared kitchen plan",
             published=T0 + DAY)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    cluster(env.conn, env.cfg["id"], env.cfg)
    s = env.conn.execute(
        "SELECT privacy FROM stories WHERE reader_id = ?", (env.cfg["id"],)
    ).fetchall()
    assert all(r["privacy"] == "household" for r in s)
    assert views.public_stories(env.conn, env.cfg["id"], "public") == []
    assert len(views.public_stories(env.conn, env.cfg["id"], "household")) == len(s)
