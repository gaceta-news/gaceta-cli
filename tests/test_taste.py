"""Taste steering: only the OWNER's reactions change the ranking; hide excludes;
score_reasons land in the edition JSON (docs/EDITION.md)."""
from __future__ import annotations

import time

import pytest

from gaceta.edition import build_edition
from gaceta.editor import cluster as run_cluster
from gaceta.editor.cluster import embed_items
from gaceta.providers.embed import FakeEmbedder

from test_cluster import T0, DAY, add_item
from test_edition_build import _edition_date


def _react(env, story_id, kind, by):
    env.conn.execute(
        "INSERT INTO reactions (reader_id, story_id, kind, at, by_name) VALUES (?, ?, ?, ?, ?)",
        (env.cfg["id"], story_id, kind, int(time.time()), by),
    )
    env.conn.commit()


def _fixture(env):
    """Two clearly separable stories + a third similar to the first."""
    for it in [
        ("a1", "A one", "alpha beta gamma", "noted", "public", T0),
        ("a2", "A two", "alpha beta gamma delta", "noted", "public", T0 + DAY),
        ("b1", "B one", "omega psi chi", "noted", "public", T0 + DAY),
        ("c1", "C one", "alpha beta gamma epsilon", "noted", "public", T0 + DAY),
    ]:
        add_item(env, *it)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    run_cluster.cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)


def _build(env, **kw):
    return build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                         max_privacy="private", force=True, data_dir=env.data_dir, **kw)


def _by_title(ed):
    return {v["title"]: (sid, v) for sid, v in ed["stories"].items()}


def _rank(ed, title):
    order = list(ed["portada"]) + [s for sec in ed["sections"] for s in sec["stories"]]
    sid = _by_title(ed)[title][0]
    return order.index(sid)


def test_score_reasons_present_on_every_story(env):
    _fixture(env)
    ed = _build(env)
    assert ed["stories"]
    for sid, story in ed["stories"].items():
        assert "score_reasons" in story, sid
        assert isinstance(story["score_reasons"], list)


def test_owner_up_raises_and_explains(env):
    _fixture(env)
    ed0 = _build(env)
    sid = _by_title(ed0)["B one"][0]  # currently below A one (fewer items)
    assert _rank(ed0, "A one") < _rank(ed0, "B one")
    _react(env, sid, "up", "Test Reader")  # owner name, exact
    ed = _build(env)
    assert _rank(ed, "B one") < _rank(ed, "A one")
    reasons = _by_title(ed)["B one"][1]["score_reasons"]
    assert any("👍" in r and "+2.0" not in r for r in reasons)


def test_more_less_weights(env):
    _fixture(env)
    ed0 = _build(env)
    sid_a = _by_title(ed0)["A one"][0]
    sid_b = _by_title(ed0)["B one"][0]
    _react(env, sid_a, "more", "Test Reader")
    _react(env, sid_b, "less", "Test Reader")
    ed = _build(env)
    assert any("Más así" in r and "+2.0" not in r
               for r in _by_title(ed)["A one"][1]["score_reasons"])
    assert any("Menos así" in r and "-2.5" not in r
               for r in _by_title(ed)["B one"][1]["score_reasons"])


def test_owner_name_case_insensitive_and_null(env):
    _fixture(env)
    ed0 = _build(env)
    sid = _by_title(ed0)["B one"][0]
    _react(env, sid, "up", "test reader")  # case differs
    ed = _build(env)
    assert any("👍" in r for r in _by_title(ed)["B one"][1]["score_reasons"])


def test_household_reactions_do_not_steer(env):
    _fixture(env)
    ed0 = _build(env)
    sid = _by_title(ed0)["B one"][0]
    _react(env, sid, "up", "Bob")  # household member: stored, but no steering
    ed = _build(env)
    assert _rank(ed, "B one") > _rank(ed, "A one")
    assert not any("👍" in r for r in _by_title(ed)["B one"][1]["score_reasons"])
    n = env.conn.execute(
        "SELECT COUNT(*) FROM reactions WHERE by_name = 'Bob'").fetchone()[0]
    assert n == 1  # …but the reaction IS kept for her own future edition


