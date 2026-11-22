"""Finite manual trial of the paper pipeline (P3; docs/release/trial.md).

A trial is a bounded, consent-gated, fully manual way to watch the REAL
pipeline run on data that never leaves this machine:

    gaceta trial preview     read-only: what exists, what start/run would do
    gaceta trial start       create the trial (consent required)
    gaceta trial run         one real pipeline pass = today's one run (consent)
    gaceta trial status      read-only report: runs of ≤3, day of 7, lock
    gaceta trial stop        end the trial's runs, final (consent required)
    gaceta trial keep        preserve the artifacts for the record (consent)
    gaceta trial remove      delete the trial (consent; stop first; --really if kept)

Contract (every line is enforced by the code, not by convention):

- Real pipeline, synthetic input: ingest → embed → cluster → write → edition →
  render, the same functions gaceta runs daily — over an isolated synthetic
  reader ('pilot') inside <data>/trials/<id>/, with its own gaceta.db. The main
  database, readers and editions are never touched.
- Offline and uncredentialed, verified before every run: one file feed written
  by start() (every link points at the reserved .invalid domain), [embed] and
  [llm.editor] pinned to the deterministic fake providers, and anything else
  (url feeds, transcripts, api_key_env/base_url, [schedule], [conditions],
  plugins, a reader profile) is refused before a run is spent.
- Bounded: ONE run per local calendar day, at most THREE runs in the trial's
  whole life across its 7-calendar-day window (start day included) — the
  fourth run refuses on any day, forever. No catchup: unused runs never carry
  to another day and missed days cannot be backfilled. Per-run LLM budgets
  are hard-capped, so lifetime fake-chat calls are bounded by construction.
- Consent before writes: every write action prints its plan first and needs
  --yes (or an interactive 'yes'). preview/status write nothing at all.
- Durable reservations: a run is spent by an fsynced state write BEFORE the
  pipeline starts, so a run that fails, is interrupted (Ctrl-C) or is killed
  still consumes its day and one of the three. A killed run leaves run.lock
  behind (pid + host); the next run refuses until --recover-lock clears it,
  and recovery never refunds the interrupted run.
- No scheduling: nothing here starts timers, cron or daemons, the trial config
  carries no [schedule], and an edition that comes back with next_update is
  refused. Every step is a person typing a command.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from . import config, db

# --- the bounds (the trial's whole point; nothing may exceed these) ----------
RUNS_PER_DAY = 1           # runs per local calendar day: one, never more
MAX_RUNS_TOTAL = 3         # whole-trial cap: there is no fourth run, on any day
TRIAL_DAYS = 7             # calendar days incl. the start day; then expired
DEFAULT_MAX_LLM_CALLS = 12 # per run, write-ups (fake chat; the cap below rules)
MAX_LLM_CALLS_CAP = 60
DEFAULT_MAX_CHANGE_CALLS = 6  # per run, 'what changed' notes
MAX_CHANGE_CALLS_CAP = 30
MAX_LIFETIME_LLM_CALLS = MAX_RUNS_TOTAL * MAX_LLM_CALLS_CAP        # ≤ 180
MAX_LIFETIME_CHANGE_CALLS = MAX_RUNS_TOTAL * MAX_CHANGE_CALLS_CAP  # ≤ 90
WINDOW_DAYS = 14           # write/changes/edition recency window (repo default)

# The lifetime call bounds hold by construction: at most MAX_RUNS_TOTAL runs,
# each hard-capped by MAX_LLM_CALLS_CAP / MAX_CHANGE_CALLS_CAP, so the whole
# trial can never spend more fake-chat calls than the two numbers above.

READER_ID = "pilot"
FEED_FILE = "synthetic-feed.xml"
DB_FILE = "gaceta.db"
STATE_FILE = "trial.json"
LOCK_FILE = "run.lock"
STATE_VERSION = 1
_HOST = socket.gethostname()


class TrialError(Exception):
    """Any refusal: bad state, policy violation, missing trial, no consent."""


class ConsentRefused(TrialError):
    """Consent was not given; nothing was written."""


# ---------------------------------------------------------------- state + io

def trials_dir(base) -> Path:
    return Path(base) / "trials"


def _root_of(base, ident: str) -> Path:
    return trials_dir(base) / ident


def _state_path(root: Path) -> Path:
    return root / STATE_FILE


def _lock_path(root: Path) -> Path:
    return root / LOCK_FILE


def _local_date(now: float) -> date:
    return datetime.fromtimestamp(now).date()


def _local_day(now: float) -> str:
    return _local_date(now).isoformat()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    _fsync_dir(path.parent)


def _save_json(path: Path, data: dict) -> None:
    """Durable JSON write: fsync the file, rename, fsync the directory."""
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _load_state(root: Path) -> dict | None:
    path = _state_path(root)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TrialError(f"trial state unreadable at {path}: {e}") from None
    if not isinstance(state, dict):
        raise TrialError(f"trial state unreadable at {path} (not a JSON object)")
    for key in ("id", "started_at", "start_day", "state", "runs"):
        if key not in state:
            raise TrialError(f"trial state at {path} is missing {key!r} (corrupt?)")
    if state["state"] not in ("active", "stopped", "kept"):
        raise TrialError(f"trial state at {path} has unknown state {state['state']!r}")
    state.setdefault("version", STATE_VERSION)
    state.setdefault("stopped_at", None)
    state.setdefault("kept_at", None)
    state["root"] = str(root)
    return state


def _save_state(root: Path, state: dict) -> None:
    state = {k: v for k, v in state.items() if k != "root"}  # root is derived
    _save_json(_state_path(root), state)


def _list(base) -> list[dict]:
    tdir = trials_dir(base)
    if not tdir.is_dir():
        return []
    out = []
    for d in sorted(tdir.iterdir()):
        if not d.is_dir():
            continue
        state = _load_state(d)  # a half-created dir (no trial.json) is skipped
        if state is not None:
            out.append(state)
    out.sort(key=lambda s: (s["started_at"], s["id"]))
    return out


def _resolve(base, trial_id: str | None, now: float) -> tuple[Path, dict]:
    trials = _list(base)
    if trial_id:
        state = next((s for s in trials if s["id"] == trial_id), None)
        if state is None:
            known = ", ".join(s["id"] for s in trials) or "none"
            raise TrialError(f"no trial {trial_id!r} (existing: {known})")
        return _root_of(base, trial_id), state
    active = [s for s in trials if s["state"] == "active"]
    state = active[-1] if active else (trials[-1] if trials else None)
    if state is None:
        raise TrialError("no trial yet — start one: gaceta trial start --yes")
    return _root_of(base, state["id"]), state


# ---------------------------------------------------------------- trial clock

def _last_day(state: dict) -> date:
    return date.fromisoformat(state["start_day"]) + timedelta(days=TRIAL_DAYS - 1)


def _expired(state: dict, now: float) -> bool:
    return _local_date(now) > _last_day(state)


def _day_number(state: dict, now: float) -> int:
    return (_local_date(now) - date.fromisoformat(state["start_day"])).days + 1


def _used_today(state: dict, day: str) -> list[dict]:
    return [r for r in state["runs"] if r.get("day") == day]


def _day_view(state: dict, day: str) -> dict:
    used = len(_used_today(state, day))
    return {"day": day, "used": used, "cap": RUNS_PER_DAY,
            "left": max(0, RUNS_PER_DAY - used)}


def _runs_left(state: dict) -> int:
    return max(0, MAX_RUNS_TOTAL - len(state["runs"]))


# ---------------------------------------------------------------- consent

def _require_consent(confirm: Callable[[str], bool] | None, plan: str) -> None:
    """The gate before every write: the caller prints the plan, the human says yes."""
    if confirm is None or not confirm(plan):
        raise ConsentRefused("no consent — nothing was written (pass --yes, or answer the prompt)")


# ---------------------------------------------------------------- lock

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True  # unknown: assume alive, the safe direction
    return True


def _read_lock(root: Path) -> dict | None:
    try:
        info = json.loads(_lock_path(root).read_text(encoding="utf-8"))
        return info if isinstance(info, dict) else None
    except (OSError, ValueError):
        return None


def _acquire_lock(root: Path, now: int, *, recover: bool) -> None:
    """O_EXCL lock: atomic to take, durable across crashes, never auto-removed."""
    lock = _lock_path(root)
    info = {"pid": os.getpid(), "host": _HOST, "at": now}
    for _attempt in (1, 2):  # second attempt only after a --recover-lock unlink
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            prev = _read_lock(root)
            if prev and prev.get("host") == _HOST and _pid_alive(int(prev.get("pid") or 0)):
                raise TrialError(
                    f"another trial run is in progress (pid {prev.get('pid')}, "
                    f"lock {lock}) — wait for it; never delete a live lock") from None
            if not recover:
                raise TrialError(
                    f"a lock from a crashed run is present ({lock}); its run was "
                    "already spent. Rerun with --recover-lock to clear it — recovery "
                    "does not refund the interrupted run") from None
            try:
                os.unlink(lock)
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(info, f)
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(root)
        return
    raise TrialError(f"lost the race to recover the lock ({lock}); try again")


def _release_lock(root: Path) -> None:
    try:
        os.unlink(_lock_path(root))
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- policy

_FORBIDDEN_EMBED_KEYS = ("base_url", "api_key_env", "model")
_FORBIDDEN_LLM_KEYS = ("base_url", "api_key_env", "model", "extra_body")


def _verify_config(cfg: dict, root: Path) -> None:
    """Fail closed: a trial only ever runs the offline synthetic reader with the
    deterministic fake providers. Anything else — a network feed, a real model,
    a schedule, a plugin — is refused BEFORE a run is spent."""
    problems: list[str] = []
    if cfg.get("id") != READER_ID:
        problems.append(f"reader id must be {READER_ID!r}")
    sources = cfg.get("sources") or {}
    for extra in sorted(set(sources) - {"rss"}):
        problems.append(f"[sources.{extra}] not allowed in a trial")
    rss = sources.get("rss") or {}
    if rss.get("transcripts"):
        problems.append("[sources.rss.transcripts] not allowed (transcripts use the network)")
    feeds = rss.get("feeds") or []
    if len(feeds) != 1:
        problems.append("exactly one [[sources.rss.feeds]] expected")
    else:
        feed = feeds[0]
        if feed.get("url"):
            problems.append("feed has url= (trials are offline: file feeds only)")
        if not feed.get("file"):
            problems.append("feed has no file=")
        else:
            resolved = (root / str(feed["file"])).resolve()
            if root.resolve() not in resolved.parents:
                problems.append("feed file must live inside the trial dir")
            elif not resolved.is_file():
                problems.append(f"feed file missing: {resolved}")
        if str(feed.get("kind") or "rss").lower() != "rss":
            problems.append("feed kind must be 'rss' (no video transcripts in trials)")
    embed = cfg.get("embed") or {}
    if embed.get("provider") != "fake":
        problems.append("[embed] provider must be 'fake'")
    for k in _FORBIDDEN_EMBED_KEYS:
        if k in embed:
            problems.append(f"[embed].{k} not allowed (fake provider only)")
    llm = (cfg.get("llm") or {}).get("editor") or {}
    if llm.get("provider") != "fake":
        problems.append("[llm.editor] provider must be 'fake' (no paid inference)")
    for k in _FORBIDDEN_LLM_KEYS:
        if k in llm:
            problems.append(f"[llm.editor].{k} not allowed (no endpoints, no credentials)")
    if llm.get("allow_private") is not False:
        problems.append("[llm.editor] allow_private must be false (model boundary)")
    if (cfg.get("cluster") or {}).get("confirm"):
        problems.append("[cluster].confirm not allowed (LLM confirm stays off in trials)")
    for key, why in (("schedule", "trials never schedule"),
                     ("conditions", "plugin condition boxes stay off"),
                     ("reader", "no reader profile in trials")):
        if key in cfg:
            problems.append(f"[{key}] not allowed ({why})")
    if problems:
        raise TrialError("trial config refused: " + "; ".join(problems))


def _check_budgets(max_llm_calls: int, max_change_calls: int) -> None:
    problems = []
    if not isinstance(max_llm_calls, int) or not (0 <= max_llm_calls <= MAX_LLM_CALLS_CAP):
        problems.append(f"--max-llm-calls must be 0..{MAX_LLM_CALLS_CAP} (got {max_llm_calls})")
    if not isinstance(max_change_calls, int) or not (0 <= max_change_calls <= MAX_CHANGE_CALLS_CAP):
        problems.append(f"--max-change-calls must be 0..{MAX_CHANGE_CALLS_CAP} (got {max_change_calls})")
    if problems:
        raise TrialError("bounded budgets only: " + "; ".join(problems))


# ---------------------------------------------------------------- synthetic data

# Four groups of three items each. The shared core sentence makes same-group
# items cluster together with the fake embedder (cosine well above 0.70); group
# vocabularies are deliberately disjoint — core nouns/verbs AND tail frames,
# with almost no shared function words — so different groups stay far apart.
# Every link points at the reserved .invalid domain: nothing here is fetchable,
# nothing is a real source.
SYNTHETIC_TOPICS = (
    ("harbour", "Harbour lamps pass winter storm test",
     "Harbour lamps passed winter's worst storm test on north dock; keepers logged "
     "each reading through that frozen week.",
     ("Night log shows glass dry.",
      "Pier log shows joint cool.",
      "Storm log shows vane bent.")),
    ("orchard", "Orchard grafts survive late frost",
     "Orchard grafts survived late frost; growers catalogued each apple variety that "
     "fruited after that cold night.",
     ("Tally sheet marks early fruit ready.",
      "Tally sheet marks late fruit ready.",
      "Tally sheet marks sweet fruit ready.")),
    ("tram", "Tram signals keep morning line running",
     "Tram signals kept morning line running; depot crews recorded each switch fault "
     "through that snowy week.",
     ("Depot slate notes switch fault clean.",
      "Depot slate notes signal fault clean.",
      "Depot slate notes rail fault clean.")),
    ("bells", "Tower bells ring noon across old square",
     "Tower bells rang noon across old square; sexton noted each strike that echoed "
     "over rooftops.",
     ("Sexton page counts noon strike clear.",
      "Sexton page counts noon echo clear.",
      "Sexton page counts noon chime clear.")),
)
_SYNTH_WORDS = ("one", "two", "three")


def synthetic_feed_xml(now: int) -> str:
    from email.utils import formatdate

    items = []
    n = 0
    for slug, title, core, tails in SYNTHETIC_TOPICS:
        for i, tail in enumerate(tails, 1):
            n += 1
            items.append(
                f"<item><title>{title} — note {_SYNTH_WORDS[i - 1]}</title>"
                f"<link>https://synthetic.invalid/{slug}/{i}</link>"
                f"<guid>{slug}-{i}</guid>"
                f"<pubDate>{formatdate(now - n * 3600, usegmt=True)}</pubDate>"
                f"<description>{core} {tail}</description>"
                f"<author>{slug}-desk@synthetic.invalid</author></item>"
            )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<rss version=\"2.0\"><channel>\n"
        "<title>Synthetic trial feed</title>\n"
        "<link>https://synthetic.invalid/</link>\n"
        "<description>Twelve synthetic items in four groups, written by gaceta trial "
        "start. No real source, no network: every link points at the reserved "
        ".invalid domain.</description>\n"
        + "\n".join(items) + "\n"
        "</channel></rss>\n"
    )


TRIAL_READER_TOML = f"""# gaceta trial reader: synthetic, offline, fake models only.
# Generated by `gaceta trial start`. The trial verifies this config before every
# run and refuses anything not fake/offline — edit it and runs stop, by design.
# The bounds live in the trial state (trial.json), not here.

