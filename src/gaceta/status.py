"""Reader build status: one small JSON file per reader under <data>/status/.

`gaceta edition` rewrites <data>/status/<reader>.json after every run —
success, failure or an --only-if-new skip — atomically (tmp + replace), so
the owner's health check (`gaceta status`, `gaceta status --json`) reads a
file and never parses logs or opens the database. The whole file is
owner-side data, like the reader's config, and never leaves the data dir.

Schema (every key always present, in this order):

  reader                 reader id
  built_at               ISO of the reader's current edition (null before any)
  last_ok                ISO of the last successful build
  last_fail              ISO of the last failed attempt (kept after later ok runs)
  fail_reason            short reason for that failure, privacy-scrubbed
  edition_id             "<date>-<max_privacy>" of the current edition
  new_items              items newly ingested for this reader since the
                         previous edition's build (the build's input delta)
  llm_calls              LLM calls this build consumed (the edition build
                         itself calls none; null when the build failed)
  consecutive_failures   failures in a row; 0 after any success
  + "skipped": "nothing new"   only after an --only-if-new skip

Times carry the build machine's offset, like the edition snapshot's stamps.
fail_reason is exception class + message with query strings stripped from
any URL, home paths folded to ~ and at most REASON_MAX characters — a
health line, never a log dump.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

# same slug rule as the server's reader routes: the file name is reader-controlled
READER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REASON_MAX = 300
_URL_QUERY_RE = re.compile(r"((?:https?://)[^\s'\"\]<>]+)\?[^\s'\"\]<>]*")
_HOME = str(Path.home())


def status_dir(data_dir) -> Path:
    return Path(data_dir) / "status"


def status_path(data_dir, reader_id: str) -> Path:
    if not READER_RE.fullmatch(str(reader_id or "")):
        raise ValueError(f"bad reader id {reader_id!r}")
    return status_dir(data_dir) / f"{reader_id}.json"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_reason(exc: BaseException | None = None, *, text: str | None = None) -> str:
    """A short, privacy-scrubbed one-line reason for a failed build."""
    raw = text if text is not None else (
        f"{type(exc).__name__}: {exc}" if exc is not None else "")
    raw = raw.replace(_HOME, "~")
    raw = _URL_QUERY_RE.sub(r"\1", raw)  # URLs never keep their query strings
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:REASON_MAX]


def load(data_dir, reader_id: str) -> dict | None:
    """The reader's status doc, or None when no file exists (yet)."""
    try:
        doc = json.loads(status_path(data_dir, reader_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def load_all(data_dir) -> dict[str, dict]:
    """Every status doc, keyed by reader id (sorted by file name)."""
    out: dict[str, dict] = {}
    d = status_dir(data_dir)
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("reader"):
            out[str(doc["reader"])] = doc
    return out


def save(data_dir, reader_id: str, doc: dict) -> None:
    """Write the doc atomically: readers see either the old or the new file."""
    path = status_path(data_dir, reader_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _base(reader_id: str) -> dict:
    return {"reader": reader_id, "built_at": None, "last_ok": None, "last_fail": None,
            "fail_reason": None, "edition_id": None, "new_items": None,
            "llm_calls": None, "consecutive_failures": 0}


def _current(data_dir, reader_id: str) -> dict:
    """The stored doc on the canonical key order; unknown keys and a stale
    'skipped' marker drop out. Missing file → the all-null base."""
    doc = _base(reader_id)
    stored = load(data_dir, reader_id) or {}
    for key in doc:
        if key != "reader" and key in stored:
            doc[key] = stored[key]
    return doc


def record_success(data_dir, reader_id: str, edition: dict, *,
                   new_items: int, llm_calls: int = 0) -> dict:
    """A finished build: it becomes the reader's current edition."""
    doc = _current(data_dir, reader_id)
    built = str(edition.get("built_at") or "") or _now_iso()
    doc.update({
        "built_at": built, "last_ok": built,
        "edition_id": f"{edition.get('date')}-{edition.get('max_privacy')}",
        "new_items": int(new_items), "llm_calls": int(llm_calls),
        "consecutive_failures": 0,
    })
    save(data_dir, reader_id, doc)
    return doc


def record_failure(data_dir, reader_id: str, exc: BaseException | None = None,
                   *, reason: str | None = None) -> dict:
    """A build that raised: the previous edition stays the current one."""
    doc = _current(data_dir, reader_id)
    doc.update({
        "last_fail": _now_iso(),
        "fail_reason": reason or clean_reason(exc),
        "new_items": None, "llm_calls": None,
        "consecutive_failures": int(doc.get("consecutive_failures") or 0) + 1,
    })
    save(data_dir, reader_id, doc)
    return doc


def record_skip(data_dir, reader_id: str, *, new_items: int = 0,
                llm_calls: int = 0) -> dict:
    """--only-if-new with nothing new: the current edition is still current."""
    doc = _current(data_dir, reader_id)
    doc.update({"new_items": int(new_items), "llm_calls": int(llm_calls),
                "skipped": "nothing new"})
    save(data_dir, reader_id, doc)
    return doc


def age_s(doc: dict, *, now: float | None = None) -> int | None:
    """Seconds since last_ok (0 when the stamp is in the future), or None."""
    stamp = (doc or {}).get("last_ok")
    if not stamp:
        return None
    try:
        ts = datetime.fromisoformat(str(stamp)).timestamp()
    except (ValueError, TypeError):
        return None
    return max(0, int((now if now is not None else time.time()) - ts))
