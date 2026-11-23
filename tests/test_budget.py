"""Per-reader daily LLM budget ([llm] daily_max_calls, llm_usage v12).

Only real (non-cache) chat calls count; the day is the reader's wall clock
([schedule] tz else system local); reaching the limit stops a run exactly like
--max-llm-calls (graceful, the edition continues with what is written); the
count survives across commands and resets next day; `gaceta stats` shows
today's calls/budget.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gaceta import budget as budget_mod
from gaceta.editor import changes, writeup
from gaceta.editor.cluster import Confirmer, _desc_fn, cluster, embed_items
from gaceta.providers.embed import FakeEmbedder
from gaceta.providers.llm import FakeChat

from test_changes import _story
from test_cluster import T0, DAY, add_item
from test_writeup import NOW, GOOD, FakeWriteup, build_story


def _day_of(cfg, now):
    return budget_mod.day_key(cfg, now)


def _used(env, day):
    row = env.conn.execute(
        "SELECT calls FROM llm_usage WHERE reader_id = ? AND day = ?",
        (env.cfg["id"], day)).fetchone()
    return int(row[0]) if row is not None else 0


def _requeue(env):
    """Force every written story back into the write queue (same prompt → cache)."""
    env.conn.execute("UPDATE story_versions SET members = NULL")
    env.conn.commit()


# ---------------------------------------------------------------- day key

def test_day_key_local_by_default_and_utc_when_configured():
    now = 1_767_225_600  # a fixed moment; both cases computed, never hardcoded
    assert budget_mod.day_key({}, now) == datetime.fromtimestamp(now).date().isoformat()
    cfg = {"schedule": {"tz": "UTC"}}
    assert budget_mod.day_key(cfg, now) == \
        datetime.fromtimestamp(now, ZoneInfo("UTC")).date().isoformat()
    # a reader pinned to a zone counts their own calendar days
    assert budget_mod.day_key(cfg, now) == "2026-01-01"
    assert budget_mod.day_key({"schedule": {"tz": "UTC"}}, now + 2 * DAY) == "2026-01-03"


def test_day_key_rejects_unknown_tz():
    with pytest.raises(ValueError, match="tz"):
        budget_mod.day_key({"schedule": {"tz": "No/Such_Zone"}}, 0)


def test_daily_max_calls_validation(env):
    with pytest.raises(ValueError, match="daily_max_calls"):
        budget_mod.DailyBudget(env.conn, env.cfg["id"], {"llm": {"daily_max_calls": -1}})
    with pytest.raises(ValueError, match="daily_max_calls"):
        budget_mod.DailyBudget(env.conn, env.cfg["id"], {"llm": {"daily_max_calls": "many"}})
    assert budget_mod.DailyBudget(env.conn, env.cfg["id"], {}).limit is None  # absent = unlimited
    assert budget_mod.DailyBudget(
        env.conn, env.cfg["id"], {"llm": {"daily_max_calls": 0}}).limit == 0


# ---------------------------------------------------------------- counting

def test_counts_only_real_calls_cache_hits_are_free(env):
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    build_story(env, ("b1", "B one", "delta epsilon zeta", "noted", "public", T0))
    chat = FakeWriteup(GOOD)
    r1 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r1["llm_calls"] == 2
    assert _used(env, _day_of(env.cfg, NOW)) == 2
    # rerun of the same decisions: cached → no calls, no new usage
    _requeue(env)
    r2 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r2["llm_calls"] == 0 and r2["llm_cache_hits"] == 2
    assert _used(env, _day_of(env.cfg, NOW)) == 2


def test_budget_stops_gracefully_at_n(env):
    env.cfg["llm"] = {"daily_max_calls": 1}
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    build_story(env, ("b1", "B one", "delta epsilon zeta", "noted", "public", T0))
    chat = FakeWriteup(GOOD)
    r1 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r1["written"] == 1 and r1["pending"] == 1 and r1["budget_exhausted"]
    assert r1["llm_calls"] == 1 and _used(env, _day_of(env.cfg, NOW)) == 1
    # a second command the same day: the spent budget is remembered, nothing new
    add_item(env, "c1", "C one", "kappa lambda mu", "noted", "public", T0)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    cluster(env.conn, env.cfg["id"], env.cfg)  # no rebuild: a/b keep their versions
    r2 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r2["llm_calls"] == 0 and r2["written"] == 0 and r2["budget_exhausted"]
    assert env.conn.execute("SELECT COUNT(*) FROM story_versions").fetchone()[0] == 1
    # but cache hits stay free even past the daily budget (b/c still pending:
    # only a's decision was ever made and cached)
    _requeue(env)
    r3 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r3["llm_calls"] == 0 and r3["llm_cache_hits"] == 1 and r3["written"] == 1
    assert r3["pending"] == 2 and _used(env, _day_of(env.cfg, NOW)) == 1


def test_budget_resets_next_day(env):
    env.cfg["llm"] = {"daily_max_calls": 1}
    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    build_story(env, ("b1", "B one", "delta epsilon zeta", "noted", "public", T0))
    chat = FakeWriteup(GOOD)
    r1 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=NOW)
    assert r1["budget_exhausted"] and r1["llm_calls"] == 1
    next_day = NOW + 2 * DAY  # a different local day in any zone
    r2 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, chat=chat, now=next_day)
    assert r2["llm_calls"] == 1 and not r2["budget_exhausted"]
    assert _used(env, _day_of(env.cfg, NOW)) == 1
    assert _used(env, _day_of(env.cfg, next_day)) == 1  # a fresh row, a fresh budget


def test_budget_survives_across_commands(env):
    env.cfg["llm"] = {"daily_max_calls": 1}
    _story(env)  # a version-2 story waiting for a change note (items a1/a2)
    add_item(env, "w1", "W one", "omega psi chi", "noted", "public", T0)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    cluster(env.conn, env.cfg["id"], env.cfg)  # no rebuild: s1 keeps its versions
    writeup.write_stories(env.conn, env.cfg["id"], env.cfg,
                           chat=FakeWriteup(GOOD), now=NOW)  # spends the day's one call
    # a separate command the same day (change notes) is stopped by the same budget
    r = changes.note_changes(env.conn, env.cfg["id"], env.cfg,
                             chat=FakeWriteup('{"what_changed": "x"}'), now=NOW)
    assert r["llm_calls"] == 0 and r["budget_exhausted"]
    assert env.conn.execute(
        "SELECT what_changed FROM story_versions WHERE story_id = 's1' AND version = 2"
    ).fetchone()[0] is None  # graceful: left for another day, nothing broken


def test_cluster_confirm_counts_against_the_daily_budget(env):
    env.cfg["llm"] = {"daily_max_calls": 1,
                      "editor": {"provider": "fake", "allow_private": True}}
    # fixture wording from test_confirm._seed_zone_pair: cosine lands in the
    # borderline confirm zone [.55, .70) with the fake embedder
    add_item(env, "i0", "first", "alpha beta mu gamma", "noted", "public", T0)
    add_item(env, "i1", "second", "alpha beta mu delta", "noted", "public", T0)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    env.cfg["cluster"] = {"confirm": True, "confirm_low": 0.55, "threshold": 0.70}
    r = cluster(env.conn, env.cfg["id"], env.cfg)
    # the borderline join asked the model once, and that call is accounted
    assert r["llm_calls"] == 1 and _used(env, budget_mod.day_key(env.cfg)) == 1


def test_confirmer_budget_stops_confirmations(env):
    add_item(env, "i0", "A one", "alpha beta gamma", published=T0)
    add_item(env, "i1", "B two", "other words", published=T0 + DAY)
    daily = budget_mod.DailyBudget(env.conn, env.cfg["id"], {"llm": {"daily_max_calls": 1}})
    daily.count()  # some other command already spent today's call
    conf = Confirmer(FakeChat(rules=lambda p: True), env.conn,
                     _desc_fn(env.conn, env.cfg["id"]), confirm_low=0.55, budget=daily)
    assert conf.ask("i1", "i0", "i0") is False  # embeddings-only answer
    assert conf.budget_exhausted and conf.calls == 0


# ---------------------------------------------------------------- stats

def test_stats_shows_today_s_calls_and_budget(env, monkeypatch, capsys):
    import time as time_mod

    from gaceta import cli

    add_item(env, "a1", "A one", "alpha beta gamma", "noted", "public",
             int(time_mod.time()) - 3600)  # inside the 14-day window
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    cluster(env.conn, env.cfg["id"], env.cfg)
    env.conn.commit()
    env.cfg["llm"] = {"daily_max_calls": 5}  # what the write runs against …
    toml = env.data_dir / "readers" / env.cfg["id"] / "reader.toml"
    toml.write_text(toml.read_text() + "\n[llm]\ndaily_max_calls = 5\n")  # … and stats reads
    writeup.write_stories(env.conn, env.cfg["id"], env.cfg,
                          chat=FakeWriteup(GOOD), now=None)  # now=None → today, as stats counts
    assert _used(env, budget_mod.day_key(env.cfg)) == 1
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli.cmd_stats(argparse.Namespace(reader=env.cfg["id"]))
    out = capsys.readouterr().out
    assert rc == 0
    assert f"llm calls today ({budget_mod.day_key(env.cfg)})" in out
    assert "1/5" in out


def test_stats_without_a_limit(env, monkeypatch, capsys):
    from gaceta import cli

    add_item(env, "a1", "A one", "alpha beta gamma", published=T0)  # ensures the reader row
    env.conn.commit()
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli.cmd_stats(argparse.Namespace(reader=env.cfg["id"]))
    out = capsys.readouterr().out
    assert rc == 0 and "llm calls today" in out and "no daily limit" in out


# ---------------------------------------------------------------- migration

def test_llm_usage_migration_is_additive_and_idempotent(tmp_path):
    import sqlite3

    import gaceta.db as gdb

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    schema = (gdb.resources.files("gaceta") / "schema.sql").read_text()
    raw.executescript(schema.split("-- v12 (additive)")[0])  # a pre-v12 db
    raw.execute("INSERT INTO readers (id, name, langs, created_at) VALUES ('r', 'R', 'en', 0)")
    raw.commit()
    raw.close()

    conn = gdb.connect(path)
    gdb.init(conn)  # gains llm_usage
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "llm_usage" in tables
    conn.execute("INSERT INTO llm_usage (reader_id, day, calls) VALUES ('r', '2026-01-01', 3)")
    conn.commit()
    gdb.init(conn)  # idempotent
    assert conn.execute("SELECT calls FROM llm_usage").fetchone()[0] == 3
    # counting is an upsert on (reader, day)
    budget_mod.DailyBudget(conn, "r", {}).count()
    today = budget_mod.day_key({})
    assert conn.execute(
        "SELECT calls FROM llm_usage WHERE day = ?", (today,)).fetchone()[0] == 1
    conn.close()