id = "{READER_ID}"
name = "Trial pilot (synthetic)"
langs = "en"

[privacy]
pinned = []
household = []

[sources.rss]
max_items = 24
max_age_days = 14

[[sources.rss.feeds]]
id = "synthetic"
name = "Synthetic trial feed"
file = "{FEED_FILE}"

[embed]
provider = "fake"

[llm.editor]
provider = "fake"
allow_private = false

[cluster]
threshold = 0.70
window_days = 21
"""


# ---------------------------------------------------------------- pipeline

def _pipeline(root: Path, cfg: dict, now: int,
              max_llm_calls: int, max_change_calls: int) -> dict:
    """One real pipeline pass over the synthetic reader. Same functions the
    daily CLI uses: ingest → embed → cluster → write → edition → render."""
    from .edition import build_edition, snapshot_path
    from .editor import cluster as run_cluster
    from .editor.changes import note_changes
    from .editor.cluster import embed_items
    from .editor.writeup import write_stories
    from .ingest import ingest
    from .providers.embed import from_config as embedder_from_config
    from .render import html as render_html
    from .sources.rss import RssAdapter

    rss = dict(cfg["sources"]["rss"])
    feeds = []
    for f in rss.get("feeds") or []:
        f = dict(f)
        if f.get("file"):  # same resolution rule as cli._ingest_rss, against the trial root
            f["file"] = str((root / str(f["file"])).resolve())
        feeds.append(f)
    rss["feeds"] = feeds
    adapter = RssAdapter(rss, transcript=None, now=lambda: int(now))
    conn = db.connect(root / DB_FILE)
    try:
        db.init(conn)
        ing = ingest(conn, adapter, cfg)
        if adapter.errors:
            raise TrialError(f"synthetic feed unreadable: {adapter.errors[0][1]}")
        emb = embed_items(conn, cfg["id"], embedder_from_config(cfg))
        clu = run_cluster.cluster(conn, cfg["id"], cfg)
        wr = write_stories(conn, cfg["id"], cfg, days=WINDOW_DAYS,
                           max_llm_calls=max_llm_calls, now=now)
        try:
            ch = note_changes(conn, cfg["id"], cfg, days=WINDOW_DAYS,
                             max_llm_calls=max_change_calls, now=now)
        except Exception as e:  # noqa: BLE001 — like cmd_write: retried next run, never fatal
            ch = {"error": f"{type(e).__name__}: {e}"}
        day = _local_day(now)
        edition = build_edition(conn, cfg["id"], cfg, date_str=day,
                                max_privacy="private", force=True,
                                data_dir=root, now=now)
        if "next_update" in edition:
            # defense in depth: [schedule] is refused at verification, and an
            # edition that claims a next slot would contradict 'no scheduling'
            raise TrialError("edition carries next_update — trials never schedule")
        html_path = None
        try:
            doc = render_html.render(edition, "private")
            site = root / "site" / cfg["id"]
            site.mkdir(parents=True, exist_ok=True)
            out = site / f"{day}.html"
            _atomic_write_text(out, doc)
            shutil.copyfile(out, site / "index.html")
            html_path = str(out)
        except Exception:  # noqa: BLE001 — the snapshot is the deliverable; HTML is courtesy
            html_path = None
        return {
            "items": ing["new"], "items_updated": ing["updated"],
            "embedded": emb["embedded"], "stories": clu["n_stories"],
            "written": wr["written"], "llm_calls": wr["llm_calls"],
            "llm_cache_hits": wr["llm_cache_hits"],
            "budget_exhausted": bool(wr.get("budget_exhausted")),
            "change_calls": int(ch.get("llm_calls") or 0) if "error" not in ch else 0,
            "change_note": ch.get("error"),
            "snapshot": str(snapshot_path(root, cfg["id"], day, "private")),
            "html": html_path, "edition_stories": len(edition["stories"]),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- run records

def _new_record(run: int, day: str, now: float, max_llm_calls: int,
                max_change_calls: int) -> dict:
    """A run record is created 'reserved' with every field present, then
    finalised in place: durable reservation first, outcome afterwards. The
    ordinal counts the trial's three, not just the day."""
    return {
        "run": run, "day": day, "at": int(now), "status": "reserved",
        "max_llm_calls": max_llm_calls, "max_change_calls": max_change_calls,
        "items": None, "items_updated": None, "embedded": None, "stories": None,
        "written": None, "llm_calls": None, "llm_cache_hits": None,
        "budget_exhausted": None, "change_calls": None, "change_note": None,
        "snapshot": None, "html": None, "edition_stories": None, "error": None,
    }


