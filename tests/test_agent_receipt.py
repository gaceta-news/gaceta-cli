"""The seen-marks receipt (`gaceta agent receipt`): the owner-side, opt-in
bridge that hands ONE person their own reading history back — the write-up
version their actual 'Visto' mark pins, verified against the paper's own
history or honestly unknown — rendered as the baseline the existing
`agent since` path already accepts.

Acceptance pinned here:
- the public content bundle stays byte-identical independent of reading
  history (the export never opens the seen table);
- the receipt is the person's own memory and nothing more: no raw name, no
  mark timestamp, not even the rev; only the explicit (reader, person, story)
  row is consulted — another person's mark on the same story never answers,
  another reader's rows never enter;
- the mapping is verified or unknown: an actual mark on an earlier revision
  (the rev the D1 page really carried), an edition skipped in between, then
  the brief grading the receipt against the current evidence; another
  person or a rev matching nothing → unknown, nothing stated;
- gaceta writes nothing: no mutations, and the receipt never joins the
  public export.

Everything runs the real paths: build_edition for the public snapshots
(D1 read, D2 skipped, D3 current), the real CLI for export/receipt/since,
and marks carrying the revs those snapshots actually recorded.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from gaceta import agent as agent_mod
from gaceta.cli import main as cli_main

from test_agent_export import READER
from test_agent_since import _build_edition, _export
from test_cluster import DAY, T0, add_item
from test_readonly import tree_hashes

H = 3600
FAKE_REV = "eeee0000aaaa1111"  # well-formed hex, matches nothing on record


def _day(n: int) -> str:
    return datetime.fromtimestamp(T0 + n * DAY, tz=timezone.utc).date().isoformat()


D1, D2, D3 = _day(5), _day(6), _day(7)


# ------------------------------------------------------------------ helpers

def _item(env, iid, title, body, *, published, ingested, url):
    add_item(env, iid, title, body, "noted", "public", published)
    env.conn.execute("UPDATE items SET url = ?, ingested_at = ? WHERE id = ?",
                     (url, ingested, iid))


def _story(env, sid, item_ids, title, summary, *, privacy="public",
           last_seen=T0 + 2 * DAY):
    env.conn.execute(
        "INSERT INTO stories (id, reader_id, title, summary, status, confidence, "
        "privacy, first_seen, last_seen, updated_at) "
        "VALUES (?, ?, ?, ?, 'settled', NULL, ?, ?, ?, ?)",
        (sid, READER, title, summary, privacy, T0, last_seen, last_seen))
    env.conn.executemany(
        "INSERT INTO story_items (story_id, item_id, role) VALUES (?, ?, ?)",
        [(sid, iid, "origin" if k == 0 else "update")
         for k, iid in enumerate(item_ids)])


def _version(env, sid, version, summary, written_at, what_changed):
    env.conn.execute(
        "INSERT INTO story_versions (story_id, version, summary, model, "
        "written_at, members, what_changed) VALUES (?, ?, ?, 'fake', ?, ?, ?)",
        (sid, version, summary, written_at, 1, what_changed))


def _mark(env, by, sid, rev, at=T0 + 5 * DAY + 4 * H):
    """The raw mark the way the local UI writes it (serve.py POST /api/seen)."""
    env.conn.execute(
        "INSERT INTO seen (reader_id, by_name, story_id, rev, at) VALUES (?, ?, ?, ?, ?)",
        (READER, by, sid, rev, at))
    env.conn.commit()


def _snapshot_rev(env, day, sid) -> str:
    """The rev the page of `day`'s public edition actually carried — the
    data-rev the UI posts with a 'Visto' mark."""
    snap = json.loads(agent_mod.snapshot_path(
        env.data_dir, READER, day, "public").read_text(encoding="utf-8"))
    return snap["stories"][sid]["rev"]


def _world(env) -> str:
    """A public story written up at v1 (edition D1 — the revision the person
    actually marks seen), then a second source and a v2 rewrite before the
    skipped edition D2 and the current one, D3. Returns the real rev of the
    D1 page (what an actual mark on the earlier revision carries)."""
    _item(env, "o1", "Bridge inspection due", "The valley council said the "
          "bridge inspection report would be published in March.",
          published=T0 - 30 * DAY, ingested=T0, url="https://example.com/old")
    _story(env, "stOld", ["o1"], "Story Old", "write-up v1")
    _version(env, "stOld", 1, "write-up v1", T0 + DAY,
             "First write-up from the inspection note.")

    _item(env, "d1", "One load limit", "One load limit is planned for the "
          "county road.", published=T0 + 600, ingested=T0 + 600,
          url="https://example.com/dup1")
    _story(env, "stDup", ["d1"], "Story Dup", "dup write-up")
    _version(env, "stDup", 1, "dup write-up", T0 + DAY, None)

    # household canary: a receipt must refuse it even with a mark on file
    _item(env, "h1", "Household note", "household body", published=T0,
          ingested=T0, url="https://h.example/h")
    _story(env, "stH", ["h1"], "Household story", "house write-up",
           privacy="household")
    env.conn.commit()

    _build_edition(env, D1)                    # the edition the person read
    d1_rev = _snapshot_rev(env, D1, "stOld")   # the earlier revision's rev

    # after D1: a second source joins stOld, the write-up moves to v2
    _item(env, "o2", "Engineer confirms two limits", "The county engineer "
          "confirms the bridge report will name two new load limits from April.",
          published=T0 - 10 * DAY, ingested=T0 + 5 * DAY + H,
          url="https://example.net/confirm")
    env.conn.execute("INSERT INTO story_items (story_id, item_id, role) "
                     "VALUES ('stOld', 'o2', 'update')")
    _version(env, "stOld", 2, "write-up v2", T0 + 5 * DAY + 2 * H,
             "A second source confirms the April limits.")
    env.conn.execute(
        "UPDATE stories SET summary = 'write-up v2', last_seen = ?, updated_at = ? "
        "WHERE id = 'stOld'", (T0 + 5 * DAY + 2 * H, T0 + 5 * DAY + 2 * H))
    env.conn.commit()
    _build_edition(env, D2)                    # the edition the person skipped
    _build_edition(env, D3)                    # the current one
    return d1_rev


def _other_reader(env):
    """A second paper on the same DB, with its own story and its own seen
    mark: its rows must never answer this reader's receipts."""
    env.conn.execute("INSERT OR IGNORE INTO readers (id, name, langs, created_at) "
                     "VALUES ('otherreader', 'Other Reader', 'en', 0)")
    env.conn.execute(
        "INSERT INTO items (id, source_id, author, title, url, published, body, "
        "ingested_at, origin) VALUES ('x1', 'test:direct', NULL, 'Other story', "
        "'https://other.example/x', ?, 'other body', 0, 'test')", (T0 + 6 * DAY,))
    env.conn.execute(
        "INSERT INTO annotations (reader_id, item_id, verdict, note, library_path, "
        "item_privacy, privacy_reason, annotated_at) "
        "VALUES ('otherreader', 'x1', 'noted', NULL, NULL, 'public', 'test', 0)")
    env.conn.execute(
        "INSERT INTO stories (id, reader_id, title, summary, status, confidence, "
        "privacy, first_seen, last_seen, updated_at) "
        "VALUES ('stOther', 'otherreader', 'Other story', 'other write-up', "
        "'settled', NULL, 'public', ?, ?, ?)", (T0 + 6 * DAY,) * 3)
    env.conn.execute("INSERT INTO story_items (story_id, item_id, role) "
                     "VALUES ('stOther', 'x1', 'origin')")
    env.conn.execute(
        "INSERT INTO seen (reader_id, by_name, story_id, rev, at) "
        "VALUES ('otherreader', 'Zoe', 'stOther', 'abcdef0123456789', 0)")
    env.conn.commit()


