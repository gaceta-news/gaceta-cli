"""P0 baseline contract: what counts as 'since I read it'.

The plan's first failing acceptance, pinned as regressions on the real
export path (build_edition → agent export → agent since) over the
synthetic since-world:

- version-only memory (``stOld:1``, no date) — the answer cites the v2
  correction with its source and claims NOTHING about reading date,
  skipped editions or delivery/access cycles;
- the same cycling interest with no last-read record — the paper's
  latest correction is cited as a public fact, SEPARATE from an explicit
  'new to you: unknown';
- a stated date bounds publication chronology only — intervening
  editions are never an unread claim, and the legacy ``skipped_editions``
  key stays present, deprecated, and always null (brief v2: no silent
  rename of a stable field);
- provenance is a caller-stated label, never authentication: a self-
  labeled 'verified_receipt' grades exactly like 'user_statement', and
  the receipt path's real output is still evidence-checked like any
  claim;
- backward compatibility: baselines and receipt fragments written before
  provenance existed load unchanged, old export files still serve the
  brief;
- the wording contract (SKILL.md + agent README) pins the rules and the
  two exemplar answers, so the free-form answer an agent renders from a
  correct JSON cannot quietly regress into the recorded failure mode
  (a model inferring a reading date from a version).

Live free-form model turns are P5 cleanroom work; here the schema, the
skill wording and the exemplars are the pinned contract.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from gaceta import agent as agent_mod
from gaceta.agent import story_rev
from gaceta.cli import main as cli_main

from test_agent_export import READER
from test_agent_since import (D1, D2, D3, assert_no_reading_claims,
                              _since, _world)

DOCS = Path(__file__).resolve().parent.parent / "docs" / "agent"
DEMO_SCRIPT = DOCS / "demo" / "make_demo_export.py"


def _brief(out: str) -> dict:
    return {u["story_id"]: u for u in json.loads(out)["updates"]}


def _receipt_cli(monkeypatch, capsys, env, *, by, story):
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli_main(["agent", "receipt", "--reader", READER,
                   "--by", by, "--story", story])
    caught = capsys.readouterr()
    return rc, caught.out, caught.err


# ------------------------------------------------ acceptance 1: version-only

def test_version_only_acceptance_cites_correction_and_claims_nothing(
        monkeypatch, env, capsys, tmp_path):
    """Baseline: only ``stOld:1``; v2 has a sourced correction. Pass =
    the answer cites the correction and makes NO claim about reading
    date, skipped editions or cycle access."""
    _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path,
                          {"reader": READER, "versions": {"stOld": 1}})
    assert (rc, err) == (0, "")
    got = json.loads(out)
    old = _brief(out)["stOld"]

    # the correction IS cited, with the public source behind it
    assert old["baseline"]["status"] == "known"
    assert old["change"]["status"] == "material_change"
    corr = next(p for p in old["change"]["proposals"]
                if p["kind"] == "proposed_correction")
    assert (corr["said"], corr["now"]) == ("one new load limit",
                                           "two new load limits")
    assert corr["support"][0]["url"] == "https://example.net/confirm"
    assert "two new load limits" in corr["support"][0]["excerpt"]

    # and nothing claims reading history, anywhere
    assert_no_reading_claims(out)
    assert got["baseline"]["date"] is None          # no date synthesized
    assert got["intervening_editions"] is None      # readership: unknown
    assert "will not infer" in got["intervening_editions_note"]
    assert "never derives a reading date" in got["semantics"]["reading_history"]
    # the story's own block carries no date the model could launder into
    # 'you last read on …'
    assert re.search(r"\d{4}-\d{2}-\d{2}", json.dumps(old["baseline"])) is None

    # API stability: the change is versioned, the old key stays, inert
    assert got["brief_version"] == 2
    assert got["skipped_editions"] is None
    assert "deprecated" in got["skipped_editions_note"]
    # provenance of the one stated fact: unstated origin → explicit unknown
    assert old["baseline"]["provenance"] == "unknown"
    assert got["baseline"]["provenance"] == {
        "date": None, "versions": {"stOld": "unknown"}}


# --------------------------------------- acceptance 2: cycling interest

def test_cycling_interest_no_last_read_cites_correction_separately(
        monkeypatch, env, capsys, tmp_path):
    """Same story the reader keeps asking about, but NO last-read record:
    the paper's latest correction is cited as a public fact, SEPARATE
    from 'new to you: unknown' — neither 'nothing new' nor 'new to you'."""
    _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, None)
    assert (rc, err) == (0, "")
    old = _brief(out)["stOld"]
    assert old["baseline"]["status"] == "none_stated"
    assert old["change"]["status"] == "unknown"
    assert "new to you is unknown" in old["change"]["reason"]
    assert "proposals" not in old["change"]          # no material claim
    on_record = old["change"]["on_record"]           # the citation lives
    assert on_record["current_version"] == 2
    corr = on_record["corrections"][-1]              # latest on record
    assert (corr["said"], corr["now"]) == ("one new load limit",
                                           "two new load limits")
    assert corr["supported_by"]["url"] == "https://example.net/confirm"
    assert "excerpt" in corr["supported_by"]
    assert "not claims about what you have read" in on_record["note"]
    assert_no_reading_claims(out)


def test_cycling_interest_with_a_date_but_no_version_separates_chronology(
        monkeypatch, env, capsys, tmp_path):
    """A stated date, no version for the story: the correction is cited
    as chronology, and personal novelty stays its own explicit unknown —
    a separate field, never merged into the proposal."""
    _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path,
                          {"reader": READER, "date": D1})
    assert (rc, err) == (0, "")
    old = _brief(out)["stOld"]
    assert old["change"]["status"] == "material_change"   # chronology
    corr = next(p for p in old["change"]["proposals"]
                if p["kind"] == "proposed_correction")
    assert corr["support"][0]["url"] == "https://example.net/confirm"
    # …and the personal unknown rides as its OWN key, not inside the note
    assert old["change"]["new_to_you"] == "unknown"
    assert "state no version" in old["change"]["new_to_you_note"]
    assert_no_reading_claims(out)


# ------------------------------------------- acceptance 3: dates vs skipped

def test_stated_date_bounds_chronology_never_an_unread_claim(
        monkeypatch, env, capsys, tmp_path):
    """An explicitly known prior date bounds publication chronology; it
    never licenses 'you did not read the intervening editions'."""
    baseline = _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, baseline)
    assert (rc, err) == (0, "")
    got = json.loads(out)
    assert got["intervening_editions"] == [
        {"date": D2, "built_at": got["intervening_editions"][0]["built_at"]}]
    note = got["intervening_editions_note"]
    assert "chronology only" in note
    assert "not evidence" in note and "unknown" in note
    assert_no_reading_claims(out)
    # the legacy key can no longer deliver the old claim-shaped list
    assert got["skipped_editions"] is None
    assert "deprecated" in got["skipped_editions_note"]


def test_version_only_returns_unknown_for_intervening_editions(
        monkeypatch, env, capsys, tmp_path):
    """With version only, versions are compared and readership of any
    intervening edition is an explicit unknown — even though the export
    carries the full public build trace."""
    _world(env)
    export = json.loads(agent_mod.current_export_path(
        env.data_dir, READER).read_text(encoding="utf-8"))
    assert [e["date"] for e in export["editions"]] == [D1, D2, D3]
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path,
                          {"reader": READER, "versions": {"stOld": 1}})
    assert (rc, err) == (0, "")
    got = json.loads(out)
    assert got["intervening_editions"] is None
    assert "cannot" in got["intervening_editions_note"]
    assert_no_reading_claims(out)


# ------------------------------------------------------ provenance contract

def test_provenance_user_statement_flows_through(monkeypatch, env, capsys,
                                                 tmp_path):
    _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, {
        "reader": READER, "date": D1, "versions": {"stOld": 1},
        "provenance": {"date": "user_statement",
                       "versions": {"stOld": "user_statement"}}})
    assert (rc, err) == (0, "")
    got = json.loads(out)
    old = _brief(out)["stOld"]
    assert old["baseline"]["status"] == "known"
    assert old["baseline"]["provenance"] == "user_statement"
    assert got["baseline"]["provenance"] == {
        "date": "user_statement",
        "versions": {"stOld": "user_statement"}}


def test_self_labelled_verified_receipt_is_not_authenticated(
        monkeypatch, env, capsys, tmp_path):
    """Anyone can WRITE 'verified_receipt' into their baseline file. The
    label must buy nothing: identical grading to 'user_statement', and
    no 'verified'/'authenticated' status anywhere in the answer."""
    _world(env)
    graded = {}
    for token in ("user_statement", "verified_receipt", "unknown"):
        rc, out, err = _since(monkeypatch, capsys, env, tmp_path, {
            "reader": READER, "versions": {"stOld": 1},
            "provenance": {"versions": {"stOld": token}}})
        assert (rc, err) == (0, "")
        got = json.loads(out)
        old = _brief(out)["stOld"]
        # same evidence check, same answer, whatever the label says
        graded[token] = {k: v for k, v in old["baseline"].items()
                         if k != "provenance"} | {"change": old["change"]}
        assert old["baseline"]["provenance"] == token
        assert "never authentication" in got["semantics"]["provenance"]
        # the reason never promotes the label to a status
        assert "verified" not in old["baseline"]["reason"].lower()
        assert "authenticated" not in out.lower()
    assert graded["verified_receipt"] == graded["user_statement"]
    assert graded["unknown"] == graded["user_statement"]


@pytest.mark.parametrize("baseline, word", [
    ({"reader": READER, "versions": {"stOld": 1}, "provenance": "junk"},
     "provenance must be an object"),
    ({"reader": READER, "versions": {"stOld": 1},
      "provenance": {"extra": 1}},
     "provenance has unknown keys (extra)"),
    ({"reader": READER, "versions": {"stOld": 1},
      "provenance": {"versions": {"stOld": "guess"}}},
     "must be one of"),
    ({"reader": READER, "versions": {"stOld": 1},
      "provenance": {"versions": {"ghost": "user_statement"}}},
     "states no version for"),
    # an origin for a date the baseline does not state: contradictory memory
    ({"reader": READER, "versions": {"stOld": 1},
      "provenance": {"date": "user_statement"}},
     "states no date"),
    # a stated date with a token this surface does not know
    ({"reader": READER, "date": D1, "versions": {"stOld": 1},
      "provenance": {"date": 1}},
     "must be one of"),
])
def test_bad_provenance_refuses(monkeypatch, env, capsys, tmp_path,
                                baseline, word):
    _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, baseline)
    assert rc == 2 and out == ""
    assert word in err


def test_receipt_fragment_grades_verified_but_is_still_evidence_checked(
        monkeypatch, env, capsys, tmp_path):
    """The receipt path's own output (a real verified rev) passes to the
    brief unchanged and is graded against the evidence like any claim —
    the verified_receipt label carries continuity, not extra trust."""
    _world(env)
    # a real mark: the rev the paper's own history reproduces for v1
    # (Carol has no mark in this world, so the row is hers alone)
    env.conn.execute(
        "INSERT INTO seen (reader_id, by_name, story_id, rev, at) "
        "VALUES (?, 'Carol', 'stOld', ?, 1)",
        (READER, story_rev([], "write-up v1")))
    env.conn.commit()
    rc, out, err = _receipt_cli(monkeypatch, capsys, env,
                                by="carol", story="stOld")
    assert rc == 0, err
    fragment = json.loads(out)
    assert fragment == {"reader": READER, "versions": {"stOld": 1},
                        "provenance": {"versions": {"stOld":
                                                    "verified_receipt"}}}

    p = tmp_path / "receipt.json"
    p.write_text(out, encoding="utf-8")
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, fragment)
    assert (rc, err) == (0, "")
    old = _brief(out)["stOld"]
    assert old["baseline"] == {"claimed_version": 1, "status": "known",
                              "provenance": "verified_receipt",
                              "reason": "baseline version 1 of 2 matches the "
                                        "paper's write-up history"}
    # still evidence-checked: the correction is proposed with its source
    assert old["change"]["status"] == "material_change"
    assert old["change"]["proposals"][0]["support"][0][
        "url"] == "https://example.net/confirm"
    # and the receipt's no-date honesty holds: readership stays unknown
    assert json.loads(out)["intervening_editions"] is None
    assert_no_reading_claims(out)


# -------------------------------------------------- backward compatibility

def test_legacy_baseline_and_fragment_load_unchanged(monkeypatch, env,
                                                     capsys, tmp_path):
    """Baseline files written before provenance existed (and receipt
    fragments in the old shape) still load and grade exactly as the
    pre-change suite pinned — every stated fact then reads 'unknown'."""
    baseline = _world(env)
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path, baseline)
    assert (rc, err) == (0, "")
    u = _brief(out)
    assert u["stOld"]["baseline"]["status"] == "known"
    assert u["stOld"]["change"]["status"] == "material_change"
    assert u["stSame"]["change"]["status"] == "no_supported_update"
    assert u["stDup"]["change"]["status"] == "no_supported_update"
    assert u["stMiss"]["change"]["status"] == "unknown"
    assert u["stDrift"]["change"]["status"] == "unknown"
    assert u["stNew"]["change"]["status"] == "new_story"
    assert u["stOld"]["baseline"]["provenance"] == "unknown"
    # an old hand-written receipt fragment: reader + versions, nothing else
    rc, out, err = _since(monkeypatch, capsys, env, tmp_path,
                          {"reader": READER, "versions": {"stOld": 1}})
    assert (rc, err) == (0, "")
    assert _brief(out)["stOld"]["baseline"]["status"] == "known"


def test_demo_export_file_still_serves_the_brief(monkeypatch, capsys,
                                                 tmp_path):
    """An export file built by the pre-change generator (schema_version 1,
    no provenance era) still serves the v2 brief — the export schema is
    untouched by the P0 contract change."""
    spec = importlib.util.spec_from_file_location("make_demo_export",
                                                  DEMO_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out_path = Path(mod.build(tmp_path))
    bp = tmp_path / "baseline.json"
    bp.write_text(json.dumps({"reader": "demo",
                              "versions": {"demo-trail": 1}}),
                  encoding="utf-8")
    monkeypatch.setenv("GACETA_DATA", str(tmp_path))
    rc = cli_main(["agent", "since", "--reader", "demo",
                  "--baseline", str(bp), "--path", str(out_path),
                  "--max-age-hours", "999999"])
    caught = capsys.readouterr()
    out, err = caught.out, caught.err
    assert rc == 0, err
    got = json.loads(out)
    assert got["brief_version"] == 2
    assert got["intervening_editions"] is None
    assert got["skipped_editions"] is None
    assert_no_reading_claims(out)


# ------------------------------------------------------ the wording contract

SKILL_MARKERS = (
    "Never infer a reading date from a version",
    "intervening editions",
    "publication chronology",
    "deprecated",               # skipped_editions is retained, inert
    "never authentication",     # provenance labels buy nothing
    "unknown to you",           # public facts vs personal novelty
)
README_MARKERS = (
    "provenance",
    "intervening editions",
    "publication chronology",
    "never authentication",
)


def _fenced_after(text: str, heading: str) -> str:
    """The fenced block under one '### Example:' heading in SKILL.md."""
    start = text.index(heading)
    rest = text[start:]
    nxt = rest.find("\n### ", len(heading))
    section = rest if nxt < 0 else rest[:nxt]
    blocks = re.findall(r"```[a-z]*\n(.*?)```", section, re.DOTALL)
    assert blocks, f"no fenced exemplar under {heading!r}"
    return blocks[0]


def _normalized(text: str) -> str:
    """Collapse all whitespace to single spaces, so a rule that wraps in
    the markdown still matches as the phrase an agent actually reads."""
    return " ".join(text.split())


def test_skill_wording_pins_the_contract():
    skill = _normalized((DOCS / "SKILL.md").read_text(encoding="utf-8"))
    for marker in SKILL_MARKERS:
        assert marker.lower() in skill.lower(), marker


def test_agent_readme_matches_the_contract():
    readme = _normalized((DOCS / "README.md").read_text(encoding="utf-8"))
    for marker in README_MARKERS:
        assert marker.lower() in readme.lower(), marker


def test_exemplar_answers_follow_the_contract():
    """The two exemplar answers are the free-form half of the acceptance:
    each cites what is citable, states its unknowns as unknown, and
    carries none of the claim-shaped phrasings."""
    skill = (DOCS / "SKILL.md").read_text(encoding="utf-8")
    version_only = _normalized(
        _fenced_after(skill, "### Example: version-only baseline"))
    assert "correction" in version_only.lower()
    assert "unknown" in version_only.lower()
    assert_no_reading_claims(version_only)

    no_record = _normalized(
        _fenced_after(skill, "### Example: no last-read record"))
    assert "correction" in no_record.lower()
    assert "unknown to you" in no_record.lower()
    assert_no_reading_claims(no_record)
