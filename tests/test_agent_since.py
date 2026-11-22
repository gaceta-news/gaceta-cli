"""The launch brief (`gaceta agent since`): known prior reader/story version vs
unknown, skipped editions, material change with supporting public source
excerpt/reference vs no supported update — duplicate coverage distinguished
from missing evidence, evidence-supported proposals flowing to the reader's
approval (the caller's own baseline update). Publication timestamps are never
confused with reading history; no other reader, private excerpt or raw mark
ever rides along. Everything runs the real export path (build_edition →
agent export) on the synthetic DB.
"""
from __future__ import annotations

import io
import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from gaceta import agent as agent_mod
from gaceta import config as gconfig
from gaceta.cli import main as cli_main
from gaceta.edition import build_edition

from test_agent_export import READER, _read_cli, _tampered_copy
from test_cluster import DAY, T0, add_item
from test_readonly import tree_hashes

H = 3600
HOUSE_CANARY = "zorblax-house-since"
PRIV_CANARY = "zorblax-priv-since"
OTHER_CANARY = "OTHER-PAPER-CANARY"
SEEN_REV_A = "aaa111222333444"
SEEN_REV_B = "bbb222333444555"

# Claim-shaped phrasings the brief must never contain, in any story block,
# note or reason: wording that asserts the reader's reading history (dates,
# non-reading, delivery or access). Explicit DENIALS ("never proves they
# were skipped or unread") and explicit unknowns stay allowed — the rubric
# bans claims, not the words 'skipped'/'unread' inside a denial.
CLAIM_PHRASES = (
    "you skipped", "you missed", "you did not read", "you didn't read",
    "you haven't read", "you have not read", "you never read",
    "you did not see", "you haven't seen", "you have not seen",
    "since you read", "you last read", "you read on", "you read it",
    "no record of you reading", "unread by you", "you did not open",
    "your subscription", "you received", "you have access",
    "you had access", "you did not receive", "you were not sent",
)


def assert_no_reading_claims(text: str) -> None:
    low = text.lower()
    for phrase in CLAIM_PHRASES:
        assert phrase not in low, f"brief claims reading history: {phrase!r}"


def _day(n: int) -> str:
    return datetime.fromtimestamp(T0 + n * DAY, tz=timezone.utc).date().isoformat()


D1, D2, D3 = _day(5), _day(6), _day(7)


# ------------------------------------------------------------------ helpers

def _item(env, iid, title, body, *, privacy="public", published, ingested, url):
    add_item(env, iid, title, body, "noted", privacy, published)
    env.conn.execute("UPDATE items SET url = ?, ingested_at = ? WHERE id = ?",
                     (url, ingested, iid))


def _story(env, sid, item_ids, title, *, privacy="public",
           first_seen=T0, last_seen=T0 + 2 * DAY):
    env.conn.execute(
        "INSERT INTO stories (id, reader_id, title, summary, status, confidence, "
        "privacy, first_seen, last_seen, updated_at) "
        "VALUES (?, ?, ?, NULL, 'settled', NULL, ?, ?, ?, ?)",
        (sid, READER, title, privacy, first_seen, last_seen, last_seen))
    env.conn.executemany(
        "INSERT INTO story_items (story_id, item_id, role) VALUES (?, ?, ?)",
        [(sid, iid, "origin" if k == 0 else "update")
         for k, iid in enumerate(item_ids)])


def _version(env, sid, version, written_at, members, what_changed):
    env.conn.execute(
        "INSERT INTO story_versions (story_id, version, summary, model, "
        "written_at, members, what_changed) VALUES (?, ?, ?, 'fake', ?, ?, ?)",
        (sid, version, f"write-up v{version}", written_at, members, what_changed))


def _build_edition(env, day, *, reader=READER, cfg=None, max_privacy="public"):
    return build_edition(env.conn, reader, cfg or env.cfg, date_str=day,
                         max_privacy=max_privacy, force=False,
                         data_dir=env.data_dir)