def _receipt(monkeypatch, capsys, env, *, by, story, reader=READER):
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli_main(["agent", "receipt", "--reader", reader, "--by", by,
                   "--story", story])
    caught = capsys.readouterr()
    return rc, caught.out, caught.err


# --------------------------------------------------- the public bundle is fixed

def test_public_bundle_identical_independent_of_reading_history(monkeypatch, env, capsys):
    d1_rev = _world(env)
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli_main(["agent", "export", "--reader", READER, "--date", D3])
    caught = capsys.readouterr()
    assert rc == 0, caught.err
    dated = agent_mod.export_dir(env.data_dir, READER) / f"{D3}.json"
    current = agent_mod.current_export_path(env.data_dir, READER)
    before_dated, before_current = dated.read_bytes(), current.read_bytes()

    # the paper's own reading history lands: an actual mark, an unverifiable
    # one, another person's — none of it may move the public bundle by a byte
    _mark(env, "Alice", "stOld", d1_rev)
    _mark(env, "Alice", "stDup", FAKE_REV)
    _mark(env, "Bob", "stOld", _snapshot_rev(env, D3, "stOld"))
    capsys.readouterr()
    rc = cli_main(["agent", "export", "--reader", READER, "--date", D3])
    caught = capsys.readouterr()
    assert rc == 0, caught.err
    assert dated.read_bytes() == before_dated
    assert current.read_bytes() == before_current
    # and none of the reading history rides along in the first place
    assert b"Alice" not in before_dated and b"Bob" not in before_dated
    assert d1_rev.encode() not in before_dated