# ---------------------------------------------------------------- actions

def _check_runnable(state: dict, now: float) -> None:
    if state["state"] != "active":
        raise TrialError(f"trial is {state['state']!r} — runs refuse "
                         "(stop/keep is final; keep or remove the trial)")
    if _expired(state, now):
        raise TrialError(
            f"trial expired: the seven-day window {state['start_day']}.."
            f"{_last_day(state).isoformat()} ended. No catchup — unused runs "
            "never carry over and missed days cannot be backfilled. keep or "
            "remove the trial.")
    if len(state["runs"]) >= MAX_RUNS_TOTAL:
        raise TrialError(
            f"trial cap: {MAX_RUNS_TOTAL}/{MAX_RUNS_TOTAL} runs already spent — "
            "there is no fourth run, on any day (a fresh day buys nothing). "
            "keep or remove the trial.")


def _refusal_reason(state: dict, root: Path, now: float) -> str | None:
    """preview's non-raising twin of the run() checks (same order)."""
    if state["state"] != "active":
        return f"trial is {state['state']!r} — runs refuse"
    if _expired(state, now):
        return "trial expired (seven-day window ended; no catchup)"
    if len(state["runs"]) >= MAX_RUNS_TOTAL:
        return (f"trial cap: {MAX_RUNS_TOTAL}/{MAX_RUNS_TOTAL} runs already "
                "spent — no fourth run, on any day")
    day = _local_day(now)
    if len(_used_today(state, day)) >= RUNS_PER_DAY:
        return f"day cap reached: {RUNS_PER_DAY}/{RUNS_PER_DAY} runs spent on {day}"
    prev = _read_lock(root)
    if prev and prev.get("host") == _HOST and _pid_alive(int(prev.get("pid") or 0)):
        return f"a run is in progress (pid {prev.get('pid')})"
    if prev:
        return "a crashed run left a stale lock — rerun with --recover-lock (its slot stays spent)"
    return None


