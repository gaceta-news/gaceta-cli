"""Reader build status: the story badge rules, and the per-reader status file
`gaceta edition` writes after every run (success, failure, nothing-new skip).
Offline: builds go through the CLI with the fixture reader and fake models."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from gaceta import status
from gaceta.cli import main as cli_main
from gaceta.edition import snapshot_path
from gaceta.edition.build import edition_status
from gaceta.editor import cluster as run_cluster
from gaceta.editor.cluster import embed_items
from gaceta.providers.embed import FakeEmbedder

from test_cluster import T0, DAY, add_item
from test_edition_build import _edition_date

D = 86400

SCHEMA = {"reader", "built_at", "last_ok", "last_fail", "fail_reason",
          "edition_id", "new_items", "llm_calls", "consecutive_failures"}


def test_status_rules():
    end = 100 * D
    assert edition_status("corrected", 1, 0, end) == "corrected"
    assert edition_status("developing", 3, end - 1 * D, end) == "developing"
    assert edition_status("developing", 3, end - 6 * D, end) == "settled"
    assert edition_status("developing", 1, end, end) == "settled"


# --------------------------------------------------------------- unit: status

def test_clean_reason_strips_urls_home_and_noise():
    exc = RuntimeError(f"fetch failed at https://feed.example/item?token=abc&x=1 "
                        f"for {Path.home()}/gaceta-data   reader")
    reason = status.clean_reason(exc)
    assert reason.startswith("RuntimeError:")
    assert "?token=" not in reason and "?x=1" not in reason  # no query strings
    assert "https://feed.example/item" in reason
    assert str(Path.home()) not in reason and "~/gaceta-data" in reason
    assert "  " not in reason
    assert len(status.clean_reason(ValueError("x" * 1000))) <= status.REASON_MAX


def test_age_s_follows_last_ok():
    now = time.time()
    assert status.age_s({}) is None
    assert status.age_s({"last_ok": None}) is None
    stamp = datetime.fromtimestamp(now - 90).astimezone().isoformat(timespec="seconds")
    assert 80 <= status.age_s({"last_ok": stamp}, now=now) <= 95
    assert status.age_s({"last_ok": "not a date"}, now=now) is None


def test_status_path_rejects_bad_reader_ids(tmp_path):
    for bad in ("../evil", "a/b", "", "UPPER", ".hidden"):
        with pytest.raises(ValueError):
            status.status_path(tmp_path, bad)


# ------------------------------------------- the CLI writes it on every run

def _ready(env, monkeypatch, items=None):
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    for it in items or [
        ("a1", "A one", "alpha beta gamma", "noted", "public", T0),
        ("a2", "A two", "alpha beta gamma", "noted", "public", T0 + DAY),
    ]:
        add_item(env, *it)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    run_cluster.cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)
    return env.cfg["id"]


def _edition(env, *extra):
    return cli_main(["edition", "--reader", env.cfg["id"],
                     "--date", _edition_date(), "--max-privacy", "private", *extra])


def test_status_written_on_success(env, monkeypatch):
    reader = _ready(env, monkeypatch)
    assert _edition(env) == 0
    doc = status.load(env.data_dir, reader)
    assert doc is not None
    assert set(doc) == SCHEMA and "skipped" not in doc
    snap = json.loads(snapshot_path(env.data_dir, reader, _edition_date(), "private")
                      .read_text(encoding="utf-8"))
    assert doc["built_at"] == snap["built_at"] == doc["last_ok"]
    datetime.fromisoformat(doc["built_at"])  # ISO, with offset
    assert doc["edition_id"] == f"{_edition_date()}-private"
    assert doc["new_items"] == 2   # first build: both ingested items are new
    assert doc["llm_calls"] == 0   # the edition build itself calls no model
    assert doc["last_fail"] is None and doc["fail_reason"] is None
    assert doc["consecutive_failures"] == 0


def test_status_failure_counts_then_resets_on_success(env, monkeypatch):
    reader = _ready(env, monkeypatch)
    assert _edition(env) == 0
    before = status.load(env.data_dir, reader)

    # same (reader, date, privacy) without --force: the build raises, rc 2
    assert _edition(env) == 2
    doc = status.load(env.data_dir, reader)
    assert doc["consecutive_failures"] == 1
    assert doc["last_fail"] and doc["fail_reason"].startswith("FileExistsError")
    # the previous edition stays the reader's current one
    assert doc["built_at"] == before["built_at"] == doc["last_ok"]
    assert doc["edition_id"] == before["edition_id"]
    assert doc["new_items"] is None and doc["llm_calls"] is None
    assert "skipped" not in doc

    # a successful forced rebuild resets the streak; the last failure is kept
    assert _edition(env, "--force") == 0
    doc = status.load(env.data_dir, reader)
    assert doc["consecutive_failures"] == 0
    assert datetime.fromisoformat(doc["built_at"]) >= datetime.fromisoformat(before["built_at"])
    assert doc["built_at"] == doc["last_ok"]
    assert doc["last_fail"] and doc["fail_reason"]  # history, not current health


def test_status_failure_on_unexpected_exception_reraises(env, monkeypatch):
    import gaceta.cli as cli_mod

    reader = _ready(env, monkeypatch)
    assert _edition(env) == 0
    before = status.load(env.data_dir, reader)

    def boom(*a, **kw):
        raise RuntimeError("feed fetch died at https://feed.example/p?sig=1")

    monkeypatch.setattr(cli_mod, "build_edition", boom)
    with pytest.raises(RuntimeError):
        _edition(env)
    doc = status.load(env.data_dir, reader)
    assert doc["consecutive_failures"] == 1
    assert "?sig=" not in doc["fail_reason"]
    assert "https://feed.example/p" in doc["fail_reason"]
    assert doc["built_at"] == before["built_at"]  # unchanged: nothing was built


# ------------------------------------------------------------ gaceta status

def test_status_cli_one_reader_and_all(env, monkeypatch, capsys):
    reader = _ready(env, monkeypatch)
    assert _edition(env) == 0
    capsys.readouterr()  # drop the edition's own line

    assert cli_main(["status", "--reader", reader, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["reader"] == reader
    assert set(doc) == SCHEMA | {"age_s"}
    assert isinstance(doc["age_s"], int) and doc["age_s"] >= 0

    assert cli_main(["status", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert list(rows) == [reader] and rows[reader]["reader"] == reader

    assert cli_main(["status"]) == 0
    line = capsys.readouterr().out
    assert f"reader={reader}" in line and "ok" in line


def test_status_cli_read_only_and_failures(env, monkeypatch, capsys):
    reader = _ready(env, monkeypatch)
    assert _edition(env) == 0
    assert _edition(env) == 2  # no --force: recorded failure
    capsys.readouterr()  # drop the edition's own lines

    assert cli_main(["status", "--reader", reader, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["consecutive_failures"] == 1 and doc["age_s"] >= 0

    assert cli_main(["status"]) == 0
    line = capsys.readouterr().out
    assert "consecutive_failures=1" in line and "reason=FileExistsError" in line

    # read-only and honest: unknown readers and bad slugs refuse, no file is made
    assert cli_main(["status", "--reader", "ghost"]) == 2
    assert "no status file" in capsys.readouterr().err
    assert cli_main(["status", "--reader", "../evil"]) == 2
    assert not (env.data_dir / "status").exists() or \
        "ghost" not in status.load_all(env.data_dir)


def test_status_cli_empty_data_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GACETA_DATA", str(tmp_path))
    assert cli_main(["status"]) == 0
    assert "no reader status" in capsys.readouterr().out
    assert cli_main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {}