# ------------------------------------------- an actual mark → receipt → brief

def test_receipt_from_a_real_mark_on_an_earlier_revision_reaches_the_brief(
        monkeypatch, env, capsys, tmp_path):
    d1_rev = _world(env)
    _export(monkeypatch, env, capsys, D3)
    # the actual mark on the earlier revision, plus another person reading
    # the current one on the very same story
    _mark(env, "Alice", "stOld", d1_rev)
    _mark(env, "Bob", "stOld", _snapshot_rev(env, D3, "stOld"))
    export = json.loads(
        (agent_mod.export_dir(env.data_dir, READER) / f"{D3}.json").read_text(
            encoding="utf-8"))
    # the skipped edition D2 really is in the public trace between the
    # marked revision (D1) and the current evidence (D3)
    assert [e["date"] for e in export["editions"]] == [D1, D2, D3]

    # owner side, opt-in: derive Alice's receipt for the public story
    rc, out, err = _receipt(monkeypatch, capsys, env, by="alice", story="stOld")
    assert rc == 0, err
    assert json.loads(out) == {"reader": READER, "versions": {"stOld": 1},
                               "provenance": {"versions": {"stOld":
                                                           "verified_receipt"}}}
    # the consent/host boundary is said out loud where the file is made
    assert "never into the public export" in err
    # the receipt carries no raw reading history: no name, no timestamp, no rev
    for leaked in ("Alice", "alice", "Bob", d1_rev, str(T0 + 5 * DAY + 4 * H)):
        assert leaked not in out

    # only the person's own row answers (case-insensitive, the paper's own
    # person matching): Bob's mark on the same story is a different state
    rc, out_b, err_b = _receipt(monkeypatch, capsys, env, by="bob", story="stOld")
    assert rc == 0, err_b
    assert json.loads(out_b) == {"reader": READER, "versions": {"stOld": 2},
                                 "provenance": {"versions": {"stOld":
                                                               "verified_receipt"}}}
    rc, out_a2, _err = _receipt(monkeypatch, capsys, env, by="Alice", story="stOld")
    assert json.loads(out_a2) == json.loads(out)  # unchanged by Bob's mark

    # explicit passing to the existing since path, for a public story
    p = tmp_path / "receipt.json"
    p.write_text(out, encoding="utf-8")
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli_main(["agent", "since", "--reader", READER, "--baseline", str(p)])
    caught = capsys.readouterr()
    assert rc == 0, caught.err
    got = json.loads(caught.out)
    u = {x["story_id"]: x for x in got["updates"]}

    # the mark was on the earlier revision; the skipped edition sits in the
    # public trace between it and the current evidence (asserted above)
    old = u["stOld"]
    assert old["baseline"] == {"claimed_version": 1, "status": "known",
                              "provenance": "verified_receipt",
                              "reason": "baseline version 1 of 2 matches the "
                                        "paper's write-up history"}
    assert old["current_version"] == 2
    assert old["change"]["status"] == "material_change"
    prop = old["change"]["proposals"][0]
    assert (prop["version"], prop["what_changed"]) == (
        2, "A second source confirms the April limits.")
    assert prop["support"][0]["url"] == "https://example.net/confirm"

    # the seen table records marks, not which edition was read: the receipt
    # states no date, and the brief answers the intervening-editions question
    # with its honest unknown instead of inventing D2
    assert got["intervening_editions"] is None
    assert "cannot" in got["intervening_editions_note"]

    # stories the receipt says nothing about claim nothing
    assert u["stDup"]["baseline"]["status"] == "none_stated"
    assert u["stDup"]["change"]["status"] == "unknown"
    # and no per-person anything rides along in the brief
    for leaked in ("Alice", "alice", "Bob", d1_rev, FAKE_REV):
        assert leaked not in caught.out


