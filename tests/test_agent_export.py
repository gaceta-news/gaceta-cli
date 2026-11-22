"""Read-only public agent lookup (agent.py): public-only isolated exports a
versioned JSON, a bound reader, fail closed on private/stale/unknown content
or a foreign baseline, and reads that never write (no marks, no db access at
all). The citation contract rides in every payload. The demo fixture in
docs/agent/demo (a separate worker's deliverable) is exercised when present."""
from __future__ import annotations

import importlib.util
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from gaceta import agent as agent_mod
from gaceta.cli import main as cli_main
from gaceta.edition import build_edition

from test_cluster import DAY, T0, add_item
from test_edition_build import _edition_date
from test_readonly import tree_hashes

READER = "testreader"
CANARY = "zorblax-agent-private"
EDATE = _edition_date()
DEMO_SCRIPT = Path(__file__).resolve().parent.parent / "docs" / "agent" / "demo" / "make_demo_export.py"


# ------------------------------------------------------------------ fixture

def _story(env, sid, item_ids, title, privacy="public", summary=None,
           confidence=None, last_seen=T0 + 2 * DAY):
    env.conn.execute(
        "INSERT INTO stories (id, reader_id, title, summary, status, confidence, "
        "privacy, first_seen, last_seen, updated_at) "
        "VALUES (?, ?, ?, ?, 'settled', ?, ?, ?, ?, ?)",
        (sid, env.cfg["id"], title, summary, confidence, privacy, T0, last_seen, last_seen),
    )
    env.conn.executemany(
        "INSERT INTO story_items (story_id, item_id, role) VALUES (?, ?, ?)",
        [(sid, iid, "origin" if k == 0 else "update") for k, iid in enumerate(item_ids)],
    )


def _seed(env):
    """Three public stories (stA written-up with corrections+history, stB bare,
    stC newest), plus one household and one private canary story that a public
    edition physically cannot contain."""
    for iid, ts in (("ia1", T0), ("ia2", T0 + DAY), ("ib1", T0 + DAY),
                    ("ic1", T0 + 3 * DAY), ("ih1", T0), ("ip1", T0)):
        add_item(env, iid, f"Item {iid}", "plain body", "noted", "public", ts)
    for iid, url in (("ia1", "https://example.com/a"), ("ia2", "https://example.org/b"),
                     ("ib1", "https://example.net/c"), ("ic1", "https://example.net/d"),
                     ("ih1", "https://example.net/h"), ("ip1", "https://example.net/p")):
        env.conn.execute("UPDATE items SET url = ? WHERE id = ?", (url, iid))
    _story(env, "stA", ["ia1", "ia2"], "Story A", summary="A model write-up",
           confidence=0.8, last_seen=T0 + 2 * DAY)
    _story(env, "stB", ["ib1"], "Story B", last_seen=T0 + 3 * DAY)
    _story(env, "stC", ["ic1"], "Story C", last_seen=T0 + 4 * DAY)
    _story(env, "stH", ["ih1"], f"Household {CANARY}", privacy="household")
    _story(env, "stP", ["ip1"], f"Private {CANARY}", privacy="private")
    env.conn.execute(
        "INSERT INTO corrections (story_id, said, now, at) VALUES ('stA', ?, ?, ?)",
        ("old claim", "new wording", T0 + DAY))
    env.conn.executemany(
        "INSERT INTO story_versions (story_id, version, title, summary, confidence, "
        "model, written_at, members, what_changed) VALUES (?, ?, ?, ?, ?, 'fake', ?, ?, ?)",
        [("stA", 1, "Story A", "first write-up", 0.5, T0, 1, None),
         ("stA", 2, "Story A", "A model write-up", 0.8, T0 + 2 * DAY, 2,
          "second source joined")],
    )
    env.conn.commit()


def _build_public_edition(env):
    return build_edition(env.conn, READER, env.cfg, date_str=EDATE,
                          max_privacy="public", force=True, data_dir=env.data_dir)