def _export(monkeypatch, env, capsys, day):
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli_main(["agent", "export", "--reader", READER, "--date", day])
    caught = capsys.readouterr()
    assert rc == 0, caught.err
    p = agent_mod.export_dir(env.data_dir, READER) / f"{day}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _since(monkeypatch, capsys, env, tmp_path, baseline):
    """Run the launch brief; the baseline file is the CALLER's memory and
    lives in the caller's sandbox, never in the data dir."""
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    argv = ["agent", "since", "--reader", READER]
    if baseline is not None:
        p = tmp_path / "baseline.json"
        p.write_text(json.dumps(baseline), encoding="utf-8")
        argv += ["--baseline", str(p)]
    rc = cli_main(argv)
    caught = capsys.readouterr()
    return rc, caught.out, caught.err


def _seed_canaries(env):
    _item(env, "h1", f"Household {HOUSE_CANARY}", "household body",
          privacy="household", published=T0, ingested=T0, url="https://h.example/h")
    _story(env, "stH", ["h1"], f"Household {HOUSE_CANARY}", privacy="household")
    _item(env, "p1", f"Private {PRIV_CANARY}", "private body",
          privacy="private", published=T0, ingested=T0, url="https://p.example/p")
    _story(env, "stP", ["p1"], f"Private {PRIV_CANARY}", privacy="private")


def _seed_baseline_state(env):
    """Five public stories with a v1 write-up each, as of edition D1."""
    _item(env, "o1", "Bridge inspection due", "The valley council said the "
          "bridge inspection report would be published in March.",
          published=T0 - 30 * DAY, ingested=T0, url="https://example.com/old")
    _story(env, "stOld", ["o1"], "Story Old")
    _version(env, "stOld", 1, T0 + DAY, 1, "First write-up from the inspection note.")

    _item(env, "s1", "Mill festival", "The mill festival returns on the first "
          "weekend of June.", published=T0 + 600, ingested=T0 + 600,
          url="https://example.com/same")
    _item(env, "s2", "Future-dated notice", "A future-dated notice with no "
          "bearing on the timeline.", published=T0 + 20 * DAY,
          ingested=T0 + 1200, url="https://example.com/future")
    _story(env, "stSame", ["s1", "s2"], "Story Same")
    _version(env, "stSame", 1, T0 + 2 * DAY, 2, None)

    _item(env, "d1", "One load limit", "One load limit is planned for the "
          "county road.", published=T0 + 600, ingested=T0 + 600,
          url="https://example.com/dup1")
    _story(env, "stDup", ["d1"], "Story Dup")
    _version(env, "stDup", 1, T0 + DAY, 1, None)

    _item(env, "m1", "Register office move", "The register office will move "
          "downtown.", published=T0 + 600, ingested=T0 + 600,
          url="https://example.com/miss1")
    _story(env, "stMiss", ["m1"], "Story Miss")
    _version(env, "stMiss", 1, T0 + DAY, 1, None)

    _item(env, "f1", "Co-op winter catalog", "The co-op opened its winter "
          "catalog.", published=T0 + 600, ingested=T0 + 600,
          url="https://example.com/drift1")
    _story(env, "stDrift", ["f1"], "Story Drift")
    _version(env, "stDrift", 1, T0 + DAY, 1, None)
    _seed_canaries(env)
    env.conn.commit()


def _join(env, sid, iid, title, body, *, published, ingested, url):
    _item(env, iid, title, body, published=published, ingested=ingested, url=url)
    env.conn.execute("INSERT INTO story_items (story_id, item_id, role) "
                     "VALUES (?, ?, 'update')", (sid, iid))


