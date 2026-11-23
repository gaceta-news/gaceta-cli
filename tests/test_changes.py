"""'Desde que la viste': what_changed per story version (v11), its edition/HTML
plumbing and seen_at in /api/state."""
from __future__ import annotations

import json

from gaceta.editor import changes
from gaceta.edition.build import _story_entry
from gaceta.render.html import _render_story

from test_cluster import T0, DAY, add_item
from test_writeup import FakeWriteup

NOW = T0 + 10 * DAY


def _story(env, members=(("a1", T0), ("a2", T0 + DAY))):
    rid = env.cfg["id"]
    for iid, ts in members:
        add_item(env, iid, f"T {iid}", f"body of {iid}", "noted", "public", ts)
        env.conn.execute("UPDATE items SET ingested_at = ? WHERE id = ?", (ts, iid))
    env.conn.execute(
        "INSERT INTO stories (id, reader_id, title, summary, status, privacy, first_seen, last_seen) "
        "VALUES ('s1', ?, 'T', 'v2 text', 'developing', 'public', ?, ?)", (rid, T0, NOW))
    for iid, _ in members:
        env.conn.execute("INSERT INTO story_items (story_id, item_id) VALUES ('s1', ?)", (iid,))
    env.conn.execute(
        "INSERT INTO story_versions (story_id, version, summary, written_at, members) "
        "VALUES ('s1', 1, 'v1 text', ?, 1), ('s1', 2, 'v2 text', ?, 2)",
        (T0 + 3600, T0 + DAY + 3600))
    env.conn.commit()


def test_parse_changes():
    assert changes.parse_changes('{"what_changed": "Ahora X."}') == "Ahora X."
    assert changes.parse_changes('{"what_changed": null}') == ""
    assert changes.parse_changes("garbage") is None
    assert changes.parse_changes('{"other": 1}') is None


def test_note_changes_new_items_only_and_cached(env):
    _story(env)
    chat = FakeWriteup('{"what_changed": "Se suma un segundo reporte."}')
    r = changes.note_changes(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r["noted"] == 1 and r["llm_calls"] == 1
    prompt = chat.prompts[0]
    assert "PREVIOUS CARD" in prompt and "v1 text" in prompt and "v2 text" in prompt
    assert "T a2" in prompt and "T a1" not in prompt  # only the item that joined
    row = env.conn.execute("SELECT what_changed FROM story_versions WHERE version = 2").fetchone()
    assert row[0] == "Se suma un segundo reporte."
    # computed once: a rerun has nothing to do
    r2 = changes.note_changes(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r2["candidates"] == 0 and r2["llm_calls"] == 0


def test_reworded_is_empty_and_budget(env):
    _story(env)
    r = changes.note_changes(env.conn, env.cfg["id"], env.cfg,
                             chat=FakeWriteup('{"what_changed": null}'), now=NOW)
    assert r["unchanged"] == 1
    assert env.conn.execute(
        "SELECT what_changed FROM story_versions WHERE version = 2").fetchone()[0] == ""


def test_budget_zero_leaves_null(env):
    _story(env)
    r = changes.note_changes(env.conn, env.cfg["id"], env.cfg, max_llm_calls=0,
                             chat=FakeWriteup('{"what_changed": "x"}'), now=NOW)
    assert r["budget_exhausted"] and env.conn.execute(
        "SELECT what_changed FROM story_versions WHERE version = 2").fetchone()[0] is None


def test_invalid_reply_is_cached_not_repaid(env):
    """Step 0: an invalid what_changed reply IS cached in llm_decisions — the
    version is still a candidate next run, but the cached reply is re-read, so
    the model is never re-asked (and never re-paid) for it."""
    _story(env)
    chat = FakeWriteup("no JSON anywhere in this reply")
    r1 = changes.note_changes(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r1["failed"] == 1 and r1["llm_calls"] == 1
    assert env.conn.execute("SELECT COUNT(*) FROM llm_decisions").fetchone()[0] == 1

    def never(messages, **kw):
        raise AssertionError("invalid replies are cached; no second call expected")

    chat2 = FakeWriteup("unused")  # same default name → the same cache key
    chat2.complete = never
    r2 = changes.note_changes(env.conn, env.cfg["id"], env.cfg, chat=chat2, now=NOW)
    assert r2["llm_calls"] == 0 and r2["llm_cache_hits"] == 1 and r2["failed"] == 1
    assert env.conn.execute(
        "SELECT what_changed FROM story_versions WHERE version = 2").fetchone()[0] is None


def test_private_items_never_reach_a_public_model(env):
    _story(env)
    env.conn.execute("UPDATE annotations SET item_privacy = 'private' WHERE item_id = 'a2'")
    chat = FakeWriteup('{"what_changed": "x"}', allow_private=False)
    r = changes.note_changes(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r["skipped"] == 1 and chat.prompts == []


def test_entry_history_and_rendered_since_block(env):
    _story(env)
    env.conn.execute("UPDATE story_versions SET what_changed = 'Nuevo <b>dato</b>.' WHERE version = 2")
    row = dict(env.conn.execute(
        "SELECT id, title, summary, why_it_matters, status, confidence, privacy, first_seen, "
        "last_seen, 2 AS n_items FROM stories WHERE id = 's1'").fetchone())
    entry = _story_entry(env.conn, row, NOW)
    assert [h["version"] for h in entry["history"]] == [1, 2]
    html = _render_story("s1", json.loads(json.dumps(entry)))
    assert '<div class="since" hidden>' in html
    assert f'<li data-at="{T0 + DAY + 3600}">Nuevo &lt;b&gt;dato&lt;/b&gt;.</li>' in html
    assert f'<p data-at="{T0 + 3600}">v1 text</p>' in html  # what they read
    # single-version stories carry no block
    entry.pop("history")
    assert "since" not in _render_story("s1", entry)


def test_state_carries_seen_at(server):  # noqa: F811
    from test_seen import get_state, post_json
    port, env, ed = server
    sid = next(iter(ed["stories"]))
    post_json(port, "/api/seen", {"reader": env.cfg["id"], "story_ids": [sid],
                                  "revs": {sid: ed["stories"][sid]["rev"]}, "by": "Alice"})
    _, st = get_state(port, env.cfg["id"], "Alice")
    assert isinstance(st["seen_at"][sid], int)


from test_serve import server  # noqa: E402,F401 — shared fixture