def _plan_run(state: dict, day: str, spent: int, root: Path, now: float,
              max_llm_calls: int, max_change_calls: int) -> str:
    return (
        f"trial {state['id']}: spend run {spent + 1}/{MAX_RUNS_TOTAL} on {day} "
        f"(day {_day_number(state, now)} of {TRIAL_DAYS}; one run per local day)\n"
        f"  real pipeline over synthetic data: ingest → embed (fake) → cluster → "
        f"write (fake chat, ≤{max_llm_calls} calls) → edition → render\n"
        f"  writes only inside {root}: {DB_FILE}, editions/{READER_ID}/{day}-private.json, "
        f"site/{READER_ID}/{day}.html\n"
        "  the run is spent even if it fails or is interrupted (durable "
        f"reservation); after {MAX_RUNS_TOTAL}/{MAX_RUNS_TOTAL} there is no fourth"
    )


def run(base, trial_id: str | None = None, *, now: float, confirm,
        max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
        max_change_calls: int = DEFAULT_MAX_CHANGE_CALLS,
        recover_lock: bool = False) -> dict:
    """One manual pipeline pass. Order matters: read-only checks → consent →
    lock → re-check under the lock → durable reservation → pipeline → outcome."""
    base = Path(base)
    root, state = _resolve(base, trial_id, now)
    _check_runnable(state, now)
    _check_budgets(max_llm_calls, max_change_calls)
    try:
        cfg = config.load_reader(READER_ID, base=root)
    except (OSError, ValueError) as e:
        raise TrialError(f"trial reader config unreadable: {e}") from None
    _verify_config(cfg, root)
    day = _local_day(now)
    used = _used_today(state, day)
    if len(used) >= RUNS_PER_DAY:
        raise TrialError(
            f"day cap: {RUNS_PER_DAY}/{RUNS_PER_DAY} runs already spent on {day}. "
            "No catchup — a day's run never repeats; the next one is another day.")
    _require_consent(confirm, _plan_run(state, day, len(state["runs"]), root, now,
                                       max_llm_calls, max_change_calls))
    _acquire_lock(root, int(now), recover=recover_lock)
    try:
        state = _load_state(root)  # reload under the lock: the truth on disk
        _check_runnable(state, now)  # re-checks state, expiry and the 3-run cap
        used = _used_today(state, day)
        if len(used) >= RUNS_PER_DAY:
            raise TrialError(f"another run raced in: {RUNS_PER_DAY}/{RUNS_PER_DAY} "
                             f"runs already spent on {day}")
        record = _new_record(len(state["runs"]) + 1, day, now,
                             max_llm_calls, max_change_calls)
        state["runs"].append(record)
        try:
            _save_state(root, state)  # the spend: durable before any work
        except OSError as e:
            state["runs"].pop()
            raise TrialError(f"could not record the reservation — run not spent: {e}") from None
        try:
            outcome = _pipeline(root, cfg, int(now), max_llm_calls, max_change_calls)
        except Exception as e:  # noqa: BLE001 — the run is spent either way
            record["status"] = "failed"
            record["error"] = f"{type(e).__name__}: {e}"
            try:
                _save_state(root, state)
            except OSError:
                pass
            raise TrialError(
                f"run failed after it was spent (it still counts against the "
                f"day and the three): "
                f"{type(e).__name__}: {e}") from None
        record.update(status="done", **outcome)
        _save_state(root, state)
    finally:
        # runs to completion or a caught failure release the lock; only a hard
        # kill (SIGKILL/power) leaves it behind, stale by its dead pid
        _release_lock(root)
    return {"id": state["id"], "root": str(root), "run": record,
            "today": _day_view(state, day), "runs_left": _runs_left(state),
            "outcome": outcome}