def _post_baseline_events(env):
    """Everything the paper saw after D1: a material change (new source +
    rewrite + correction), assessed duplicate coverage, an unassessed
    rewrite, unassessed coverage, and one brand-new story."""
    _join(env, "stOld", "o2", "Engineer confirms two limits",
          "The county engineer confirms the bridge report will name two new "
          "load limits from April.",
          published=T0 - 10 * DAY, ingested=T0 + 5 * DAY + H,
          url="https://example.net/confirm")
    _version(env, "stOld", 2, T0 + 5 * DAY + 2 * H, 2,
             "A second source confirms the April limits.")
    env.conn.execute(
        "INSERT INTO corrections (story_id, said, now, at, evidence_item_id) "
        "VALUES ('stOld', 'one new load limit', 'two new load limits', ?, 'o2')",
        (T0 + 5 * DAY + 3 * H,))

    _join(env, "stDup", "d2", "Second outlet repeats figure",
          "A second outlet repeats the same road figure, adding no new detail.",
          published=T0 - 10 * DAY, ingested=T0 + 5 * DAY + H,
          url="https://example.net/dup2")
    _version(env, "stDup", 2, T0 + 5 * DAY + 2 * H, 2, "")  # assessed: duplicate

    _join(env, "stMiss", "m2", "Move delayed a week",
          "The register office move was delayed a week.",
          published=T0 - 5 * DAY, ingested=T0 + 5 * DAY + H,
          url="https://example.net/miss2")
    _version(env, "stMiss", 2, T0 + 5 * DAY + 2 * H, 2, None)  # never assessed

    _join(env, "stDrift", "f2", "New county filing",
          "A new filing appeared in the county register for the co-op.",
          published=T0 - 2 * DAY, ingested=T0 + 5 * DAY + H,
          url="https://example.org/drift2")  # no rewrite: unassessed coverage

    _item(env, "n1", "Winter ferry timetable", "The harbor office posted the "
          "winter ferry timetable.", published=T0 + 6 * DAY, ingested=T0 + 6 * DAY,
          url="https://example.com/new")
    _story(env, "stNew", ["n1"], "Story New", first_seen=T0 + 6 * DAY,
           last_seen=T0 + 6 * DAY)

    for sid in ("stOld", "stDup", "stMiss", "stDrift"):
        env.conn.execute("UPDATE stories SET last_seen = ?, updated_at = ? "
                         "WHERE id = ?", (T0 + 5 * DAY + 2 * H, T0 + 5 * DAY + 2 * H, sid))
    # raw marks: the paper's own per-person reading history. Never exported,
    # never consulted by the launch brief.
    env.conn.executemany(
        "INSERT INTO seen (reader_id, by_name, story_id, rev, at) VALUES (?, ?, ?, ?, ?)",
        [(READER, "Alice", "stOld", SEEN_REV_A, T0 + 5 * DAY + 4 * H),
         (READER, "Bob", "stDup", SEEN_REV_B, T0 + 5 * DAY + 5 * H)])
    env.conn.commit()


def _other_reader_paper(env):
    """A second gaceta paper on the same DB: its stories and build dates must
    never surface in testreader's exports or briefs."""
    rdir = env.data_dir / "readers" / "otherreader"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "reader.toml").write_text(
        'name = "Other Reader"\nlangs = "en"\n', encoding="utf-8")
    cfg = gconfig.load_reader("otherreader", env.data_dir)
    env.conn.execute("INSERT OR IGNORE INTO readers (id, name, langs, created_at) "
                     "VALUES ('otherreader', 'Other Reader', 'en', 0)")
    env.conn.execute(
        "INSERT INTO items (id, source_id, author, title, url, published, body, "
        "ingested_at, origin) VALUES ('x1', 'test:direct', NULL, ?, "
        "'https://other.example/x', ?, ?, 0, 'test')",
        (f"{OTHER_CANARY} item", T0 + 6 * DAY, f"{OTHER_CANARY} body"))
    env.conn.execute(
        "INSERT INTO annotations (reader_id, item_id, verdict, note, library_path, "
        "item_privacy, privacy_reason, annotated_at) "
        "VALUES ('otherreader', 'x1', 'noted', NULL, NULL, 'public', 'test', 0)")
    env.conn.execute(
        "INSERT INTO stories (id, reader_id, title, summary, status, confidence, "
        "privacy, first_seen, last_seen, updated_at) "
        "VALUES ('stOther', 'otherreader', ?, NULL, 'settled', NULL, 'public', ?, ?, ?)",
        (f"{OTHER_CANARY} story", T0 + 6 * DAY, T0 + 6 * DAY, T0 + 6 * DAY))
    env.conn.execute("INSERT INTO story_items (story_id, item_id, role) "
                     "VALUES ('stOther', 'x1', 'origin')")
    env.conn.commit()
    _build_edition(env, _day(9), reader="otherreader", cfg=cfg)


