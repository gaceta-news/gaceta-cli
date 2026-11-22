"""Beta story regression tests: the offline two-edition demo (fixtures/
make_two_edition_demo.py) drives the whole pipeline twice with synthetic
content, and these tests pin the editor's evidence and 'seen' contract:

  stable story        unchanged between editions: no rewrite, no LLM call,
                      same rev → never resurfaces for the reader who saw it
  approval vs proposal  the primary-source verbatim quote survives the
                      evidence gate; an invented quote is unquoted; a proposal
                      stays worded as a proposal and less confident
  repeat coverage     a new member and a rewritten card, but what_changed ''
                      — the paper claims no development that isn't one
  unknown baseline    a never-seen story still archives its change note;
                      resurfaced stories are explained first; a version whose
                      previous card is missing is skipped, not invented
  skipped edition     the middle day has no snapshot; both snapshots are
                      immutable; day-1 stories stay in edition 2's window

Everything the demo produces is synthetic and marked as such (ids, URLs,
outlets, authors, bodies, masthead, cards). No network: a test forbids
urllib outright. Deterministic: a test builds the demo twice and compares
artifacts byte for byte.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import urllib.request

import pytest

import make_two_edition_demo as demo
from gaceta import config as gconfig, db as gdb
from gaceta.editor import quotes
from gaceta.editor.changes import note_changes
from gaceta.edition import build_edition, snapshot_path
from gaceta.render import html as render_html


@pytest.fixture
def made(tmp_path):
    """A completed two-edition demo: (result, root)."""
    root = tmp_path / "demo"
    return demo.run_demo(root), root


def _conn(root):
    conn = gdb.connect(pathlib.Path(root) / "data" / "gaceta.db")
    return conn


def _cfg(root):
    return gconfig.load_reader(demo.READER_ID, pathlib.Path(root) / "data")


def _story_texts(root, story_id):
    """(title + body) of every item of the story, as the evidence gate sees."""
    conn = _conn(root)
    try:
        return [f"{r['title'] or ''} {r['body'] or ''}" for r in conn.execute(
            "SELECT i.title, i.body FROM story_items si JOIN items i ON i.id = si.item_id "
            "WHERE si.story_id = ? ORDER BY i.id", (story_id,))]
    finally:
        conn.close()


# ------------------------------------------------------------ the two editions

def test_two_editions_and_a_skipped_middle_day(made):
    result, root = made
    m = result["manifest"]
    t = m["timeline"]
    assert t["edition1"] == "2026-11-16" and t["edition2"] == "2026-11-18"
    assert t["skipped"] == "2026-11-17"

    conn = _conn(root)
    try:
        dates = {r["date"] for r in conn.execute(
            "SELECT date FROM editions WHERE reader_id = ?", (demo.READER_ID,))}
    finally:
        conn.close()
    assert dates == {t["edition1"], t["edition2"]}, "the skipped day gets no edition row"
    for day in (t["edition1"], t["edition2"]):
        assert (root / m["artifacts"]["editions"][day]).exists()
        assert (root / m["artifacts"]["html"][day]).exists()
        doc = (root / m["artifacts"]["html"][day]).read_text()
        assert m["reader"]["masthead"] in doc
    assert not (root / "data" / "editions" / demo.READER_ID /
                f"{t['skipped']}-{demo.MAX_PRIVACY}.json").exists()
    # distinct papers: the folio fingerprint follows the member set
    assert (m["editions"][t["edition1"]]["folio"]["code"]
            != m["editions"][t["edition2"]]["folio"]["code"])


def test_demo_artifacts_are_deterministic(tmp_path):
    r1 = demo.run_demo(tmp_path / "one")["manifest"]
    r2 = demo.run_demo(tmp_path / "two")["manifest"]
    assert r1 == r2, "same synthetic input must produce the same manifest"
    for rel in (*r1["artifacts"]["editions"].values(), *r1["artifacts"]["html"].values()):
        assert (tmp_path / "one" / rel).read_bytes() == (tmp_path / "two" / rel).read_bytes(), rel


# --------------------------------------------------------------- stable story

def test_stable_story_costs_nothing_and_never_resurfaces(made):
    result, root = made
    m = result["manifest"]
    stable = m["stories"]["stable"]
    sid = stable["story"]

    # written once, never again: one version, no day-2 LLM call
    assert stable["versions"] == 1
    assert not any("[demo:estable]" in p for p in result["passes"]["write2"]), \
        "the stable story must not be rewritten on the second pass"
    assert m["runs"]["write2"]["written"] == 3  # repeat coverage, proposal move, fresh story

    # same rev in both editions and the same rev the reader marked seen:
    # equality is what keeps a seen story hidden (it never resurfaces)
    assert stable["rev_edition1"] == stable["rev_edition2"]
    conn = _conn(root)
    try:
        row = conn.execute(
            "SELECT rev FROM seen WHERE by_name = ? AND story_id = ?",
            (demo.READER_BY, sid)).fetchone()
        assert row["rev"] == stable["rev_edition2"]
        assert conn.execute(
            "SELECT COUNT(*) FROM story_versions WHERE story_id = ?", (sid,)
        ).fetchone()[0] == 1
    finally:
        conn.close()

    # the skipped day must not drop it from edition 2
    assert sid in m["editions"][m["timeline"]["edition2"]]["revs"]


# ------------------------------------------------------- approval vs proposal

def test_approval_keeps_the_primary_source_quote_and_unquotes_the_invented(made):
    result, root = made
    m = result["manifest"]
    apv = m["stories"]["approval"]
    v1 = m["cards"][apv["story"]][0]

    assert f"\u201c{demo.APPROVAL_QUOTE}\u201d" in v1["summary"], \
        "the verbatim span from the primary source keeps its quotation marks"
    assert f"\u201c{demo.INVENTED_QUOTE}\u201d" not in v1["summary"]
    assert demo.INVENTED_QUOTE in v1["summary"], \
        "the invented span survives only as unquoted paraphrase"
    assert m["runs"]["write1"]["quotes_unquoted"] == 1, "exactly the invented quote is stripped"
    # the stored card is clean: re-running the gate changes nothing
    out, removed = quotes.enforce(v1["summary"], _story_texts(root, apv["story"]))
    assert removed == 0 and out == v1["summary"]
    # the primary source (the council's own outlet) is linked under the card
    ed1 = json.loads((root / m["artifacts"]["editions"][m["timeline"]["edition1"]]
                      ).read_text())
    authors = [s["author"] for s in ed1["stories"][apv["story"]]["sources"]]
    assert "Mesa del Concejo (demo)" in authors


def test_proposal_stays_a_proposal_and_less_confident(made):
    result, root = made
    m = result["manifest"]
    prop = m["stories"]["proposal"]
    apv = m["stories"]["approval"]
    v1 = m["cards"][prop["story"]][0]

    assert "propuso" in v1["summary"]
    assert "aprob" not in (v1["summary"] + v1["title"]).casefold(), \
        "a proposal must never be reported as an approval"
    assert v1["confidence"] < m["cards"][apv["story"]][0]["confidence"], \
        "the approval backed by the primary source is the more confident card"
    assert m["stories"]["proposal"]["confidence_version1"] == 0.55


# ------------------------------------------------------------ repeat coverage

def test_repeat_coverage_grows_the_story_but_claims_no_change(made):
    result, root = made
    m = result["manifest"]
    rep = m["stories"]["repeat_coverage"]
    sid = rep["story"]
    versions = m["cards"][sid]

    # a second outlet joined and the card was rewritten…
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[1]["members"] == 2
    assert m["runs"]["write2"]["llm_calls"] == 3  # the rewrite was a real (fake) call
    assert m["runs"]["changes2"] == {"candidates": 2, "noted": 1, "unchanged": 1,
                                     "failed": 0, "skipped": 0, "llm_calls": 2,
                                     "llm_cache_hits": 0, "budget_exhausted": False}
    # …but the member growth moved the rev (it resurfaces for the reader)…
    ed1 = m["editions"][m["timeline"]["edition1"]]["revs"][sid]
    ed2 = m["editions"][m["timeline"]["edition2"]]["revs"][sid]
    assert ed1 != ed2
    # …while the change pass records that nothing material changed
    assert versions[1]["what_changed"] == ""
    conn = _conn(root)
    try:
        row = conn.execute("SELECT COUNT(*) FROM story_versions").fetchone()
        assert row[0] == 6, "the changes pass must never add or rewrite versions"
    finally:
        conn.close()

    # on the page: the 'since' block ships, but no bullet claims a development
    ed = json.loads((root / m["artifacts"]["editions"][m["timeline"]["edition2"]]
                     ).read_text())
    html = render_html._render_story(sid, ed["stories"][sid])
    assert '<div class="since"' in html
    assert '<li data-at=' not in html


# ---------------------------------------------------------- unknown baseline

def test_unknown_baseline_change_archived_resurfaced_explained_first(made):
    result, root = made
    m = result["manifest"]
    unk = m["stories"]["unknown_baseline"]
    sid = unk["story"]

    # the reader never marked this story seen: their baseline is unknown…
    conn = _conn(root)
    try:
        seen = {r["story_id"] for r in conn.execute(
            "SELECT story_id FROM seen WHERE by_name = ?", (demo.READER_BY,))}
    finally:
        conn.close()
    assert sid not in seen
    # …yet its change note is still archived for the story's history
    assert m["cards"][sid][1]["what_changed"] == demo.PRESUPUESTO_CHANGE

    # and the change notes were explained in the right order: the story the
    # reader had seen (resurfaced) first, the never-seen one second
    markers = [demo.DemoEditor()._marker(p) for p in result["passes"]["changes2"]]
    assert markers == ["repeticion", "propuesta-avance"]

    # the never-seen story's change renders as an ordinary 'since' bullet
    ed = json.loads((root / m["artifacts"]["editions"][m["timeline"]["edition2"]]
                     ).read_text())
    html = render_html._render_story(sid, ed["stories"][sid])
    assert f'<li data-at="{demo.NOW2}">{demo.PRESUPUESTO_CHANGE}' in html


def test_missing_baseline_version_is_skipped_not_invented(made, tmp_path):
    """A version >= 2 whose previous card is gone (unknown baseline) is left
    unexplained rather than compared against nothing."""
    _, root = made
    conn = _conn(root)
    try:
        sid = json.loads((root / "demo-manifest.json").read_text())["stories"]["proposal"]["story"]
        conn.execute("DELETE FROM story_versions WHERE story_id = ? AND version = 1", (sid,))
        conn.execute("UPDATE story_versions SET what_changed = NULL "
                     "WHERE story_id = ? AND version = 2", (sid,))
        conn.commit()
        chat = demo.DemoEditor()
        r = note_changes(conn, demo.READER_ID, _cfg(root), chat=chat, now=demo.NOW2)
        assert r["candidates"] == 1 and r["llm_calls"] == 0 and chat.prompts == []
        assert conn.execute(
            "SELECT what_changed FROM story_versions WHERE story_id = ? AND version = 2",
            (sid,)).fetchone()[0] is None, "no change note may be invented"
    finally:
        conn.close()


# --------------------------------------------------------- skipped edition …

def test_edition_snapshots_are_immutable(made):
    _, root = made
    m = json.loads((root / "demo-manifest.json").read_text())
    p1 = snapshot_path(pathlib.Path(root) / "data", demo.READER_ID,
                       m["timeline"]["edition1"], demo.MAX_PRIVACY)
    first = p1.read_bytes()
    conn = _conn(root)
    try:
        with pytest.raises(FileExistsError):
            build_edition(conn, demo.READER_ID, _cfg(root),
                          date_str=m["timeline"]["edition1"], max_privacy=demo.MAX_PRIVACY,
                          data_dir=pathlib.Path(root) / "data", now=demo.NOW1 + 60)
        assert p1.read_bytes() == first, "the refused rebuild must not touch the snapshot"
        # --force is the documented way to replace the same day's snapshot
        build_edition(conn, demo.READER_ID, _cfg(root),
                      date_str=m["timeline"]["edition1"], max_privacy=demo.MAX_PRIVACY,
                      force=True, data_dir=pathlib.Path(root) / "data", now=demo.NOW1)
        assert p1.exists()
        n = conn.execute("SELECT COUNT(*) FROM editions WHERE reader_id = ?",
                         (demo.READER_ID,)).fetchone()[0]
        assert n == 2, "a forced rebuild replaces its own row, never adds one"
    finally:
        conn.close()


# ------------------------------------------------------------- synthetic-only

def test_every_piece_of_demo_content_is_marked_synthetic(made):
    _, root = made
    m = json.loads((root / "demo-manifest.json").read_text())
    assert m["synthetic"] is True

    # the collector's items: ids, urls, outlets, authors, bodies
    src = sqlite3.connect(f"file:{pathlib.Path(root) / 'collector' / 'items.db'}?mode=ro",
                           uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute("SELECT * FROM items ORDER BY id").fetchall()
        assert len(rows) == 6
        for r in rows:
            assert r["id"].startswith("syn:")
            assert r["url"].startswith("https://example.com/synthetic-demo/")
            assert r["source"].endswith("-demo")
            assert r["author"].endswith("(demo)")
            assert "[synthetic demo]" in r["body"] and "[demo:" in r["body"]
    finally:
        src.close()

    # the ingested copy keeps the ids; sources carry the demo suffix
    conn = _conn(root)
    try:
        assert conn.execute("SELECT COUNT(*) FROM items WHERE id NOT LIKE 'syn:%'"
                            ).fetchone()[0] == 0
        for (sid,) in conn.execute("SELECT id FROM sources"):
            assert sid.endswith("-demo"), sid
        # every card's why_it_matters says the card is synthetic…
        for row in conn.execute("SELECT why_it_matters FROM stories"):
            assert "demostración" in row["why_it_matters"]
    finally:
        conn.close()

    # …the masthead says so, and it is what the paper prints
    assert "Sintética" in m["reader"]["masthead"]
    for day in (m["timeline"]["edition1"], m["timeline"]["edition2"]):
        ed = json.loads((root / m["artifacts"]["editions"][day]).read_text())
        assert ed["reader"]["masthead"] == m["reader"]["masthead"]
        doc = (root / m["artifacts"]["html"][day]).read_text()
        assert m["reader"]["masthead"] in doc


def test_demo_runs_with_network_calls_forbidden(made, tmp_path, monkeypatch):
    """Offline by construction: the whole two-edition build never opens a
    socket — any attempted HTTP request fails the test."""

    def no_network(*a, **kw):
        raise AssertionError("the offline demo must never touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    demo.run_demo(tmp_path / "offline")
    assert (tmp_path / "offline" / "demo-manifest.json").exists()
