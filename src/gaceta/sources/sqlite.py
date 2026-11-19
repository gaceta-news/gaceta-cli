"""SQLite notes source adapter (example plugin).

Reads a collector-style sqlite database plus an optional note library,
STRICTLY read-only, and yields RawItems with the reader-side layer
(verdict, note, library link) attached.

Expected upstream layout under the configured ``path``:

- ``items.db`` with table ``items(id, source, author, title, url, published,
  body, verdict, note)`` and an optional ``kv(k, v)`` table.
- ``library/*/*.md`` note files; item id 'yt:ABC' maps to file 'yt_ABC.md'.

- The db is opened via ``sqlite3.connect('file:…?mode=ro', uri=True)``; any
  write raises.
- source_id: '{source}:{author}' for youtube/nostr, else 'feed:{source}'.
- kind: youtube|nostr|hn; '*-releases' → release; else rss.
- content_public=True (collected sources are publicly published content).
- ``discovery_candidates`` (optional hook used by discovery.suggestions):
  nostr accounts the reader's follows mention but don't follow, from kv
  'nostr_candidates' = {pubkey_hex: mentions} and 'nostr_names'. A missing or
  broken kv never breaks the run.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterator

from .base import RawItem, RawSource

_VERDICTS = ("told", "noted", "skip")


def _kind(source: str) -> str:
    if source == "youtube":
        return "youtube"
    if source == "nostr":
        return "nostr"
    if source == "hn":
        return "hn"
    if source.endswith("-releases"):
        return "release"
    return "rss"


def _source_id(source: str, author: str | None) -> str:
    if source in ("youtube", "nostr"):
        return f"{source}:{author or ''}"
    return f"feed:{source}"


class SqliteAdapter:
    name = "sqlite"

    def __init__(self, cfg: dict):
        self.path = Path(cfg["path"] if isinstance(cfg, dict) else cfg)
        self.db_path = self.path / "items.db"
        self._lib: dict[str, Path] | None = None

    def connect(self) -> sqlite3.Connection:
        """Read-only connection into the upstream items.db."""
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def _library(self) -> dict[str, Path]:
        """Index of library files by filename, built once."""
        if self._lib is None:
            self._lib = {}
            for p in sorted(self.path.glob("library/*/*.md")):
                self._lib.setdefault(p.name, p)
        return self._lib

    def _library_path(self, item_id: str) -> str | None:
        p = self._library().get(item_id.replace(":", "_") + ".md")
        return str(p) if p else None

    def items(self, since: int | None = None) -> Iterator[RawItem]:
        sql = (
            "SELECT id, source, author, title, url, published, body, verdict, note "
            "FROM items"
        )
        params: tuple = ()
        if since is not None:
            sql += " WHERE published >= ?"
            params = (since,)
        conn = self.connect()
        try:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(sql, params):
                verdict = (row["verdict"] or "").strip().lower()
                if verdict not in _VERDICTS:
                    verdict = "pending"
                item_id = row["id"]
                src = _source_id(row["source"], row["author"])
                yield RawItem(
                    id=item_id,
                    source=RawSource(
                        id=src,
                        kind=_kind(row["source"]),
                        name=row["author"] if row["source"] in ("youtube", "nostr") else row["source"],
                        content_public=True,
                    ),
                    author=row["author"],
                    title=row["title"],
                    url=row["url"],
                    published=row["published"],
                    body=row["body"],
                    verdict=verdict,
                    note=row["note"],
                    library_path=self._library_path(item_id),
                )
        finally:
            conn.close()

    def discovery_candidates(self, limit: int = 8) -> list[dict]:
        """Nostr accounts the reader's follows mention but don't follow:
        kv 'nostr_candidates' = {pubkey_hex: mentions}, 'nostr_names' maybe
        partial. Read-only; a missing/broken kv never breaks the edition."""
        try:
            conn = self.connect()
        except sqlite3.Error:
            return []
        try:
            def kvdict(key: str) -> dict:
                row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
                if row is None:
                    return {}
                try:
                    val = json.loads(row[0])
                except (TypeError, ValueError):
                    return {}
                return val if isinstance(val, dict) else {}

            counts = {str(k): n for k, n in kvdict("nostr_candidates").items()
                      if isinstance(n, (int, float))}
            names = kvdict("nostr_names")
            out = []
            for pk, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]:
                out.append({
                    "key": f"nostr:{pk}",
                    "name": str(names.get(pk) or ""),
                    "kind": "nostr",
                    "reason": f"Mencionada {int(n)} veces por gente que sigues",
                    "url": f"https://njump.me/{pk}",
                })
            return out
        finally:
            conn.close()