def _world(env):
    """The full synthetic world. Returns the caller's baseline as read from
    the REAL D1 export artifact (the agent's own memory of edition D1)."""
    _seed_baseline_state(env)
    _build_edition(env, D1)
    export1 = _export_helper(env, D1)
    baseline = {"reader": READER, "date": D1,
                "versions": {sid: s["current_version"]
                             for sid, s in export1["stories"].items()
                             if s.get("current_version") is not None}}
    _post_baseline_events(env)
    _other_reader_paper(env)
    _build_edition(env, D2)                      # the skipped public edition
    _build_edition(env, D2, max_privacy="private")  # invisible in the trace
    _build_edition(env, D3)
    _export_helper(env, D3)
    return baseline


def _export_helper(env, day):
    """Export via the real CLI without capsys (usable from _world)."""
    import os
    import contextlib
    old = os.environ.get("GACETA_DATA")
    os.environ["GACETA_DATA"] = str(env.data_dir)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli_main(["agent", "export", "--reader", READER, "--date", day])
        assert rc == 0
    finally:
        if old is None:
            os.environ.pop("GACETA_DATA", None)
        else:
            os.environ["GACETA_DATA"] = old
    p = agent_mod.export_dir(env.data_dir, READER) / f"{day}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _brief(out: str) -> dict:
    got = json.loads(out)
    return {u["story_id"]: u for u in got["updates"]}


# ------------------------------------------------------------- the launch flow

def test_launch_brief_known_material_duplicate_and_missing(monkeypatch, env, capsys, tmp_path):
    baseline = _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, baseline)
    assert (rc, err) == (0, "")
    got = json.loads(out)
    u = _brief(out)

    # --- known prior version → material change, with the evidence behind it
    old = u["stOld"]
    assert old["baseline"] == {"claimed_version": 1, "status": "known",
                              "provenance": "unknown",
                              "reason": "baseline version 1 of 2 matches the "
                                        "paper's write-up history"}
    assert old["current_version"] == 2
    assert old["change"]["status"] == "material_change"
    assert len(old["change"]["proposals"]) == 2
    upd, corr = old["change"]["proposals"]
    assert upd["kind"] == "proposed_update" and upd["version"] == 2
    assert upd["what_changed"] == "A second source confirms the April limits."
    assert upd["what_changed_origin"] == "model"
    assert upd["support_kind"] == "joined_sources"
    sup = upd["support"][0]
    assert sup["url"] == "https://example.net/confirm"
    # the excerpt is a verbatim span of the public source's own text
    assert sup["excerpt"] == ("The county engineer confirms the bridge report "
                              "will name two new load limits from April.")
    assert sup["added"] == datetime.fromtimestamp(
        T0 + 5 * DAY + H, tz=timezone.utc).isoformat(timespec="seconds")
    assert corr["kind"] == "proposed_correction"
    assert (corr["said"], corr["now"]) == ("one new load limit", "two new load limits")
    assert corr["support"][0]["url"] == "https://example.net/confirm"
    assert "excerpt" in corr["support"][0]

    # --- known + current → no supported update
    same = u["stSame"]
    assert same["baseline"]["status"] == "known"
    assert "current write-up" in same["baseline"]["reason"]
    assert same["change"]["status"] == "no_supported_update"
    assert "after your baseline point" in same["change"]["reason"]
    # a source published in the future but ingested before the baseline is
    # never surfaced as new (publication time is never compared)
    assert "https://example.com/future" not in json.dumps(same["change"])

    # --- known + assessed duplicate coverage → no supported update, and the
    #     answer says WHICH coverage was judged duplicate
    dup = u["stDup"]
    assert dup["baseline"]["status"] == "known"
    assert dup["change"]["status"] == "no_supported_update"
    assert "duplicate coverage" in dup["change"]["reason"]
    assert dup["change"]["assessed_no_change"][0]["version"] == 2
    assert dup["change"]["assessed_no_change"][0]["support"][0]["url"] == \
        "https://example.net/dup2"

    # --- known + rewrite never assessed → explicit unknown (missing evidence)
    miss = u["stMiss"]
    assert miss["baseline"]["status"] == "known"
    assert miss["change"]["status"] == "unknown"
    assert "missing evidence" in miss["change"]["reason"]
    assert miss["change"]["unassessed_versions"][0]["version"] == 2
    assert miss["change"]["unassessed_versions"][0]["support"][0]["url"] == \
        "https://example.net/miss2"
    assert "proposals" not in miss["change"]  # no invented material claim

    # --- known + coverage with no write-up assessment → unknown, coverage listed
    drift = u["stDrift"]
    assert drift["change"]["status"] == "unknown"
    assert drift["change"]["new_coverage_unassessed"][0]["url"] == \
        "https://example.org/drift2"
    assert "excerpt" in drift["change"]["new_coverage_unassessed"][0]

    # --- not in the baseline, first traced after the baseline date → new
    new = u["stNew"]
    assert new["baseline"]["status"] == "none_stated"
    assert new["change"]["status"] == "new_story"
    assert "first entered the paper's trace" in new["change"]["reason"]
    assert new["change"]["new_coverage_unassessed"][0]["url"] == \
        "https://example.com/new"

    # --- intervening editions: the middle public edition, not the current
    #     one, never the private build, never the other reader's build —
    #     publication chronology only, never an unread claim
    assert got["intervening_editions"] == [
        {"date": D2, "built_at": got["intervening_editions"][0]["built_at"]}]
    note = got["intervening_editions_note"]
    assert "chronology only" in note and "unknown" in note
    # --- API stability (brief v2): the renamed block is versioned, and the
    #     legacy key stays present, deprecated, and pinned to null — a
    #     silent rename would break stable-field clients; a live list would
    #     hand them an unread claim through the old name
    assert got["brief_version"] == 2
    assert got["skipped_editions"] is None
    assert "deprecated" in got["skipped_editions_note"]
    # --- and nothing anywhere reads as a claim on reading history
    assert_no_reading_claims(out)

    # --- the contract blocks ride along
    assert got["semantics"]["comparisons"] and got["semantics"]["trace"]
    assert got["approval"]["state"] == "proposal" and got["approval"]["by"] == "reader"
    assert got["citation"]["policy"] and got["privacy"] == {"max_level": "public"}


