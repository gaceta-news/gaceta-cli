"""The finite manual trial (P3): consent before writes, ONE run per local
day, at most THREE runs across the seven-day window (the fourth refuses on
any day), durable lock + reservations that are never refunded, bounded fake
providers only, isolation, and the crash paths. Everything runs on the module
API with an explicit clock (`now`), so no test touches the real wall clock.
The CLI wiring is covered end-to-end through cli.main with GACETA_DATA set.

No network anywhere: the trial reader's only feed is a local synthetic file and
both model providers are the deterministic fakes.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta

import pytest

from gaceta import cli, trial

# A clock the tests control: local noon of each trial day avoids midnight and
# DST edges regardless of the machine's timezone.
T0 = date(2026, 11, 19)


def at(day_offset: int = 0, hour: int = 12) -> float:
    return datetime.combine(T0 + timedelta(days=day_offset), time(hour)).timestamp()


def yes(_plan: str) -> bool:
    return True


def no(_plan: str) -> bool:
    return False


@pytest.fixture
def base(tmp_path):
    return tmp_path / "gaceta-data"


def _start(base, now=at(0), confirm=yes):
    return trial.start(base, None, now=now, confirm=confirm)


def _run(base, now=at(0), confirm=yes, **kw):
    return trial.run(base, None, now=now, confirm=confirm, **kw)


def _root(base, ident=None):
    trials = trial._list(base)
    assert trials, "no trials on disk"
    return trial.trials_dir(base) / (ident or trials[-1]["id"])


def _state(base):
    return trial._load_state(_root(base))


def _toml(base):
    return _root(base) / "readers" / trial.READER_ID / "reader.toml"


def _state_bytes(base):
    return open(trial._state_path(_root(base)), "rb").read()


def _dead_pid() -> int:
    """A real pid that is gone: spawn a child that exits immediately and reap it."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ------------------------------------------------------------------ start/preview

def test_preview_before_any_trial_writes_nothing(base):
    rep = trial.preview(base, now=at(0))
    assert rep["trial"] is None
    assert rep["start_would_create"]["runs_per_day"] == 1
    assert rep["start_would_create"]["runs_total"] == 3
    assert rep["start_would_create"]["days_total"] == 7
    assert not (base / "trials").exists(), "preview must not create anything"


def test_start_requires_consent_and_writes_nothing(base):
    plans = []

    def refuse(plan):
        plans.append(plan)
        return False

    with pytest.raises(trial.ConsentRefused, match="no consent"):
        trial.start(base, None, now=at(0), confirm=refuse)
    assert plans and "finite manual trial" in plans[0]
    assert not (base / "trials").exists(), "refused consent must leave no files"


def test_start_creates_isolated_synthetic_reader(base):
    rep = _start(base)
    root = trial.trials_dir(base) / rep["id"]
    assert sorted(p.name for p in root.iterdir()) == [
        "gaceta.db", "readers", "synthetic-feed.xml", "trial.json"]
    assert os.listdir(base) == ["trials"], "nothing outside trials/"
    state = trial._load_state(root)
    assert state["state"] == "active"
    assert state["start_day"] == datetime.fromtimestamp(at(0)).date().isoformat()
    toml = (root / "readers" / "pilot" / "reader.toml").read_text()
    assert '[embed]\nprovider = "fake"' in toml
    assert '[llm.editor]\nprovider = "fake"' in toml
    assert "url =" not in toml          # offline file feed only
    assert "schedule" not in toml       # no scheduling, ever
    conn = trial.db.connect(root / "gaceta.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM readers").fetchone()[0] == 0
    finally:
        conn.close()
    assert rep["last_day"] == (T0 + timedelta(days=6)).isoformat()


def test_start_refuses_while_a_live_trial_exists(base):
    _start(base)
    with pytest.raises(trial.TrialError, match="live trial already exists"):
        _start(base, now=at(1))
    assert len(trial._list(base)) == 1


# ------------------------------------------------------------------ run: the real pipeline