def _plan_start(ident: str, root: Path, day: str, last: str) -> str:
    return (
        f"trial {ident}: create a finite manual trial (nothing scheduled — every "
        "step is a command)\n"
        f"  root {root} — everything stays inside; the main gaceta.db and readers "
        "are never touched\n"
        f"  synthetic reader {READER_ID!r}: offline file feed + fake embedder + "
        "fake chat (no network, no keys, no paid inference)\n"
        f"  bounds: one run per local day, at most {MAX_RUNS_TOTAL} runs total, "
        f"window {day}..{last} ({TRIAL_DAYS} days, then expired; no fourth "
        "run, no catchup)\n"
        f"  writes: {STATE_FILE}, {FEED_FILE}, readers/{READER_ID}/reader.toml, {DB_FILE}"
    )


def start(base, trial_id: str | None = None, *, now: float, confirm) -> dict:
    base = Path(base)
    if trial_id is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}", trial_id):
        raise TrialError("trial id: 1-39 chars of a-z, 0-9 and '-' (first not '-')")
    live = [s for s in _list(base) if s["state"] == "active" and not _expired(s, now)]
    if live:
        raise TrialError(
            f"a live trial already exists ({live[-1]['id']}) — one at a time. "
            "Stop it (gaceta trial stop --yes), then keep or remove it.")
    ident = trial_id or "trial-" + datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    root = trials_dir(base) / ident
    n = 1
    while root.exists():
        n += 1
        root = trials_dir(base) / f"{ident}-{n}"
    ident = root.name
    day = _local_day(now)
    last = (date.fromisoformat(day) + timedelta(days=TRIAL_DAYS - 1)).isoformat()
    _require_consent(confirm, _plan_start(ident, root, day, last))
    root.mkdir(parents=True)
    _atomic_write_text(root / FEED_FILE, synthetic_feed_xml(int(now)))
    reader_dir = root / "readers" / READER_ID
    reader_dir.mkdir(parents=True)
    _atomic_write_text(reader_dir / "reader.toml", TRIAL_READER_TOML)
    conn = db.connect(root / DB_FILE)
    try:
        db.init(conn)
    finally:
        conn.close()
    state = {
        "version": STATE_VERSION, "id": ident, "reader": READER_ID,
        "started_at": int(now), "start_day": day, "state": "active",
        "stopped_at": None, "kept_at": None, "runs": [],
    }
    _save_state(root, state)  # the commit point: a valid trial exists from here
    _verify_config(config.load_reader(READER_ID, base=root), root)  # even our own output
    return {
        "id": ident, "root": str(root), "start_day": day, "last_day": last,
        "runs_per_day": RUNS_PER_DAY, "runs_total": MAX_RUNS_TOTAL,
        "days_total": TRIAL_DAYS,
        "created": [str(root / FEED_FILE), str(reader_dir / "reader.toml"),
                    str(root / DB_FILE), str(_state_path(root))],
    }