def test_launch_brief_from_date_only_baseline(monkeypatch, env, capsys, tmp_path):
    """A caller with only a date (no per-story versions) still gets the
    trace-evidence answer; same-day events are invisible to it (the known
    limitation of date-only granularity)."""
    _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path,
                          {"reader": READER, "date": D1})
    assert (rc, err) == (0, "")
    u = _brief(out)
    assert u["stOld"]["change"]["status"] == "material_change"
    assert u["stOld"]["change"]["proposals"][0]["version"] == 2
    # f2 joined on the baseline date itself (intraday): date-only comparison
    # cannot see it — 'no supported update', the documented granularity limit
    assert u["stDrift"]["change"]["status"] == "no_supported_update"
    assert u["stNew"]["change"]["status"] == "new_story"
    assert json.loads(out)["intervening_editions"][0]["date"] == D2


def test_first_launch_no_baseline_at_all(monkeypatch, env, capsys, tmp_path):
    _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, None)
    assert (rc, err) == (0, "")
    got = json.loads(out)
    u = _brief(out)
    for sid in ("stOld", "stSame", "stDup", "stMiss", "stDrift", "stNew"):
        assert u[sid]["baseline"]["status"] == "none_stated", sid
        assert u[sid]["change"]["status"] == "unknown", sid
    # no date stated → intervening editions is an explicit unknown, not 'none'
    assert got["intervening_editions"] is None
    assert "cannot" in got["intervening_editions_note"]
    # and nothing is claimed anywhere
    for up in got["updates"]:
        assert "proposals" not in up["change"]


# --------------------------------------------------------- approval (the loop)