def test_run_is_the_real_pipeline_on_synthetic_data(base):
    _start(base)
    rep = _run(base)
    rec = rep["run"]
    assert rec["status"] == "done" and rec["run"] == 1
    with open(rec["snapshot"], encoding="utf-8") as f:
        ed = json.load(f)
    assert ed["reader"]["id"] == trial.READER_ID
    assert "next_update" not in ed, "trials never schedule"
    assert len(ed["portada"]) == 4 and ed["folio"]["items_total"] == 12
    assert all(s["summary"] for s in ed["stories"].values()), "fake chat wrote no cards"
    assert rec["html"] and os.path.exists(rec["html"])
    conn = trial.db.connect(_root(base) / "gaceta.db")
    try:
        groups = dict(conn.execute(
            "SELECT si.story_id, COUNT(*) FROM story_items si "
            "JOIN stories s ON s.id = si.story_id GROUP BY si.story_id").fetchall())
        assert sorted(groups.values()) == [3, 3, 3, 3], "4 synthetic stories of 3 items"
        assert conn.execute("SELECT COUNT(*) FROM story_versions").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM llm_decisions").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 1
    finally:
        conn.close()
    assert rec["llm_calls"] == 4 and rec["budget_exhausted"] is False


def test_second_run_same_day_refuses_and_writes_nothing(base):
    _start(base)
    _run(base)
    before = _state_bytes(base)
    with pytest.raises(trial.TrialError, match=r"1/1 runs already spent"):
        _run(base)
    assert _state_bytes(base) == before, "a refused run must write nothing"


def test_next_day_run_is_an_honest_near_noop(base):
    _start(base)
    _run(base, now=at(0))
    rep = _run(base, now=at(1))
    assert rep["run"]["run"] == 2
    assert rep["run"]["items"] == 0          # the synthetic feed is static
    assert rep["run"]["written"] == 0         # nothing changed → no rewrite
    assert rep["run"]["llm_calls"] == 0       # cached decisions stay free
    assert rep["runs_left"] == 1


# ------------------------------------------------------------------ caps: 1/day, 3 total, no fourth

def test_three_days_then_no_fourth_on_any_day(base):
    _start(base)
    for i in range(3):                        # days 1, 2, 3: one run each
        rep = _run(base, now=at(i))
        assert rep["run"]["run"] == i + 1 and rep["run"]["status"] == "done"
    before = _state_bytes(base)
    for off in (3, 5, 6):                     # day 4, 6, 7: fresh days buy nothing
        with pytest.raises(trial.TrialError, match="no fourth run"):
            _run(base, now=at(off))
    assert _state_bytes(base) == before, "a refused run must write nothing"
    prev = trial.preview(base, now=at(4))
    assert "no fourth run" in prev["would_run"]
    assert prev["runs_left"] == 0


def test_missed_days_are_neither_backfilled_nor_granted(base):
    _start(base)
    assert _run(base, now=at(0))["run"]["run"] == 1   # day 1
    # days 2-3 missed: no debt, no backfill, nothing granted
    assert _run(base, now=at(3))["run"]["run"] == 2   # day 4: fresh day, run 2
    assert _run(base, now=at(6))["run"]["run"] == 3   # day 7 (the last): run 3
    with pytest.raises(trial.TrialError, match="no fourth run"):
        _run(base, now=at(6))
    with pytest.raises(trial.TrialError, match="no fourth run"):
        _run(base, now=at(3))                  # an earlier unused day reopens nothing


def test_one_run_per_day_even_on_a_fresh_late_day(base):
    _start(base)                               # days 1-5 untouched
    rep = _run(base, now=at(5))
    assert rep["run"]["run"] == 1
    with pytest.raises(trial.TrialError, match=r"1/1 runs already spent"):
        _run(base, now=at(5))                  # unused days grant no extra runs


def test_run_cap_survives_the_lock_and_a_crash_on_the_last_run(base):
    _start(base)
    _run(base, now=at(0))
    _run(base, now=at(1))
    _simulate_hard_kill(base, at(2))           # run 3 reserved, lock stale
    with pytest.raises(trial.TrialError, match="no fourth run"):
        _run(base, now=at(3), recover_lock=True)   # even recovery buys no fourth
    assert trial._lock_path(_root(base)).exists(), "refusal must not touch the lock"
    trial.keep(base, None, now=at(3), confirm=yes)  # still closes out honestly
    assert _state(base)["state"] == "kept"


# ------------------------------------------------------------------ expiry

def test_seven_day_expiry_even_with_all_runs_unused(base):
    _start(base, now=at(0))
    rep = trial.run(base, None, now=at(6), confirm=yes)   # day 7: still valid
    assert rep["run"]["status"] == "done"
    with pytest.raises(trial.TrialError, match="expired"):
        trial.run(base, None, now=at(7), confirm=yes)     # day 8: dead
    plans = []
    with pytest.raises(trial.TrialError, match="expired"):
        trial.run(base, None, now=at(9, hour=9),
                 confirm=lambda p: (plans.append(p), True)[1])
    assert plans == [], "an expired run must not even ask for consent"