def _export_cli(monkeypatch, env, capsys):
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli_main(["agent", "export", "--reader", READER, "--date", EDATE])
    caught = capsys.readouterr()
    return rc, caught.out, caught.err


def _read_cli(monkeypatch, capsys, *argv):
    rc = cli_main(["agent", *argv])
    caught = capsys.readouterr()
    return rc, caught.out, caught.err


def _current(env) -> dict:
    p = agent_mod.current_export_path(env.data_dir, READER)
    return json.loads(p.read_text(encoding="utf-8"))


def _tampered_copy(env, tmp_path, mutate) -> Path:
    export = _current(env)
    mutate(export)
    p = tmp_path / "tampered.json"
    p.write_text(json.dumps(export, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


# ------------------------------------------------------------------ export

def test_export_is_public_only_and_carries_the_contract(monkeypatch, env, capsys):
    _seed(env)
    _build_public_edition(env)
    rc, out, _err = _export_cli(monkeypatch, env, capsys)
    assert rc == 0 and "agent export reader=testreader" in out
    export = _current(env)
    # manifest: versioned, bound, public-only
    assert export["schema_version"] == 1
    assert export["kind"] == "gaceta.agent-export"
    assert export["reader"]["id"] == READER and export["reader"]["name"]
    assert export["edition"]["date"] == EDATE
    assert export["edition"]["stories_total"] == 3
    assert export["edition"]["folio_code"]
    assert export["privacy"] == {"max_level": "public"}
    assert "confirmed" in export["citation"]["policy"]
    assert export["citation"]["summary_origin"] == "model"
    # strictly the public stories, never the household/private ones
    assert set(export["stories"]) == {"stA", "stB", "stC"}
    assert CANARY not in json.dumps(export, ensure_ascii=False)
    # per story: bound to public, summary marked as model text, evidence intact
    a = export["stories"]["stA"]
    assert a["privacy"] == "public" and a["summary_written_by"] == "model"
    assert a["summary"] == "A model write-up" and a["confidence"] == 0.8
    assert a["corrections"] == [{"said": "old claim", "now": "new wording",
                                 "at": a["corrections"][0]["at"]}]
    assert [h["what_changed"] for h in a["history"]] == [None, "second source joined"]
    assert a["sources"][0]["url"] == "https://example.com/a"
    # source trace stamps: 'added' (when the paper ingested it) is an ISO
    # trace time, never the outlet's publication time and never reading history
    assert all(set(s) == {"title", "url", "author", "kind", "published", "role",
                          "added"} for s in a["sources"])
    assert all(a["sources"][i]["added"] <= a["sources"][i + 1]["added"]
               for i in range(len(a["sources"]) - 1))
    # version evidence rides along from the real tables
    assert a["current_version"] == 2
    assert [u["version"] for u in a["versions"]] == [1, 2]
    assert [u["what_changed"] for u in a["versions"]] == [None, "second source joined"]
    assert all(set(u) == {"version", "written_at", "what_changed", "support"}
               for u in a["versions"])
    # the public build trace, this reader's public editions only
    assert [e["date"] for e in export["editions"]] == [EDATE]
    assert export["stories"]["stB"]["summary"] is None
    assert export["stories"]["stB"]["current_version"] is None
    assert "versions" not in export["stories"]["stB"]
    # nothing private, nothing reader-internal, rides along
    for story in export["stories"].values():
        assert "why_it_matters" not in story
    assert set(export) == {"schema_version", "kind", "reader", "edition",
                           "privacy", "citation", "stories", "editions"}


def test_export_refuses_when_no_public_edition_exists(monkeypatch, env, capsys):
    _seed(env)  # stories exist, but only a private edition was ever built
    build_edition(env.conn, READER, env.cfg, date_str=EDATE, max_privacy="private",
                  force=True, data_dir=env.data_dir)
    rc, out, err = _export_cli(monkeypatch, env, capsys)
    assert rc == 2 and out == ""
    assert "no public edition snapshot" in err
    assert "gaceta edition --reader testreader --max-privacy public" in err
    assert not agent_mod.export_dir(env.data_dir, READER).exists()


def test_build_export_refuses_nonpublic_or_broken_snapshots():
    def snapshot(story, max_privacy="public"):
        return {"version": 1, "max_privacy": max_privacy, "date": "2026-11-19",
                "built_at": "2026-11-19T08:00:00+00:00", "folio": {"code": "ABC123"},
                "stories": {"s1": story}}
    base = {"title": "t", "status": "settled", "privacy": "public", "summary": None,
            "first_seen": None, "last_seen": None,
            "sources": [{"title": None, "url": "https://x.example/a",
                         "author": None, "kind": "rss", "published": None,
                         "role": "origin"}]}
    with pytest.raises(agent_mod.ExportError, match="non-public edition snapshot"):
        agent_mod.build_export(snapshot(base, max_privacy="private"), READER, "R")
    with pytest.raises(agent_mod.ExportError, match="unsupported edition snapshot version"):
        agent_mod.build_export(snapshot(base) | {"version": 2}, READER, "R")
    household = snapshot(base | {"privacy": "household"})
    with pytest.raises(agent_mod.ExportError, match="not public"):
        agent_mod.build_export(household, READER, "R")
    why = snapshot(base | {"why_it_matters": "reader-private angle"})
    with pytest.raises(agent_mod.ExportError, match="why_it_matters"):
        agent_mod.build_export(why, READER, "R")
    bad_url = snapshot(base | {"sources": [{"url": "file:///etc/passwd"}]})
    with pytest.raises(agent_mod.ExportError, match="non-http"):
        agent_mod.build_export(bad_url, READER, "R")
    with pytest.raises(agent_mod.ExportError, match="bad reader id"):
        agent_mod.build_export(snapshot(base), "../evil", "R")


# ------------------------------------------------------------------ read

def _seeded_export(monkeypatch, env, capsys):
    _seed(env)
    _build_public_edition(env)
    rc, _out, _err = _export_cli(monkeypatch, env, capsys)
    assert rc == 0


def test_agent_edition_lists_public_stories(monkeypatch, env, capsys):
    _seeded_export(monkeypatch, env, capsys)
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", READER)
    assert (rc, err) == (0, "")
    listing = json.loads(out)
    assert listing["reader"]["id"] == READER
    assert listing["edition"]["stories_total"] == 3
    assert listing["privacy"]["max_level"] == "public"
    assert listing["citation"]["policy"]
    # newest last_seen first
    assert [s["id"] for s in listing["stories"]] == ["stC", "stB", "stA"]
    row = next(s for s in listing["stories"] if s["id"] == "stA")
    assert row["title"] == "Story A" and row["sources"] == 2 and row["corrections"] == 1
    assert row["version"] == 2  # the memory-update loop: the caller's new baseline
    assert set(row) == {"id", "title", "status", "version", "sources",
                        "corrections", "last_seen"}


def test_agent_story_returns_evidence_and_citation_contract(monkeypatch, env, capsys):
    _seeded_export(monkeypatch, env, capsys)
    rc, out, err = _read_cli(monkeypatch, capsys, "story", "--reader", READER,
                             "--id", "stA")
    assert (rc, err) == (0, "")
    got = json.loads(out)
    story = got["story"]
    assert story["id"] == "stA" and story["summary_written_by"] == "model"
    assert story["sources"][0]["url"] == "https://example.com/a"
    assert story["corrections"][0]["said"] == "old claim"
    assert story["history"][-1]["what_changed"] == "second source joined"
    assert got["edition"]["date"] == EDATE
    assert "confirmed" in got["citation"]["policy"]


def test_reads_never_write_and_never_need_the_db(monkeypatch, env, capsys, tmp_path):
    _seeded_export(monkeypatch, env, capsys)
    before = tree_hashes(env.data_dir)
    counts = {t: env.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("seen", "reactions", "agent_sends", "annotations")}
    assert _read_cli(monkeypatch, capsys, "edition", "--reader", READER)[0] == 0
    assert _read_cli(monkeypatch, capsys, "story", "--reader", READER,
                     "--id", "stA")[0] == 0
    # the launch brief is a read too — with no baseline (first launch)
    assert _read_cli(monkeypatch, capsys, "since", "--reader", READER)[0] == 0
    assert before == tree_hashes(env.data_dir)  # no marks, no writes, nothing
    for t, n in counts.items():
        assert env.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n
    # strongest form: a sandbox holding ONLY the export still serves reads —
    # the agent side has no database or reader config to reach for
    sandbox = tmp_path / "sandbox"
    shutil.copytree(env.data_dir / "agent-exports", sandbox / "agent-exports")
    monkeypatch.setenv("GACETA_DATA", str(sandbox))
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", READER)
    assert rc == 0 and json.loads(out)["reader"]["id"] == READER
    rc, out, _ = _read_cli(monkeypatch, capsys, "story", "--reader", READER, "--id", "stB")
    assert rc == 0 and json.loads(out)["story"]["id"] == "stB"


# ------------------------------------------------------------------ fail closed

def test_fail_closed_on_unknown_baseline(monkeypatch, env, capsys, tmp_path):
    _seeded_export(monkeypatch, env, capsys)
    for mutate, word in (
        (lambda e: e.update(schema_version=2), "unknown export schema version"),
        (lambda e: e.update(schema_version="1"), "unknown export schema version"),
        (lambda e: e.update(kind="gaceta.other"), "kind mismatch"),
        (lambda e: e.pop("citation"), "citation policy"),
        (lambda e: e.pop("stories"), "no stories object"),
    ):
        p = _tampered_copy(env, tmp_path, mutate)
        rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", READER,
                                 "--path", str(p))
        assert rc == 2 and out == "", word
        assert word in err


def test_fail_closed_on_reader_mismatch(monkeypatch, env, capsys):
    _seeded_export(monkeypatch, env, capsys)
    # a foreign reader pointed at the real export: the manifest binds it
    current = agent_mod.current_export_path(env.data_dir, READER)
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", "someoneelse",
                             "--path", str(current))
    assert rc == 2 and out == ""
    assert "bound to reader 'testreader', not 'someoneelse'" in err
    # without --path the default path is per-reader: a foreign reader finds
    # nothing there and still fails closed
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", "someoneelse")
    assert rc == 2 and out == "" and "no agent export" in err
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", "../evil")
    assert rc == 2 and "bad reader id" in err