def test_approval_loop_updates_the_callers_own_baseline(monkeypatch, env, capsys, tmp_path):
    """Evidence-supported proposal → the reader approves → the agent's own
    memory advances (the only write this surface ever implies, and it is the
    caller's): the next brief has nothing left to propose."""
    baseline = _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, baseline)
    assert rc == 0 and _brief(out)["stOld"]["change"]["status"] == "material_change"

    # the reader approves; the agent snapshots the current edition through the
    # real read command and records it as its new baseline
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc, out, err = _read_cli(monkeypatch, capsys, "edition", "--reader", READER)
    assert rc == 0, err
    listing = json.loads(out)
    baseline2 = {"reader": READER, "date": listing["edition"]["date"],
                 "versions": {s["id"]: s["version"] for s in listing["stories"]
                              if s["version"] is not None}}
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, baseline2)
    assert (rc, err) == (0, "")
    u = _brief(out)
    for sid in ("stDup", "stMiss", "stNew", "stSame"):
        assert u[sid]["change"]["status"] == "no_supported_update", sid
        assert "proposals" not in u[sid]["change"]
    # the v2 write-up itself is now covered — but the correction was recorded
    # AFTER the newest write-up, and a version-only baseline cannot confirm the
    # caller saw it: it honestly re-proposes (alone, evidence attached) until a
    # newer write-up version exists
    old = u["stOld"]
    assert old["baseline"]["reason"] == "your baseline version is the current write-up"
    assert old["change"]["status"] == "material_change"
    assert [p["kind"] for p in old["change"]["proposals"]] == ["proposed_correction"]
    # stDrift keeps its unassessed coverage: the paper never wrote a v2, so a
    # version-only baseline cannot confirm the caller saw f2 — honest unknown
    drift = u["stDrift"]
    assert drift["change"]["status"] == "unknown"
    assert drift["change"]["new_coverage_unassessed"][0]["url"] == \
        "https://example.org/drift2"
    assert json.loads(out)["intervening_editions"] == []


# --------------------------------------------------------- unknown baselines

def test_unknown_versions_are_explicit_not_invented(monkeypatch, env, capsys, tmp_path):
    baseline = _world(env)
    # a version ahead of the paper's: stale export or foreign memory — unknown
    b = {**baseline, "versions": {**baseline["versions"], "stOld": 7}}
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, b)
    assert (rc, err) == (0, "")
    old = _brief(out)["stOld"]
    assert old["baseline"]["status"] == "unknown"
    assert "ahead of this export's newest write-up version 2" in old["baseline"]["reason"]
    assert old["change"]["status"] == "unknown"
    assert "proposals" not in old["change"]


def test_story_outside_the_public_edition_is_never_confirmed_or_denied(monkeypatch, env, capsys, tmp_path):
    baseline = _world(env)
    b = {**baseline, "versions": {**baseline["versions"], "ghost": 1}}
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, b)
    assert (rc, err) == (0, "")
    ghost = _brief(out)["ghost"]
    assert ghost["baseline"]["status"] == "unknown"
    assert "no public evidence for this story id" in ghost["baseline"]["reason"]
    assert "cannot confirm or deny" in ghost["baseline"]["reason"]
    assert ghost["change"]["status"] == "unknown"
    # never phrased as non-existence, and the private/household canaries stay
    # unreferenced anywhere in the brief
    assert "unknown story id" not in err
    for canary in (HOUSE_CANARY, PRIV_CANARY, OTHER_CANARY):
        assert canary not in out


# ---------------------------------------------------------- fail-closed baselines

@pytest.mark.parametrize("baseline, word", [
    ({"reader": "someoneelse"}, "baseline is bound to reader 'someoneelse', not 'testreader'"),
    ({}, "baseline must state its reader"),
    ({"reader": READER, "extra": 1}, "unknown keys (extra)"),
    ({"reader": READER, "versions": {"stOld": "1"}}, "must be an integer >= 1"),
    ({"reader": READER, "versions": {"stOld": 0}}, "must be an integer >= 1"),
    ({"reader": READER, "versions": {"x" * 200: 1}}, "malformed story id"),
    ({"reader": READER, "date": "2026-13-40"}, "baseline date is not YYYY-MM-DD"),
    ({"reader": READER, "date": (_day(8))}, "older than your memory"),
])
def test_bad_baselines_refuse(monkeypatch, env, capsys, tmp_path, baseline, word):
    _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, baseline)
    assert rc == 2 and out == ""
    assert word in err