def test_expiry_refusal_writes_nothing(base):
    _start(base, now=at(0))
    before = _state_bytes(base)
    with pytest.raises(trial.TrialError, match="expired"):
        trial.run(base, None, now=at(7) + 3600, confirm=yes)
    assert _state_bytes(base) == before


# ------------------------------------------------------------------ stop / keep / remove

def test_stop_is_final_and_needs_consent(base):
    _start(base)
    _run(base)
    with pytest.raises(trial.ConsentRefused):
        trial.stop(base, None, now=at(0), confirm=no)
    assert _state(base)["state"] == "active"
    trial.stop(base, None, now=at(1), confirm=yes)
    assert _state(base)["state"] == "stopped"
    with pytest.raises(trial.TrialError, match="already 'stopped'"):
        trial.stop(base, None, now=at(1), confirm=yes)
    with pytest.raises(trial.TrialError, match="'stopped'"):
        _run(base, now=at(1))
    with pytest.raises(trial.TrialError, match="'stopped'"):
        _run(base, now=at(9))                   # even after expiry: no resurrection


def test_keep_then_remove_requires_really_and_frees_the_id(base):
    _start(base)
    trial.keep(base, None, now=at(0), confirm=yes)
    assert _state(base)["state"] == "kept"
    with pytest.raises(trial.TrialError, match="'kept'"):
        _run(base, now=at(1))
    with pytest.raises(trial.TrialError, match="--really"):
        trial.remove(base, None, now=at(1), confirm=yes)
    assert _root(base).exists()
    root = _root(base)
    trial.remove(base, None, now=at(1), confirm=yes, really=True)
    assert not root.exists()
    rep = _start(base, now=at(1))               # the id is free again
    assert trial._load_state(trial.trials_dir(base) / rep["id"])["state"] == "active"


def test_remove_refuses_on_a_live_trial_stop_first(base):
    _start(base)
    with pytest.raises(trial.TrialError, match="still live"):
        trial.remove(base, None, now=at(0), confirm=yes)
    assert _root(base).exists()
    root = _root(base)
    trial.stop(base, None, now=at(0), confirm=yes)
    trial.remove(base, None, now=at(0), confirm=yes)
    assert not root.exists() and trial._list(base) == []


# ------------------------------------------------------------------ budgets

def test_budget_bounds_are_enforced_before_any_reservation(base):
    _start(base)
    for bad in ({"max_llm_calls": 61}, {"max_change_calls": 31},
                {"max_llm_calls": -1}):
        with pytest.raises(trial.TrialError, match="bounded budgets only"):
            _run(base, **bad)
    assert _state(base)["runs"] == []


def test_zero_budget_run_is_bounded_and_still_produces_an_edition(base):
    _start(base)
    rep = _run(base, max_llm_calls=0, max_change_calls=0)
    rec = rep["run"]
    assert rec["status"] == "done"
    assert rec["llm_calls"] == 0 and rec["budget_exhausted"] is True
    with open(rec["snapshot"], encoding="utf-8") as f:
        ed = json.load(f)
    assert len(ed["stories"]) == 4  # raw cards: a spent budget never blocks the paper


def test_recorded_calls_stay_within_the_caps(base):
    _start(base)
    for i in range(3):
        _run(base, now=at(i))                   # one run per day, default budgets
    state = _state(base)
    assert sum(r["llm_calls"] or 0 for r in state["runs"]) <= 3 * trial.DEFAULT_MAX_LLM_CALLS


def test_lifetime_call_bounds_hold_by_construction(base):
    assert trial.MAX_LIFETIME_LLM_CALLS == trial.MAX_RUNS_TOTAL * trial.MAX_LLM_CALLS_CAP
    assert trial.MAX_LIFETIME_CHANGE_CALLS == trial.MAX_RUNS_TOTAL * trial.MAX_CHANGE_CALLS_CAP
    _start(base)
    for i in range(trial.MAX_RUNS_TOTAL):      # three runs at the hard caps
        _run(base, now=at(i), max_llm_calls=trial.MAX_LLM_CALLS_CAP,
             max_change_calls=trial.MAX_CHANGE_CALLS_CAP)
    rep = trial.status(base, now=at(3))
    assert rep["totals"]["llm_calls"] <= trial.MAX_LIFETIME_LLM_CALLS
    assert rep["totals"]["change_calls"] <= trial.MAX_LIFETIME_CHANGE_CALLS
    with pytest.raises(trial.TrialError, match="no fourth run"):
        _run(base, now=at(4))                   # the run cap is what bounds the lifetime


# ------------------------------------------------------------------ policy: fake + offline only