def test_fail_closed_on_private_content(monkeypatch, env, capsys, tmp_path):
    _seeded_export(monkeypatch, env, capsys)
    for mutate, word in (
        (lambda e: e["stories"]["stA"].update(privacy="private"),
         "not public; refusing the whole export"),
        (lambda e: e["stories"]["stA"].update(why_it_matters="private angle"),
         "why_it_matters"),
        (lambda e: e["stories"]["stA"]["sources"].append({"url": "file:///etc/passwd"}),
         "non-http(s) source url"),
        (lambda e: e.update(privacy={"max_level": "household"}),
         "not public-only"),
    ):
        p = _tampered_copy(env, tmp_path, mutate)
        for action in (["edition", "--reader", READER, "--path", str(p)],
                       ["story", "--reader", READER, "--id", "stA", "--path", str(p)]):
            rc, out, err = _read_cli(monkeypatch, capsys, *action)
            assert rc == 2 and out == "", word
            assert word in err


def test_fail_closed_on_stale_or_future_export(monkeypatch, env, capsys, tmp_path):
    _seeded_export(monkeypatch, env, capsys)
    now = datetime.now().astimezone()

    def stale(e):
        e["edition"]["built_at"] = (now - timedelta(hours=72)).isoformat(timespec="seconds")

    p = _tampered_copy(env, tmp_path, stale)
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", READER,
                             "--path", str(p))
    assert rc == 2 and out == "" and "stale" in err
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", READER,
                             "--path", str(p), "--max-age-hours", "96")
    assert rc == 0 and json.loads(out)["edition"]["stories_total"] == 3

    def future(e):
        e["edition"]["built_at"] = (now + timedelta(hours=3)).isoformat(timespec="seconds")

    p = _tampered_copy(env, tmp_path, future)
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", READER,
                             "--path", str(p), "--max-age-hours", "96")
    assert rc == 2 and out == "" and "future" in err
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", READER,
                             "--path", str(p), "--max-age-hours", "0")
    assert rc == 2 and "positive number" in err