def test_missing_or_malformed_baseline_file_refuses(monkeypatch, env, capsys, tmp_path):
    _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path,
                          {"reader": READER, "date": D1})
    assert rc == 0  # the world is sound
    p = tmp_path / "baseline.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc, out, err = _read_cli(monkeypatch, capsys, "since", "--reader", READER,
                             "--baseline", str(p))
    assert rc == 2 and out == "" and "not valid JSON" in err
    rc, out, err = _read_cli(monkeypatch, capsys, "since", "--reader", READER,
                             "--baseline", str(tmp_path / "nope.json"))
    assert rc == 2 and out == "" and "no baseline file at" in err


def test_baseline_from_stdin(monkeypatch, env, capsys, tmp_path):
    _world(env)
    export = json.loads(agent_mod.current_export_path(
        env.data_dir, READER).read_text(encoding="utf-8"))
    text = json.dumps({"reader": READER, "date": D1, "versions": {"stOld": 1}})
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    baseline = agent_mod.load_baseline("-", reader=READER, export=export)
    # a baseline written before provenance existed loads unchanged in its
    # stated facts; every stated fact then carries the explicit 'unknown' origin
    assert baseline == {"reader": READER, "date": D1, "versions": {"stOld": 1},
                        "provenance": {"date": "unknown",
                                       "versions": {"stOld": "unknown"}}}


# ------------------------------------------------------------- privacy gates

def test_never_leaks_marks_other_readers_or_private_excerpts(monkeypatch, env, capsys, tmp_path):
    _world(env)  # seen marks (Alice/Bob), household/private/other canaries seeded
    payloads = []
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    for argv in (["edition", "--reader", READER],
                 ["story", "--reader", READER, "--id", "stOld"],
                 ["since", "--reader", READER]):
        rc, out, err = _read_cli(monkeypatch, capsys, *argv)
        assert rc == 0, err
        payloads.append(out)
    # and with a real baseline through the full brief
    baseline = json.loads(
        (agent_mod.export_dir(env.data_dir, READER) / f"{D1}.json").read_text(
            encoding="utf-8"))
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path,
                          {"reader": READER, "date": D1,
                           "versions": {sid: s["current_version"]
                                        for sid, s in baseline["stories"].items()
                                        if s.get("current_version") is not None}})
    assert rc == 0
    payloads.append(out)
    for text in payloads:
        for canary in ("Alice", "Bob", SEEN_REV_A, SEEN_REV_B, HOUSE_CANARY,
                       PRIV_CANARY, OTHER_CANARY, "stOther"):
            assert canary not in text
        # the build trace is this reader's public editions only
        listing = json.loads(text)
        if listing.get("editions") is not None:
            assert [e["date"] for e in listing["editions"]] == [D1, D2, D3]


def test_export_refuses_public_story_with_a_nonpublic_member(env):
    """A story labeled public whose member item is not public is corrupt
    state: the trusted export side refuses the whole export."""
    _item(env, "leak1", "Leak item", "leak body", privacy="private",
          published=T0 + 600, ingested=T0 + 600, url="https://example.com/leak")
    _story(env, "stLeak", ["leak1"], "Story Leak", privacy="public")
    env.conn.commit()
    _build_edition(env, D3)  # the edition builder trusts stories.privacy…
    with pytest.raises(agent_mod.ExportError, match="leak1.*refusing"):
        agent_mod.build_export(
            json.loads(agent_mod.snapshot_path(env.data_dir, READER, D3, "public")
                       .read_text(encoding="utf-8")),
            READER, "Test Reader", conn=env.conn)