def test_receipt_unknown_for_another_person_or_a_rev_matching_nothing(
        monkeypatch, env, capsys, tmp_path):
    d1_rev = _world(env)
    _export(monkeypatch, env, capsys, D3)
    _mark(env, "Alice", "stOld", d1_rev)
    _mark(env, "Alice", "stDup", FAKE_REV)  # well-formed rev, matches nothing

    # another person: no mark of theirs on record → unknown, nothing stated
    rc, out, err = _receipt(monkeypatch, capsys, env, by="Carol", story="stOld")
    assert rc == 0
    assert json.loads(out) == {"reader": READER, "versions": {}}
    assert "unknown" in err.lower()

    # a mark whose rev matches no write-up on record → unknown
    rc, out, err = _receipt(monkeypatch, capsys, env, by="Alice", story="stDup")
    assert rc == 0, err
    assert json.loads(out) == {"reader": READER, "versions": {}}
    assert "unknown" in err.lower()

    # the module result says it out loud too — never an invented version
    r = agent_mod.derive_receipt(env.conn, READER, "Carol", "stOld")
    assert r["status"] == "unknown" and r["version"] is None
    assert "no seen mark" in r["reason"]
    r = agent_mod.derive_receipt(env.conn, READER, "Alice", "stDup")
    assert r["status"] == "unknown" and r["version"] is None
    assert agent_mod.receipt_baseline(r) == {"reader": READER, "versions": {}}

    # an unknown receipt still passes to the existing since path and claims
    # nothing there (baseline none_stated, change unknown, no proposals)
    p = tmp_path / "receipt.json"
    p.write_text(out, encoding="utf-8")
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli_main(["agent", "since", "--reader", READER, "--baseline", str(p)])
    caught = capsys.readouterr()
    assert rc == 0, caught.err
    u = {x["story_id"]: x for x in json.loads(caught.out)["updates"]}
    assert u["stDup"]["baseline"]["status"] == "none_stated"
    assert u["stDup"]["change"]["status"] == "unknown"
    assert "proposals" not in u["stDup"]["change"]


def test_receipt_refuses_nonpublic_or_foreign_stories(monkeypatch, env, capsys):
    _world(env)
    _other_reader(env)
    _mark(env, "Alice", "stH", "abc123def456789")  # even a real mark on file

    # household: the receipt path serves public stories only — the agent
    # surface could never grade the rest, so their ids never travel
    rc, out, err = _receipt(monkeypatch, capsys, env, by="Alice", story="stH")
    assert rc == 2 and out == ""
    assert "not public" in err
    # unknown story id for this reader → fail closed, no receipt
    rc, out, err = _receipt(monkeypatch, capsys, env, by="Alice", story="stGhost")
    assert rc == 2 and out == ""
    assert "unknown story" in err
    # another reader's story never answers under this reader
    rc, out, err = _receipt(monkeypatch, capsys, env, by="Alice", story="stOther")
    assert rc == 2 and out == ""
    assert "unknown story" in err
    # a bad person name refuses the way the local server does
    rc, out, err = _receipt(monkeypatch, capsys, env, by="   ", story="stOld")
    assert rc == 2 and out == ""
    assert "1..40" in err


def test_receipt_reads_only_that_one_row_and_writes_nothing(
        monkeypatch, env, capsys, tmp_path):
    d1_rev = _world(env)
    _other_reader(env)  # another reader's marks exist; they must not answer
    _mark(env, "Alice", "stOld", d1_rev)
    _mark(env, "Bob", "stOld", _snapshot_rev(env, D3, "stOld"))
    _export(monkeypatch, env, capsys, D3)
    before = tree_hashes(env.data_dir)
    counts = {t: env.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("seen", "stories", "story_versions", "editions",
                        "items", "annotations", "reactions")}

    for by, sid in (("alice", "stOld"), ("Alice", "stOld"), ("bob", "stOld"),
                    ("Carol", "stOld"), ("Alice", "stDup")):
        rc, out, err = _receipt(monkeypatch, capsys, env, by=by, story=sid)
        assert rc == 0, err
        rc, out, err = _receipt(monkeypatch, capsys, env, by=by, story=sid)
        assert rc == 0, err  # idempotent, still nothing written
    assert json.loads(out) == {"reader": READER, "versions": {}}  # last: stDup

    # the receipt through the existing brief, still nothing written
    p = tmp_path / "receipt.json"
    p.write_text(out, encoding="utf-8")
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli_main(["agent", "since", "--reader", READER, "--baseline", str(p)])
    assert rc == 0, capsys.readouterr().err

    assert tree_hashes(env.data_dir) == before
    for t, n in counts.items():
        assert env.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n