def test_fail_closed_on_missing_export_and_unknown_story(monkeypatch, env, capsys, tmp_path):
    _seeded_export(monkeypatch, env, capsys)
    missing = tmp_path / "nope.json"
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", READER,
                             "--path", str(missing))
    assert rc == 2 and out == "" and "no agent export" in err
    rc, out, err = _read_cli(monkeypatch, capsys, "story", "--reader", READER,
                             "--id", "missing-story")
    assert rc == 2 and out == "" and "unknown story id 'missing-story'" in err
    assert "agent edition" in err  # the way forward is in the error


# ------------------------------------------------------------------ demo fixture
# owned by a separate worker; exercised here so it can never drift from the
# reader's schema

@pytest.mark.skipif(not DEMO_SCRIPT.is_file(),
                    reason="demo fixture (separate worker) not present")
def test_demo_fixture_reads(monkeypatch, capsys, tmp_path):
    spec = importlib.util.spec_from_file_location("make_demo_export", DEMO_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out_path = Path(mod.build(tmp_path))
    monkeypatch.setenv("GACETA_DATA", str(tmp_path))  # no db, no reader config
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", "demo",
                             "--path", str(out_path))
    assert rc == 0, err
    listing = json.loads(out)
    assert listing["reader"]["id"] == "demo"
    assert listing["edition"]["stories_total"] == len(listing["stories"]) == 3
    sid = listing["stories"][0]["id"]
    rc, out, err = _read_cli(monkeypatch, capsys, "story", "--reader", "demo",
                             "--id", sid, "--path", str(out_path))
    assert rc == 0, err
    got = json.loads(out)
    assert got["story"]["id"] == sid
    assert got["citation"]["policy"]