def test_tampered_exports_refuse_the_brief(monkeypatch, env, capsys, tmp_path):
    baseline = _world(env)
    cases = [
        (lambda e: e["stories"]["stOld"]["versions"][1].update(version=5),
         "contiguous from 1"),
        (lambda e: e["stories"]["stOld"]["versions"][1].update(written_at="garbage"),
         "not an ISO timestamp"),
        (lambda e: e["stories"]["stOld"]["versions"][1]["support"].append(
            {"url": "file:///etc/passwd"}), "non-http(s) url"),
        (lambda e: e["stories"]["stOld"]["versions"][1]["support"][0].update(
            excerpt="x" * 400), "exceeds 280 chars"),
        (lambda e: e["stories"]["stDrift"]["unassessed_coverage"][0].pop("added"),
         "lacks its 'added' trace stamp"),
        (lambda e: e["editions"][0].update(date="nope"), "bad date"),
        (lambda e: e.update(editions=list(reversed(e["editions"]))),
         "strictly increasing"),
        (lambda e: e["stories"]["stOld"].update(rev="abcdef123456"),
         "keys this surface never emits"),
        (lambda e: e.update(portada=[]), "keys this surface never emits"),
    ]
    for mutate, word in cases:
        p = _tampered_copy(env, tmp_path, mutate)
        rc, out, err = _since(monkeypatch, capsys, env, tmp_path, baseline)
        assert rc == 0  # untampered still serves
        monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
        bp = tmp_path / "baseline.json"
        bp.write_text(json.dumps(baseline), encoding="utf-8")
        rc, out, err = _read_cli(monkeypatch, capsys, "since", "--reader", READER,
                                 "--baseline", str(bp), "--path", str(p))
        assert rc == 2 and out == "", word
        assert word in err


# ----------------------------------------------------- time semantics + safety

def test_publication_times_are_never_reading_history(monkeypatch, env, capsys, tmp_path):
    baseline = _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, baseline)
    assert rc == 0
    got = json.loads(out)
    u = _brief(out)
    # o2 was PUBLISHED long before the baseline but INGESTED after: it counts
    # as new coverage via its trace stamp, and its reference still shows the
    # honest (old) publication time
    sup = u["stOld"]["change"]["proposals"][0]["support"][0]
    assert sup["published"] == datetime.fromtimestamp(
        T0 - 10 * DAY, tz=timezone.utc).isoformat(timespec="seconds")
    assert sup["published"] < D1 < sup["added"]
    # no classification or reason ever leans on a publication timestamp
    for up in got["updates"]:
        blob = json.dumps(up["change"].get("reason", "")) + json.dumps(
            up["baseline"]["reason"])
        assert "published" not in blob.lower()
        assert "published" not in json.dumps(up["baseline"]).lower()
    assert "never compared" in got["semantics"]["comparisons"]
    # marks (seen.at, by_name) are the paper's own reading history and never
    # appear as any timestamp in the brief
    assert "Alice" not in out and SEEN_REV_A not in out


def test_since_is_read_only_even_with_a_baseline(monkeypatch, env, capsys, tmp_path):
    baseline = _world(env)
    before = tree_hashes(env.data_dir)
    tables = ("seen", "reactions", "agent_sends", "annotations", "stories",
              "story_versions", "editions")
    counts = {t: env.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in tables}
    for _ in range(2):
        rc, out, err = _since(monkeypatch, capsys, env, tmp_path, baseline)
        assert rc == 0
    assert before == tree_hashes(env.data_dir)
    for t, n in counts.items():
        assert env.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n


def test_since_serves_from_a_sandbox_with_no_database(monkeypatch, env, capsys, tmp_path):
    baseline = _world(env)
    sandbox = tmp_path / "sandbox"
    shutil.copytree(env.data_dir / "agent-exports", sandbox / "agent-exports")
    monkeypatch.setenv("GACETA_DATA", str(sandbox))
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps(baseline), encoding="utf-8")
    rc, out, err = _read_cli(monkeypatch, capsys, "since", "--reader", READER,
                             "--baseline", str(p))
    assert rc == 0, err
    got = json.loads(out)
    by = {u["story_id"]: u for u in got["updates"]}
    assert by["stOld"]["change"]["status"] == "material_change"
    assert got["intervening_editions"][0]["date"] == D2
