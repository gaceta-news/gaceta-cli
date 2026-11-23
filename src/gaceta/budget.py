"""Per-reader daily LLM call budget ([llm] daily_max_calls, llm_usage table).

Every real (non-cache) chat call a reader's editor makes — write-ups, change
notes, cluster confirmations — counts into llm_usage, one row per reader per
day. Cache hits never count. The day is the reader's wall clock: [schedule] tz
when configured, else the system local date, so 'daily' follows the reader,
not the server. Absent daily_max_calls = unlimited (today's behaviour); the
count still accumulates so `gaceta stats` can show today's use.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def day_key(cfg: dict, now: float | None = None) -> str:
    """The reader's local date (YYYY-MM-DD) at a moment: [schedule] tz when
    configured, else the system local time. `now` is unix seconds (None =
    now); the budget stops exactly like the per-run budgets at day edges."""
    tz = (cfg.get("schedule") or {}).get("tz")
    if tz:
        try:
            zone = ZoneInfo(str(tz))
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError(
                f"[schedule] tz is not a known time zone: {tz!r}") from None
        at = datetime.fromtimestamp(now, zone) if now is not None else datetime.now(zone)
        return at.date().isoformat()
    at = datetime.fromtimestamp(now) if now is not None else datetime.now()
    return at.date().isoformat()


class DailyBudget:
    """One reader's calls-so-far-today and the optional limit.

    reached() gates a real call exactly like the per-run max_llm_calls budget
    (graceful stop; the edition continues with what is written); count()
    records one real call. Commits stay with the caller, as they already are
    for llm_decisions.
    """

    def __init__(self, conn: sqlite3.Connection, reader_id: str, cfg: dict, *,
                 now: float | None = None):
        llm = cfg.get("llm") or {}
        if "daily_max_calls" in llm:
            n = llm["daily_max_calls"]
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                raise ValueError("[llm] daily_max_calls must be a non-negative integer")
            self.limit: int | None = n
        else:
            self.limit = None
        self.conn = conn
        self.reader_id = reader_id
        self.day = day_key(cfg, now)

    def used(self) -> int:
        row = self.conn.execute(
            "SELECT calls FROM llm_usage WHERE reader_id = ? AND day = ?",
            (self.reader_id, self.day),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def reached(self) -> bool:
        return self.limit is not None and self.used() >= self.limit

    def count(self) -> None:
        """One real (non-cache) call; flushed by the caller's next commit."""
        self.conn.execute(
            "INSERT INTO llm_usage (reader_id, day, calls) VALUES (?, ?, 1) "
            "ON CONFLICT(reader_id, day) DO UPDATE SET calls = calls + 1",
            (self.reader_id, self.day),
        )
