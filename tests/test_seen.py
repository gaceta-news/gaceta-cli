"""Seen endpoints: per-person seen marks (v6), validation, isolation between
readers of the same paper, unseen deletes, state includes seen."""
from __future__ import annotations

import json

from test_serve import req, server  # noqa: F401 — shared fixture + http helper


def post_json(port, path, payload, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    return req(port, "POST", path, body=json.dumps(payload), headers=h)


def get_state(port, reader, by):
    status, _, body = req(port, "GET",
                          f"/api/state?reader={reader}&by={by}")
    return status, (json.loads(body) if status == 200 else None)


def test_seen_ok_then_state_is_per_person(server):
    port, env, ed = server
    ids = list(ed["stories"])[:2]
    revs = {sid: ed["stories"][sid]["rev"] for sid in ids}

    status, _, body = post_json(port, "/api/seen", {
        "reader": env.cfg["id"], "story_ids": ids, "revs": revs, "by": "Alice"})
    assert status == 200
    assert json.loads(body)["seen"] == revs

    rows = env.conn.execute(
        "SELECT story_id, rev FROM seen WHERE reader_id = ? AND by_name = 'Alice'",
        (env.cfg["id"],)).fetchall()
    assert {r["story_id"]: r["rev"] for r in rows} == revs
    assert env.conn.execute(
        "SELECT COUNT(*) FROM seen WHERE by_name = 'Bob'").fetchone()[0] == 0

    # Alice sees his marks; Bob's state is untouched
    _, st_alice = get_state(port, env.cfg["id"], "Alice")
    _, st_bob = get_state(port, env.cfg["id"], "Bob")
    assert st_alice is not None and st_bob is not None
    assert st_alice["seen"] == revs
    assert st_bob["seen"] == {}

    # upsert: marking again at a new rev moves it
    new_rev = "a" * 12
    status, _, _ = post_json(port, "/api/seen", {
        "reader": env.cfg["id"], "story_ids": ids[:1],
        "revs": {ids[0]: new_rev}, "by": "Alice"})
    assert status == 200
    _, st_alice = get_state(port, env.cfg["id"], "Alice")
    assert st_alice is not None
    assert st_alice["seen"][ids[0]] == new_rev
    assert env.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 2


def test_seen_excludes_stories_outside_the_edition(server):
    port, env, ed = server
    env.conn.execute(
        "INSERT INTO stories (id, reader_id, title, privacy) "
        "VALUES ('zzz-not-in-edition', ?, 'old', 'private')", (env.cfg["id"],))
    env.conn.execute(
        "INSERT INTO seen (reader_id, by_name, story_id, rev, at) "
        "VALUES (?, 'Alice', 'zzz-not-in-edition', 'abc123', 0)", (env.cfg["id"],))
    env.conn.commit()
    _, st = get_state(port, env.cfg["id"], "Alice")
    assert st is not None
    assert "zzz-not-in-edition" not in st["seen"]


def test_seen_validation(server):
    port, env, ed = server
    sid = next(iter(ed["stories"]))
    rev = ed["stories"][sid]["rev"]
    base = {"reader": env.cfg["id"], "by": "Alice"}

    bad_payloads = [
        {**base, "story_ids": [sid], "revs": {sid: "XYZ"}},            # non-hex
        {**base, "story_ids": [sid], "revs": {sid: "a" * 17}},         # too long
        {**base, "story_ids": [sid], "revs": {}},                      # missing rev
        {**base, "story_ids": [sid], "revs": [rev]},                   # revs not obj
        {**base, "story_ids": "nope", "revs": {sid: rev}},             # not a list
        {**base, "story_ids": [], "revs": {}},                         # empty list
        {**base, "story_ids": [sid] * 201, "revs": {sid: rev}},        # > 200
        {**base, "story_ids": ["no-such-story"], "revs": {"no-such-story": rev}},
        {"reader": "nope", "story_ids": [sid], "revs": {sid: rev}, "by": "J"},
        {**base, "story_ids": [sid], "revs": {sid: rev}, "by": ""},    # empty by
    ]
    for payload in bad_payloads:
        status, _, _ = post_json(port, "/api/seen", payload)
        assert status in (400, 404), (status, payload)

    # wrong content type, cross-origin, oversized body
    status, _, _ = req(port, "POST", "/api/seen", body="{}",
                       headers={"Content-Type": "text/plain"})
    assert status == 415
    status, _, _ = post_json(port, "/api/seen",
                             {**base, "story_ids": [sid], "revs": {sid: rev}},
                             headers={"Origin": "https://evil.example"})
    assert status == 403
    big = {**base, "story_ids": [f"s{i}" * 60 for i in range(200)],
           "revs": {f"s{i}" * 60: "a" * 16 for i in range(200)}}
    assert len(json.dumps(big)) > 32768
    status, _, _ = post_json(port, "/api/seen", big)
    assert status in (400, 404, 413)
    assert env.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0

    # GET on the POST-only endpoints
    for path in ("/api/seen", "/api/unseen", "/api/react"):
        status, _, _ = req(port, "GET", path)
        assert status == 405, path


def test_unseen_deletes_only_that_person(server):
    port, env, ed = server
    ids = list(ed["stories"])[:2]
    revs = {sid: ed["stories"][sid]["rev"] for sid in ids}
    for who in ("Alice", "Bob"):
        status, _, _ = post_json(port, "/api/seen", {
            "reader": env.cfg["id"], "story_ids": ids, "revs": revs, "by": who})
        assert status == 200

    status, _, _ = post_json(port, "/api/unseen", {
        "reader": env.cfg["id"], "story_id": ids[0], "by": "Alice"})
    assert status == 200
    _, st_alice = get_state(port, env.cfg["id"], "Alice")
    _, st_bob = get_state(port, env.cfg["id"], "Bob")
    assert st_alice is not None and st_bob is not None
    assert st_alice["seen"] == {ids[1]: revs[ids[1]]}
    assert st_bob["seen"] == revs  # Bob untouched

    # deleting something never marked is still ok; junk is rejected
    status, _, _ = post_json(port, "/api/unseen", {
        "reader": env.cfg["id"], "story_id": "whatever", "by": "Alice"})
    assert status == 200
    for payload in (
        {"reader": env.cfg["id"], "story_id": "", "by": "J"},
        {"reader": env.cfg["id"], "story_id": "x", "by": ""},
        {"reader": "nope", "story_id": "x", "by": "J"},
    ):
        status, _, _ = post_json(port, "/api/unseen", payload)
        assert status in (400, 404)