def _status_view(root: Path, state: dict, now: float) -> dict:
    day = _local_day(now)
    lock = _read_lock(root)
    lock_view = None
    if lock:
        lock_view = {"pid": lock.get("pid"), "at": lock.get("at"),
                     "stale": not (lock.get("host") == _HOST
                                   and _pid_alive(int(lock.get("pid") or 0)))}
    snapshots = sorted(str(p) for p in (root / "editions" / READER_ID).glob("*.json")) \
        if (root / "editions" / READER_ID).is_dir() else []
    return {
        "id": state["id"], "root": str(root), "state": state["state"],
        "start_day": state["start_day"], "last_day": _last_day(state).isoformat(),
        "expired": _expired(state, now),
        "day_number": _day_number(state, now) if not _expired(state, now) else None,
        "days_total": TRIAL_DAYS, "today": _day_view(state, day),
        "runs_total": len(state["runs"]), "runs_cap": MAX_RUNS_TOTAL,
        "runs_left": _runs_left(state), "runs": state["runs"],
        "interrupted": [r for r in state["runs"] if r["status"] == "reserved"],
        "failed": [r for r in state["runs"] if r["status"] == "failed"],
        "lock": lock_view, "snapshots": snapshots,
        "totals": {
            "llm_calls": sum(r["llm_calls"] or 0 for r in state["runs"]),
            "change_calls": sum(r["change_calls"] or 0 for r in state["runs"]),
            # bounded by construction: ≤ MAX_RUNS_TOTAL runs × the per-run caps
            "llm_calls_lifetime_cap": MAX_LIFETIME_LLM_CALLS,
            "change_calls_lifetime_cap": MAX_LIFETIME_CHANGE_CALLS,
        },
    }


