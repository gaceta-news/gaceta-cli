"""Feedback export (roadmap 6): owner's verdicts → report for the upstream collector."""
from __future__ import annotations

import json
import os
import stat
import time

from gaceta import feedback
from gaceta.editor import cluster as run_cluster
from gaceta.editor.cluster import embed_items
from gaceta.providers.embed import FakeEmbedder

from test_cluster import T0, DAY, add_item


def _stories(env):
    for it in [
        ("a1", "A one", "alpha beta gamma", "noted", "public", T0),
        ("a2", "A two", "alpha beta gamma delta", "noted", "public", T0 + DAY),
        ("b1", "B one", "omega psi chi", "noted", "public", T0 + DAY),
    ]:
        add_item(env, *it)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    run_cluster.cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)
    rows = env.conn.execute(
        "SELECT si.story_id, si.item_id FROM story_items si JOIN stories s ON s.id = si.story_id "
        "WHERE s.reader_id = ?", (env.cfg["id"],)).fetchall()
    by_item = {r["item_id"]: r["story_id"] for r in rows}
    assert by_item["a1"] == by_item["a2"] != by_item["b1"]
    return by_item["a1"], by_item["b1"]


def _react(env, sid, kind, by, at=None):
    env.conn.execute(
        "INSERT INTO reactions (reader_id, story_id, kind, at, by_name) VALUES (?, ?, ?, ?, ?)",
        (env.cfg["id"], sid, kind, at or int(time.time()), by))
    env.conn.commit()


def _seen(env, sid, by, at=None):
    env.conn.execute(
        "INSERT INTO seen (reader_id, by_name, story_id, rev, at) VALUES (?, ?, ?, 'r1', ?)",
        (env.cfg["id"], by, sid, at or int(time.time())))
    env.conn.commit()


def test_owner_verdicts_only_and_more_beats_seen(env):
    a, b = _stories(env)
    owner = env.cfg["name"]
    _seen(env, a, owner)
    _react(env, a, "more", owner)
    _seen(env, b, owner)
    _react(env, b, "less", "Bob")  # someone else's tap never counts
    rep = feedback.build(env.conn, env.cfg["id"], env.cfg)
    got = {s["story_id"]: s["verdict"] for s in rep["stories"]}
    assert got == {a: "mas", b: "visto"}
    assert rep["totals"] == {"mas": 1, "menos": 0, "visto": 1}
    src = {s["source"]: s for s in rep["sources"]}
    assert src["test:direct"]["mas"] == 1 and src["test:direct"]["visto"] == 1


def test_window_and_ping_check(env):
    a, b = _stories(env)
    owner = env.cfg["name"]
    _react(env, a, "less", owner)
    _react(env, b, "more", owner, at=int(time.time()) - 60 * 86400)  # outside 30d
    env.conn.execute("INSERT INTO told (reader_id, item_id, channel, at) VALUES (?, 'a1', 'tg', 0)",
                     (env.cfg["id"],))
    env.conn.commit()
    rep = feedback.build(env.conn, env.cfg["id"], env.cfg, days=30)
    assert [s["story_id"] for s in rep["stories"]] == [a]
    assert rep["pinged_but_menos"] == [a]
    md = feedback.render_md(rep)
    assert "Pings they marked Menos así" in md and "a1" in md


def test_export_writes_private_md_and_json(env, tmp_path):
    a, _ = _stories(env)
    _react(env, a, "more", env.cfg["name"])
    out, rep = feedback.export(env.conn, env.cfg["id"], env.cfg, out=tmp_path / "fb" / "x.md")
    assert out.exists() and out.with_suffix(".json").exists()
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert json.loads(out.with_suffix(".json").read_text())["totals"]["mas"] == 1
    assert "**Más así**" in out.read_text()


def test_pipe_in_source_name_is_escaped():
    rep = {"owner": "X", "generated_at": 0, "days": 30, "totals": {"mas": 0, "menos": 0, "visto": 0},
           "pinged_but_menos": [], "stories": [],
           "sources": [{"source": "A | B", "kind": "youtube", "mas": 1, "menos": 0, "visto": 0, "solo": 0}]}
    assert "| A \\| B | youtube |" in feedback.render_md(rep)


def test_default_out_is_in_reader_dir(env):
    assert feedback.default_out(env.cfg).parent == env.data_dir / "readers" / env.cfg["id"]


def test_empty_report(env):
    rep = feedback.build(env.conn, env.cfg["id"], env.cfg)
    assert rep["stories"] == [] and "(no verdicts yet)" in feedback.render_md(rep)