def test_config_tampering_fails_closed_without_spending_a_run(base):
    _start(base)
    text = _toml(base).read_text()
    tampers = {
        "real llm provider": text.replace(
            '[llm.editor]\nprovider = "fake"',
            '[llm.editor]\nprovider = "openai"\nbase_url = "http://x/v1"\napi_key_env = "KEY"'),
        "real embed provider": text.replace(
            '[embed]\nprovider = "fake"',
            '[embed]\nprovider = "ollama"\nbase_url = "http://localhost:11434"'),
        "network feed": text.replace(
            'file = "synthetic-feed.xml"',
            'url = "https://example.com/feed.xml"'),
        "scheduling": text + '\n[schedule]\nevery_hours = 2\n',
        "transcripts": text + '\n[sources.rss.transcripts]\nmode = "offline"\n',
        "llm confirm": text.replace('threshold = 0.70', 'threshold = 0.70\nconfirm = true'),
    }
    for name, bad in tampers.items():
        assert bad != text, f"tamper {name} did not change the config"
        _toml(base).write_text(bad)
        with pytest.raises(trial.TrialError, match="trial config refused"):
            _run(base)
        assert _state(base)["runs"] == [], f"{name}: a refused run must spend nothing"
        assert not trial._lock_path(_root(base)).exists()
    _toml(base).write_text(text)
    assert _run(base)["run"]["status"] == "done"   # restored → runs again


# ------------------------------------------------------------------ crash semantics

def test_failed_run_spends_the_day_and_the_next_day_continues(base, monkeypatch):
    _start(base)

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(trial, "_pipeline", boom)
    with pytest.raises(trial.TrialError, match="failed after it was spent"):
        _run(base)
    rec = _state(base)["runs"][0]
    assert rec["status"] == "failed" and "boom" in rec["error"]
    assert not trial._lock_path(_root(base)).exists(), "a caught failure releases the lock"
    with pytest.raises(trial.TrialError, match=r"1/1 runs already spent"):
        _run(base)                        # a failed run is not refunded today
    monkeypatch.undo()
    rep = _run(base, now=at(1))           # day 2: run 2 proceeds normally
    assert rep["run"]["run"] == 2 and rep["run"]["status"] == "done"