def status(base, trial_id: str | None = None, *, now: float) -> dict:
    root, state = _resolve(base, trial_id, now)
    return _status_view(root, state, now)


def preview(base, trial_id: str | None = None, *, now: float) -> dict:
    """Read-only. With no trial it shows what start would create; with one it
    shows the state and exactly what the next run would do (or why not)."""
    base = Path(base)
    try:
        root, state = _resolve(base, trial_id, now)
    except TrialError:
        if trial_id:
            raise
        ident = "trial-" + datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        root = trials_dir(base) / ident
        return {
            "trial": None, "root": str(trials_dir(base)),
            "would_run": None,
            "start_would_create": {
                "id": ident, "root": str(root),
                "runs_per_day": RUNS_PER_DAY, "runs_total": MAX_RUNS_TOTAL,
                "days_total": TRIAL_DAYS,
                "files": [str(root / STATE_FILE), str(root / FEED_FILE),
                          str(root / "readers" / READER_ID / "reader.toml"),
                          str(root / DB_FILE)],
            },
            "hint": "gaceta trial start --yes",
        }
    rep = _status_view(root, state, now)
    reason = _refusal_reason(state, root, now)
    rep["would_run"] = "ok" if reason is None else reason
    rep["hint"] = ("gaceta trial run --yes" if reason is None else
                   "fix the above, or keep/remove the trial")
    return rep


