"""llm_decisions cache policy (ROADMAP 14).

The cache's promise is "a rerun of the same decision costs zero calls". It held
for real answers but also froze non-answers: a quota error (HTTP 429 once every
chain member is spent) was stored as an empty reply, and an invalid-JSON reply
was stored as final, so the story never got another try. Policy:

- transient refusals (429, 5xx) are never stored, and rows stored that way by
  older versions are ignored on read: the call simply happens again later;
- a reply that the caller's parser rejects is retried after RETRY_AFTER,
  doubling per attempt, up to MAX_ATTEMPTS replies; then it stays as is;
- valid replies and permanent refusals (content filter, other 4xx) are final.

``usage.attempts`` counts the invalid replies stored for a key.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Callable

RETRY_AFTER = 6 * 3600
MAX_ATTEMPTS = 3

Valid = Callable[[str], bool] | None


def transient(code: object) -> bool:
    """HTTP codes that say "not now", not "no": quota/rate (429) and 5xx."""
    try:
        c = int(code)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return c == 429 or 500 <= c < 600


def _usage(raw: object) -> dict:
    try:
        u = json.loads(raw or "{}")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {}
    return u if isinstance(u, dict) else {}


def lookup(conn: sqlite3.Connection, key: str, *, valid: Valid = None,
           now: float | None = None) -> str | None:
    """The cached reply for ``key``, or None when a (new) call is due."""
    row = conn.execute("SELECT output, usage, at FROM llm_decisions WHERE key = ?",
                       (key,)).fetchone()
    if row is None:
        return None
    out, usage, at = row[0] or "", _usage(row[1]), int(row[2] or 0)
    if transient(usage.get("refused")):
        return None  # a quota/server error is not a decision
    if valid is None or usage.get("refused") or valid(out):
        return out
    attempts = int(usage.get("attempts") or 1)
    if attempts >= MAX_ATTEMPTS:
        return out  # tried enough; the caller keeps the story as it is
    now = time.time() if now is None else now
    if now - at >= RETRY_AFTER * 2 ** (attempts - 1):
        return None
    return out


def store(conn: sqlite3.Connection, key: str, model: str, text: str, out: str,
          usage: dict, *, valid: Valid = None, now: float | None = None) -> None:
    """Record a reply. Transient refusals are not recorded at all."""
    if transient(usage.get("refused")):
        return
    usage = dict(usage)
    if valid is not None and not usage.get("refused") and not valid(out):
        prev = conn.execute("SELECT output, usage FROM llm_decisions WHERE key = ?",
                            (key,)).fetchone()
        n = 0
        if prev is not None:
            pu = _usage(prev[1])
            if not transient(pu.get("refused")) and not valid(prev[0] or ""):
                n = int(pu.get("attempts") or 1)
        usage["attempts"] = n + 1
    conn.execute(
        "INSERT INTO llm_decisions (key, model, input_hash, output, usage, at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
        "output = excluded.output, usage = excluded.usage, at = excluded.at",
        (key, model, hashlib.sha256(text.encode()).hexdigest(), out, json.dumps(usage),
         int(time.time() if now is None else now)),
    )