def test_hide_excludes_story(env):
    _fixture(env)
    ed0 = _build(env)
    sid = _by_title(ed0)["C one"][0]
    _react(env, sid, "hide", "Test Reader")
    ed = _build(env)
    assert sid not in ed["stories"]
    assert sid not in ed["portada"]
    assert sid not in [s for sec in ed["sections"] for s in sec["stories"]]


def test_similarity_transfer_from_liked_story(env):
    _fixture(env)
    ed0 = _build(env)
    # A one (a1+a2) and C one share the alpha/beta/gamma vocabulary; react to C, A rises
    sid_c = _by_title(ed0)["C one"][0]
    _react(env, sid_c, "up", "Test Reader")
    ed = _build(env)
    reasons_a = _by_title(ed)["A one"][1]["score_reasons"]
    assert "Parecido a algo que te gustó" in reasons_a
    assert all("(+" not in r and "(-" not in r for r in reasons_a)
    reasons_b = _by_title(ed)["B one"][1]["score_reasons"]
    assert not any("Parecido" in r for r in reasons_b)  # unrelated vocabulary


@pytest.mark.parametrize("kind, phrase", [("down", "no te convence"), ("more", "suele gustarte")])
def test_author_affinity(env, kind, phrase):
    _fixture(env)
    # attribute every item of story A to the same author Alice dislikes
    ed0 = _build(env)
    sid_a = _by_title(ed0)["A one"][0]
    sid_b = _by_title(ed0)["B one"][0]
    env.conn.execute(
        "UPDATE items SET author = 'Charlie Columnist' WHERE id IN "
        "(SELECT item_id FROM story_items WHERE story_id IN (?, ?))",
        (sid_a, sid_b),
    )
    env.conn.commit()
    _react(env, sid_b, kind, "Test Reader")
    ed = _build(env)
    reasons_a = _by_title(ed)["A one"][1]["score_reasons"]
    assert any(phrase in r and "Charlie Columnist" in r for r in reasons_a)
    assert all("(+" not in r and "(-" not in r for r in reasons_a)


def test_reaction_decays_with_age(env):
    _fixture(env)
    ed0 = _build(env)
    sid = _by_title(ed0)["B one"][0]
    now = time.time()
    # 4.5 days old: full for 2 days, then linear to 0 at 7 → half weight
    env.conn.execute(
        "INSERT INTO reactions (reader_id, story_id, kind, at, by_name) VALUES (?, ?, 'up', ?, ?)",
        (env.cfg["id"], sid, int(now - 4.5 * DAY), "Test Reader"),
    )
    env.conn.commit()
    ed = _build(env, now=int(now) + 60)
    from gaceta.editor.taste import adjustments
    adj = adjustments(env.conn, env.cfg["id"], env.cfg, now=int(now) + 60)
    assert abs(adj.delta[sid] - 1.0) < 0.01
    assert any("👍" in r and "+1.0" not in r for r in _by_title(ed)["B one"][1]["score_reasons"])


def test_old_reaction_fades_entirely(env):
    _fixture(env)
    ed0 = _build(env)
    sid = _by_title(ed0)["B one"][0]
    now = time.time()
    env.conn.execute(
        "INSERT INTO reactions (reader_id, story_id, kind, at, by_name) VALUES (?, ?, 'up', ?, ?)",
        (env.cfg["id"], sid, int(now - 10 * DAY), "Test Reader"),
    )
    env.conn.commit()
    ed = _build(env, now=int(now) + 60)
    # no boost, no stale reason line: ranking is back to base order
    assert _rank(ed, "B one") > _rank(ed, "A one")
    assert not any("👍" in r for r in _by_title(ed)["B one"][1]["score_reasons"])