def stop(base, trial_id: str | None = None, *, now: float, confirm) -> dict:
    """End the trial's runs. Final: there is no resume; a new trial needs
    remove (or keep + a new id)."""
    root, state = _resolve(base, trial_id, now)
    if state["state"] != "active":
        raise TrialError(f"trial is already {state['state']!r}")
    plan = (
        f"trial {state['id']}: stop — runs refuse from now on (final, no resume)\n"
        f"  remaining slots, including today's, are voided: no catchup, no restart\n"
        f"  files stay in place under {root}; keep or remove when ready")
    _require_consent(confirm, plan)
    state["state"] = "stopped"
    state["stopped_at"] = int(now)
    _save_state(root, state)
    return {"id": state["id"], "root": str(root), "state": "stopped",
            "stopped_at": state["stopped_at"]}


def keep(base, trial_id: str | None = None, *, now: float, confirm) -> dict:
    """Preserve the trial's artifacts for the record: state → kept, runs refuse,
    and remove afterwards asks for --really."""
    root, state = _resolve(base, trial_id, now)
    if state["state"] == "kept":
        raise TrialError("trial is already kept")
    if state["state"] not in ("active", "stopped"):
        raise TrialError(f"trial is {state['state']!r} — nothing to keep")
    plan = (
        f"trial {state['id']}: keep the artifacts for the record\n"
        f"  state → kept; runs refuse from now on; remove will require --really\n"
        f"  artifacts stay under {root} (state, db, editions, renders) until removed")
    _require_consent(confirm, plan)
    if state["state"] == "active":
        state["stopped_at"] = int(now)
    state["state"] = "kept"
    state["kept_at"] = int(now)
    _save_state(root, state)
    return {"id": state["id"], "root": str(root), "state": "kept",
            "kept_at": state["kept_at"]}


def remove(base, trial_id: str | None = None, *, now: float, confirm,
           really: bool = False) -> dict:
    """Delete the trial and every file it owns. A live trial must be stopped
    first (two consents, never one); a kept trial needs --really."""
    root, state = _resolve(base, trial_id, now)
    if state["state"] == "active" and not _expired(state, now):
        raise TrialError(
            "the trial is still live — stop it first (gaceta trial stop --yes); "
            "removal never races a live trial")
    if state["state"] == "kept" and not really:
        raise TrialError("the trial is kept for the record — pass --really to "
                         "remove it anyway")
    plan = (
        f"trial {state['id']}: DELETE the whole trial directory\n"
        f"  {root}\n"
        "  removes state, db, synthetic feed, editions and renders; nothing is kept back")
    _require_consent(confirm, plan)
    shutil.rmtree(root)
    return {"id": state["id"], "removed": str(root)}