@pytest.mark.skipif(not DEMO_SCRIPT.is_file(),
                    reason="demo fixture (separate worker) not present")
def test_demo_fixture_launch_degrades_to_explicit_unknowns(monkeypatch, capsys, tmp_path):
    """The demo export predates version evidence: the launch brief must still
    answer, grading from the history block it does carry and saying 'unknown'
    wherever the evidence is missing — never a guess."""
    spec = importlib.util.spec_from_file_location("make_demo_export", DEMO_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out_path = Path(mod.build(tmp_path))
    monkeypatch.setenv("GACETA_DATA", str(tmp_path))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(
        {"reader": "demo", "versions": {"demo-trail": 1}}), encoding="utf-8")
    rc, out, err = _read_cli(monkeypatch, capsys, "since", "--reader", "demo",
                              "--baseline", str(baseline), "--path", str(out_path))
    assert rc == 0, err
    got = json.loads(out)
    by_id = {u["story_id"]: u for u in got["updates"]}
    # trail carries a history block: known baseline at version 1, and the
    # material note of version 2 rides with the story's sources as references
    trail = by_id["demo-trail"]
    assert trail["baseline"]["status"] == "known"
    assert trail["change"]["status"] == "material_change"
    proposal = trail["change"]["proposals"][0]
    assert proposal["support_kind"] == "story_sources"
    assert all("excerpt" not in s for s in proposal["support"])
    # the other two have no version evidence at all and no baseline claim:
    # explicit unknowns, never a guess
    for sid in ("demo-aurora", "demo-quiet"):
        u = by_id[sid]
        assert u["baseline"]["status"] == "none_stated"
        assert u["change"]["status"] == "unknown"
    # no edition date stated → intervening editions is an explicit unknown
    assert got["intervening_editions"] is None and "unknown" in got["intervening_editions_note"]
    assert got["semantics"]["comparisons"]
    assert got["approval"]["state"] == "proposal"