def test_interrupted_run_keeps_its_reservation_without_the_lock(base, monkeypatch):
    _start(base)

    def interrupt(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(trial, "_pipeline", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _run(base)
    rec = _state(base)["runs"][0]
    assert rec["status"] == "reserved"          # durable: the run is spent
    assert not trial._lock_path(_root(base)).exists()  # finally released it
    with pytest.raises(trial.TrialError, match=r"1/1 runs already spent"):
        _run(base)                              # the interruption bought nothing back
    monkeypatch.undo()
    rep = _run(base, now=at(1))
    assert rep["run"]["run"] == 2               # ...so day 2 is run 2 of 3


def _simulate_hard_kill(base, now):
    """Leave exactly what a SIGKILL mid-run leaves: a reserved record plus a
    lock whose pid is really dead (fork+reap gives a genuine dead pid)."""
    root = _root(base)
    state = trial._load_state(root)
    state["runs"].append(trial._new_record(len(state["runs"]) + 1,
                                           trial._local_day(now), now,
                                           trial.DEFAULT_MAX_LLM_CALLS,
                                           trial.DEFAULT_MAX_CHANGE_CALLS))
    trial._save_state(root, state)
    trial._save_json(trial._lock_path(root),
                     {"pid": _dead_pid(), "host": trial._HOST, "at": int(now)})


def test_hard_crash_leaves_a_stale_lock_that_needs_consented_recovery(base):
    _start(base)
    _simulate_hard_kill(base, at(0))            # run 1 reserved, dead-pid lock
    with pytest.raises(trial.TrialError, match=r"1/1 runs already spent"):
        _run(base, now=at(0))                    # same day: already spent, lock untouched
    assert trial._lock_path(_root(base)).exists()
    with pytest.raises(trial.TrialError, match="--recover-lock"):
        _run(base, now=at(1))                    # next day: the stale lock blocks
    assert trial._lock_path(_root(base)).exists(), "refusal must not touch the lock"
    rep = _run(base, now=at(1), recover_lock=True)
    assert rep["run"]["run"] == 2, "the killed run keeps its count: no refund"
    assert rep["run"]["status"] == "done"


def test_live_lock_from_a_running_process_blocks_the_run(base):
    _start(base)
    trial._save_json(trial._lock_path(_root(base)),
                     {"pid": os.getpid(), "host": trial._HOST, "at": int(at(0))})
    with pytest.raises(trial.TrialError, match="in progress"):
        _run(base, now=at(0))
    assert trial._lock_path(_root(base)).exists()


# ------------------------------------------------------------------ status/preview read-only

def test_status_and_preview_are_read_only_and_honest(base):
    _start(base)
    _run(base)
    path = trial._state_path(_root(base))
    before = path.read_bytes()
    rep = trial.status(base, now=at(0))
    assert rep["today"]["used"] == 1 and rep["today"]["cap"] == 1
    assert rep["runs_total"] == 1 and rep["runs_cap"] == 3 and rep["runs_left"] == 2
    assert rep["day_number"] == 1 and rep["last_day"] == (T0 + timedelta(days=6)).isoformat()
    assert rep["totals"]["llm_calls"] == 4
    assert rep["totals"]["llm_calls_lifetime_cap"] == trial.MAX_LIFETIME_LLM_CALLS
    assert rep["totals"]["change_calls_lifetime_cap"] == trial.MAX_LIFETIME_CHANGE_CALLS
    assert len(rep["snapshots"]) == 1
    prev = trial.preview(base, now=at(0))
    assert prev["would_run"] == "day cap reached: 1/1 runs spent on " \
        + datetime.fromtimestamp(at(0)).date().isoformat()
    assert path.read_bytes() == before, "status/preview must not write"
    prev2 = trial.preview(base, now=at(1))
    assert prev2["would_run"] == "ok"          # day 2: run 2 is allowed
    prev3 = trial.preview(base, now=at(7))
    assert "expired" in prev3["would_run"]


# ------------------------------------------------------------------ isolation

def test_the_main_data_area_is_untouched(base):
    _start(base)
    _run(base)
    assert os.listdir(base) == ["trials"]
    assert not (base / "gaceta.db").exists()
    assert not (base / "readers").exists()


# ------------------------------------------------------------------ CLI wiring

def test_cli_consent_flow_and_all_actions(monkeypatch, capsys, base):
    monkeypatch.setenv("GACETA_DATA", str(base))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # not a tty

    assert cli.main(["trial", "preview"]) == 0
    out = capsys.readouterr().out
    assert "no trial yet" in out and "1 run/day" in out and "3 runs total" in out

    assert cli.main(["trial", "start"]) == 2          # no --yes, no consent
    assert not (base / "trials").exists()
    assert "--yes" in capsys.readouterr().err

    assert cli.main(["trial", "start", "--yes", "--id", "alpha"]) == 0
    assert (base / "trials" / "alpha" / "trial.json").exists()

    assert cli.main(["trial", "status"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "runs: 0/3 spent" in out

    assert cli.main(["trial", "preview"]) == 0
    out = capsys.readouterr().out
    assert "next run: gaceta trial run --yes" in out and "run today" in out

    assert cli.main(["trial", "run", "--yes", "--max-llm-calls", "61"]) == 2
    assert "bounded budgets only" in capsys.readouterr().err

    assert cli.main(["trial", "run", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "run 1/3" in out and "edition 4 stories" in out
    assert "runs left: 2/3" in out

    assert cli.main(["trial", "run", "--yes"]) == 2   # same day: refused
    assert "1/1 runs already spent" in capsys.readouterr().err

    assert cli.main(["trial", "stop", "--yes"]) == 0
    assert cli.main(["trial", "run", "--yes"]) == 2
    assert "'stopped'" in capsys.readouterr().err

    assert cli.main(["trial", "keep", "--yes"]) == 0
    assert cli.main(["trial", "remove", "--yes"]) == 2
    assert "--really" in capsys.readouterr().err
    assert cli.main(["trial", "remove", "--yes", "--really"]) == 0
    assert not (base / "trials" / "alpha").exists()


class _Tty(io.StringIO):
    def isatty(self):
        return True


def test_cli_tty_prompt_accepts_a_typed_yes(monkeypatch, base):
    monkeypatch.setenv("GACETA_DATA", str(base))
    monkeypatch.setattr(sys, "stdin", _Tty("yes\n"))
    assert cli.main(["trial", "start", "--id", "beta"]) == 0
    assert (base / "trials" / "beta" / "trial.json").exists()


def test_cli_tty_prompt_refuses_a_typed_no(monkeypatch, base):
    monkeypatch.setenv("GACETA_DATA", str(base))
    monkeypatch.setattr(sys, "stdin", _Tty("no\n"))
    assert cli.main(["trial", "start", "--id", "gamma"]) == 2
    assert not (base / "trials").exists()
