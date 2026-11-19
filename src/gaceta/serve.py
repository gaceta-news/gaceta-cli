"""Local HTTP server for the built paper (localhost only, stdlib only).

Serves <data>/site/<reader>/*.html with the reaction script injected, media with
long cache, and a small JSON API that records reactions per person (by_name).
Only reactions whose by_name is the edition owner's steer that owner's ranking
(editor.taste); household readers' reactions are stored for their own editions.

Hard rules: bind refused outside 127.0.0.1/::1/localhost; file serving is
rooted at <data>/site (resolve + is_relative_to, strict filename regexes);
media filenames must match the downloaded-name pattern; every response carries
CSP + nosniff + no-referrer. No secrets, no templates, no third-party deps.
"""
from __future__ import annotations

import json
import subprocess
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config, db

ALLOWED_HOSTS = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = 8711
KINDS = ("up", "down", "more", "less", "why", "hide", "open")
SOURCE_DECISIONS = ("follow", "drop", "later")
MAX_BODY = 4096
MAX_AGENT_NOTE = 1000
MAX_SEEN_BODY = 32768  # /api/seen carries up to 200 story ids + revs
MAX_BY = 40
MAX_STORIES = 200
REV_RE = re.compile(r"^[a-f0-9]{1,16}$")
MAX_TEXT = 500
MEDIA_RE = re.compile(r"^[a-f0-9]{16}\.(jpg|png|webp|gif)$")
EDITION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.html$")
READER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

CSP = ("default-src 'self'; img-src 'self' https:; media-src https:; "
       "frame-src https://www.youtube-nocookie.com; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'")

REACT_TAG = '<script src="/static/react.js" defer></script>'


def validate_host(host: str) -> str:
    if host not in ALLOWED_HOSTS:
        raise ValueError(
            f"refusing to bind {host!r}: serve is localhost-only (127.0.0.1 / ::1 / localhost)"
        )
    return host


def inject_react(html: str) -> str:
    """Add the reaction script before </body> (once) and rebase media src to /media/."""
    if "/static/react.js" not in html:
        html = html.replace("</body>", f"{REACT_TAG}</body>", 1)
    return html.replace('src="../media/', 'src="/media/')


def _day_bounds(ts: float | None = None) -> tuple[int, int]:
    """Start/end unix seconds of the current local calendar day (toggle window)."""
    lt = time.localtime(ts)
    start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    return start, start + 86399


def _agent_label(reader: str, data_dir) -> str | None:
    """Display name of the reader's agent when one is configured ([agent] name/command)."""
    try:
        a = config.load_reader(reader, data_dir).get("agent") or {}
    except Exception:  # missing/broken reader config never breaks /api/state
        return None
    if not a.get("command"):
        return None
    return str(a.get("name") or "tu agente")[:40]


def deliver_to_agent(cfg: dict, payload: dict) -> tuple[bool, str | None]:
    """Start the reader's configured agent command with ``payload`` as JSON on stdin.

    ``[agent] command`` must be an argv list (no shell). Returns (started, error).
    No command configured → (False, None): the send stays recorded for later pickup.
    """
    cmd = (cfg.get("agent") or {}).get("command")
    if not cmd:
        return False, None
    if not (isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd)):
        return False, "agent.command must be a list of strings"
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        proc.stdin.close()
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"[:300]
    return True, None


class State:
    """Per-server context: the data dir and a serialized write lock for sqlite."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = db.connect(self.data_dir / "gaceta.db")
        db.init(conn)
        return conn


class Handler(BaseHTTPRequestHandler):
    server_version = "gaceta"
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    # ------------------------------------------------------------ plumbing

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass

    def _send(self, code: int, body: bytes, ctype: str, cache: str | None = None,
              close: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if cache:
            self.send_header("Cache-Control", cache)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, close: bool = False) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json",
                   cache="no-store", close=close)

    def _error(self, code: int, msg: str, close: bool = False) -> None:
        self._json(code, {"ok": False, "error": msg}, close=close)

    # ------------------------------------------------------------ GET

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path in ("", "/"):
                return self._get_index()
            if path == "/static/react.js":
                return self._get_react_js()
            if path.startswith("/media/"):
                return self._get_media(path[len("/media/"):])
            if path.startswith("/r/"):
                rest = path[len("/r/"):]
                reader, _, fname = rest.partition("/")
                if not READER_RE.match(reader):
                    return self._error(404, "no such reader")
                if fname == "":
                    return self._get_edition(reader, "index.html")
                if fname == "guardadas":
                    return self._get_guardadas(reader)
                if fname == "index.html" or EDITION_RE.match(fname):
                    return self._get_edition(reader, fname)
                return self._error(404, "not found")
            if path in ("/api/react", "/api/seen", "/api/unseen", "/api/save", "/api/unsave"):
                return self._error(405, "POST only")
            if path == "/api/state":
                return self._api_state()
            if path == "/api/readers":
                return self._api_readers()
            return self._error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # never leak a traceback to the wire
            try:
                self._error(500, f"internal error: {type(e).__name__}")
            except Exception:
                pass

    def _site_file(self, *parts: str) -> Path | None:
        """Resolve inside <data>/site; None on any traversal or escape."""
        base = (self.state.data_dir / "site").resolve()
        target = base.joinpath(*parts).resolve()
        if target != base and not target.is_relative_to(base):
            return None
        return target

    def _get_index(self) -> None:
        site = self.state.data_dir / "site"
        readers = sorted(
            d.name for d in site.iterdir()
            if d.is_dir() and (d / "index.html").is_file()
        ) if site.is_dir() else []
        if not readers:
            return self._error(404, "no rendered editions (run: gaceta render)")
        if len(readers) == 1:
            self.send_response(302)
            self.send_header("Location", f"/r/{readers[0]}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        lis = "\n".join(
            f'<li><a href="/r/{r}/">{r}</a></li>' for r in readers
        )
        body = (
            "<!DOCTYPE html><html lang=\"es\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>La Gaceta</title></head><body><main><h1>La Gaceta</h1>"
            f"<ul>{lis}</ul></main></body></html>"
        ).encode()
        self._send(200, body, "text/html; charset=utf-8", cache="no-cache")

    def _get_edition(self, reader: str, fname: str) -> None:
        path = self._site_file(reader, fname)
        if path is None or not path.is_file():
            return self._error(404, "not found")
        html = path.read_text(encoding="utf-8")
        self._send(200, inject_react(html).encode(), "text/html; charset=utf-8",
                   cache="no-cache")

    def _get_media(self, name: str) -> None:
        if not MEDIA_RE.match(name):
            return self._error(404, "not found")
        path = self._site_file("media", name)
        if path is None or not path.is_file():
            return self._error(404, "not found")
        ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self._send(200, path.read_bytes(), ctype, cache="public, max-age=2592000, immutable")

    def _get_react_js(self) -> None:
        data = (resources.files("gaceta") / "static" / "react.js").read_bytes()
        self._send(200, data, "application/javascript; charset=utf-8",
                   cache="no-cache")

    # ------------------------------------------------------------ POST /api/react

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or f"127.0.0.1:{getattr(self.server, 'gaceta_port', DEFAULT_PORT)}"
        try:
            o = urlsplit(origin)
        except ValueError:
            return False
        return bool(o.netloc) and o.netloc == host and o.scheme in ("http", "https")

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            if path == "/api/react":
                return self._api_react()
            if path == "/api/agent":
                return self._api_agent()
            if path == "/api/source":
                return self._api_source()
            if path == "/api/seen":
                return self._api_seen()
            if path == "/api/unseen":
                return self._api_unseen()
            if path == "/api/save":
                return self._api_save()
            if path == "/api/unsave":
                return self._api_unsave()
            return self._error(404, "not found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._error(500, f"internal error: {type(e).__name__}")
            except Exception:
                pass

    def _read_json_body(self, max_body: int = MAX_BODY):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return None, (415, "Content-Type must be application/json")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > max_body:
            return None, (413, f"body must be 1..{max_body} bytes")
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, (400, "invalid JSON")
        if not isinstance(obj, dict):
            return None, (400, "expected a JSON object")
        return obj, None

    def _api_react(self) -> None:
        if not self._origin_ok():
            return self._error(403, "cross-origin")
        obj, err = self._read_json_body()
        if err:
            # drain nothing further; close so the client can't keep the conn half-read
            return self._error(err[0], err[1], close=True)
        assert obj is not None
        reader = obj.get("reader")
        story_id = obj.get("story_id")
        kind = obj.get("kind")
        by = obj.get("by")
        text = obj.get("text")
        date = obj.get("date")
        if not (isinstance(reader, str) and READER_RE.match(reader)):
            return self._error(400, "bad reader")
        if not isinstance(story_id, str) or not story_id or len(story_id) > 200:
            return self._error(400, "bad story_id")
        if kind not in KINDS:
            return self._error(400, f"kind must be one of {', '.join(KINDS)}")
        if not isinstance(by, str) or not by.strip() or len(by) > MAX_BY:
            return self._error(400, f"by must be 1..{MAX_BY} chars")
        if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT):
            return self._error(400, f"text must be <= {MAX_TEXT} chars")
        if date is not None and (not isinstance(date, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", date)):
            return self._error(400, "bad date")

        conn = self.state.connect()
        try:
            if conn.execute("SELECT 1 FROM readers WHERE id = ?", (reader,)).fetchone() is None:
                return self._error(404, "unknown reader")
            if conn.execute("SELECT 1 FROM stories WHERE id = ? AND reader_id = ?",
                            (story_id, reader)).fetchone() is None:
                return self._error(404, "unknown story for this reader")
            day_start, day_end = _day_bounds()
            by = by.strip()
            if kind == "open":
                # implicit interest (a link was opened): fire-and-forget, never
                # toggled off; one row per (reader, by, story), ever.
                with self.state.lock:
                    exists = conn.execute(
                        "SELECT 1 FROM reactions WHERE reader_id = ? AND story_id = ? "
                        "AND kind = 'open' AND by_name = ?",
                        (reader, story_id, by),
                    ).fetchone()
                    if exists is None:
                        conn.execute(
                            "INSERT INTO reactions (reader_id, story_id, kind, at, by_name) "
                            "VALUES (?, ?, 'open', ?, ?)",
                            (reader, story_id, int(time.time()), by),
                        )
                        conn.commit()
                return self._json(200, {"ok": True, "state": {"kinds": ["open"]}})
            with self.state.lock:
                # by_name is always stored (by is required), so the toggle key is exact
                existing = conn.execute(
                    "SELECT id FROM reactions WHERE reader_id = ? AND story_id = ? "
                    "AND kind = ? AND by_name = ? AND at >= ? AND at <= ?",
                    (reader, story_id, kind, by, day_start, day_end),
                ).fetchone()
                if existing is not None:
                    conn.execute("DELETE FROM reactions WHERE id = ?", (existing["id"],))
                    conn.commit()
                else:
                    conn.execute(
                        "INSERT INTO reactions (reader_id, story_id, kind, text, at, by_name) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (reader, story_id, kind, text, int(time.time()), by),
                    )
                    conn.commit()
            kinds = [r["kind"] for r in conn.execute(
                "SELECT kind FROM reactions WHERE reader_id = ? AND story_id = ? "
                "AND by_name = ? AND at >= ? AND at <= ? ORDER BY id",
                (reader, story_id, by, day_start, day_end),
            )]
            return self._json(200, {"ok": True, "state": {"kinds": kinds}})
        finally:
            conn.close()

    # ------------------------------------------------------------ POST /api/source

    def _api_source(self) -> None:
        """Record the reader's verdict on a discovered source (schema v7).
        Only the edition owner may decide; the key must be in the current
        edition's discovery block."""
        if not self._origin_ok():
            return self._error(403, "cross-origin")
        obj, err = self._read_json_body()
        if err:
            return self._error(err[0], err[1], close=True)
        reader = obj.get("reader")
        source_key = obj.get("source_key")
        decision = obj.get("decision")
        by = obj.get("by")
        if not (isinstance(reader, str) and READER_RE.match(reader)):
            return self._error(400, "bad reader")
        if not isinstance(source_key, str) or not source_key.strip() or len(source_key) > 200:
            return self._error(400, "source_key must be 1..200 chars")
        if decision not in SOURCE_DECISIONS:
            return self._error(400, f"decision must be one of {', '.join(SOURCE_DECISIONS)}")
        if not isinstance(by, str) or not by.strip() or len(by) > MAX_BY:
            return self._error(400, f"by must be 1..{MAX_BY} chars")
        by = by.strip()
        conn = self.state.connect()
        try:
            row = conn.execute("SELECT name FROM readers WHERE id = ?", (reader,)).fetchone()
            if row is None:
                return self._error(404, "unknown reader")
            if by.lower() != str(row["name"] or reader).lower():
                return self._error(403, "only the edition owner decides sources")
            ed = self._load_edition(conn, reader, None)
            if ed is None:
                return self._error(404, "no edition for this reader")
            disc = ed.get("discovery") or {}
            keys = {str(e.get("key")) for e in (disc.get("trials") or [])}
            keys |= {str(e.get("key")) for e in (disc.get("suggestions") or [])}
            if source_key not in keys:
                return self._error(400, "source_key not in the current edition's discovery")
            with self.state.lock:
                conn.execute(
                    "INSERT INTO source_decisions (reader_id, by_name, source_key, decision, at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(reader_id, source_key) DO UPDATE SET "
                    "decision = excluded.decision, by_name = excluded.by_name, "
                    "at = excluded.at, exported_at = NULL",
                    (reader, by, source_key, decision, int(time.time())),
                )
                conn.commit()
            return self._json(200, {"ok": True, "source_key": source_key,
                                    "decision": decision})
        finally:
            conn.close()

    # ------------------------------------------------------------ POST /api/agent

    def _api_agent(self) -> None:
        """Hand a story to the reader's own agent (schema v8, roadmap 6c).

        Owner only. The story must be in the current edition. The send is recorded,
        then — if the reader configured ``[agent] command`` (argv list) — that command
        is started detached with one JSON object on stdin. gaceta never waits for or
        stores the agent's answer; the agent replies on its own channel."""
        if not self._origin_ok():
            return self._error(403, "cross-origin")
        obj, err = self._read_json_body()
        if err:
            return self._error(err[0], err[1], close=True)
        reader, story_id, by = obj.get("reader"), obj.get("story_id"), obj.get("by")
        note = obj.get("note") or ""
        if not (isinstance(reader, str) and READER_RE.match(reader)):
            return self._error(400, "bad reader")
        if not isinstance(story_id, str) or not story_id or len(story_id) > 120:
            return self._error(400, "bad story_id")
        if not isinstance(note, str) or len(note) > MAX_AGENT_NOTE:
            return self._error(400, f"note must be a string of <= {MAX_AGENT_NOTE} chars")
        if not isinstance(by, str) or not by.strip() or len(by) > MAX_BY:
            return self._error(400, f"by must be 1..{MAX_BY} chars")
        by, note = by.strip(), note.strip()
        conn = self.state.connect()
        try:
            row = conn.execute("SELECT name FROM readers WHERE id = ?", (reader,)).fetchone()
            if row is None:
                return self._error(404, "unknown reader")
            if by.lower() != str(row["name"] or reader).lower():
                return self._error(403, "only the edition owner has an agent here")
            ed = self._load_edition(conn, reader, None)
            story = ((ed or {}).get("stories") or {}).get(story_id)
            if story is None:
                return self._error(400, "story not in the current edition")
            with self.state.lock:
                cur = conn.execute(
                    "INSERT INTO agent_sends (reader_id, by_name, story_id, note, at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (reader, by, story_id, note or None, int(time.time())),
                )
                send_id = cur.lastrowid
                conn.commit()
            cfg = config.load_reader(reader, self.state.data_dir)
            delivered, error = deliver_to_agent(cfg, {
                "reader": reader, "by": by, "note": note, "send_id": send_id,
                "edition_date": (ed or {}).get("date"),
                "story": {k: story.get(k) for k in (
                    "title", "summary", "why_it_matters", "sources", "score_reasons")} | {"id": story_id},
            })
            with self.state.lock:
                conn.execute(
                    "UPDATE agent_sends SET delivered_at = ?, error = ? WHERE id = ?",
                    (int(time.time()) if delivered else None, error, send_id),
                )
                conn.commit()
            return self._json(200, {"ok": True, "send_id": send_id, "delivered": delivered})
        finally:
            conn.close()

    # ------------------------------------------------------------ POST /api/seen, /api/unseen

    def _api_seen(self) -> None:
        """Upsert per-person seen marks: {reader, story_ids: [...<=200],
        revs: {story_id: rev}, by}. rev is <=16 hex chars; every story must
        belong to the reader. Same origin/content-type/size rules as react."""
        if not self._origin_ok():
            return self._error(403, "cross-origin")
        obj, err = self._read_json_body(MAX_SEEN_BODY)
        if err:
            return self._error(err[0], err[1], close=True)
        reader = obj.get("reader")
        by = obj.get("by")
        story_ids = obj.get("story_ids")
        revs = obj.get("revs")
        if not (isinstance(reader, str) and READER_RE.match(reader)):
            return self._error(400, "bad reader")
        if not isinstance(by, str) or not by.strip() or len(by) > MAX_BY:
            return self._error(400, f"by must be 1..{MAX_BY} chars")
        if not isinstance(story_ids, list) or not 1 <= len(story_ids) <= MAX_STORIES:
            return self._error(400, f"story_ids must be a list of 1..{MAX_STORIES}")
        if not isinstance(revs, dict):
            return self._error(400, "revs must be an object {story_id: rev}")
        for sid in story_ids:
            if not isinstance(sid, str) or not sid or len(sid) > 200:
                return self._error(400, "bad story_id")
            rev = revs.get(sid)
            if not isinstance(rev, str) or not REV_RE.match(rev):
                return self._error(400, f"revs[{sid}] must be 1..16 hex chars")
        by = by.strip()
        conn = self.state.connect()
        try:
            if conn.execute("SELECT 1 FROM readers WHERE id = ?", (reader,)).fetchone() is None:
                return self._error(404, "unknown reader")
            placeholders = ",".join("?" * len(story_ids))
            known = {
                r["id"] for r in conn.execute(
                    f"SELECT id FROM stories WHERE reader_id = ? AND id IN ({placeholders})",
                    (reader, *story_ids),
                )
            }
            missing = [sid for sid in story_ids if sid not in known]
            if missing:
                return self._error(404, f"unknown story for this reader: {missing[0]}")
            now = int(time.time())
            with self.state.lock:
                conn.executemany(
                    "INSERT INTO seen (reader_id, by_name, story_id, rev, at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(reader_id, by_name, story_id) DO UPDATE SET "
                    "rev = excluded.rev, at = excluded.at",
                    [(reader, by, sid, revs[sid], now) for sid in story_ids],
                )
                conn.commit()
            return self._json(200, {"ok": True, "seen": {sid: revs[sid] for sid in story_ids}})
        finally:
            conn.close()

    def _api_unseen(self) -> None:
        if not self._origin_ok():
            return self._error(403, "cross-origin")
        obj, err = self._read_json_body()
        if err:
            return self._error(err[0], err[1], close=True)
        reader = obj.get("reader")
        by = obj.get("by")
        story_id = obj.get("story_id")
        if not (isinstance(reader, str) and READER_RE.match(reader)):
            return self._error(400, "bad reader")
        if not isinstance(by, str) or not by.strip() or len(by) > MAX_BY:
            return self._error(400, f"by must be 1..{MAX_BY} chars")
        if not isinstance(story_id, str) or not story_id or len(story_id) > 200:
            return self._error(400, "bad story_id")
        conn = self.state.connect()
        try:
            if conn.execute("SELECT 1 FROM readers WHERE id = ?", (reader,)).fetchone() is None:
                return self._error(404, "unknown reader")
            with self.state.lock:
                conn.execute(
                    "DELETE FROM seen WHERE reader_id = ? AND by_name = ? AND story_id = ?",
                    (reader, by.strip(), story_id),
                )
                conn.commit()
            return self._json(200, {"ok": True})
        finally:
            conn.close()

    # ------------------------------------------------------------ POST /api/save, /api/unsave

    def _api_save(self) -> None:
        """Save a story ('Guardar', schema v9): local and per reading person
        (owner or household), story must be in the current edition, note <= 1000
        chars. One active save per (reader, by, story); saving again updates the note.
        Only the owner's saves steer the ranking (taste)."""
        if not self._origin_ok():
            return self._error(403, "cross-origin")
        obj, err = self._read_json_body()
        if err:
            return self._error(err[0], err[1], close=True)
        reader, story_id, by = obj.get("reader"), obj.get("story_id"), obj.get("by")
        note = obj.get("note") or ""
        if not (isinstance(reader, str) and READER_RE.match(reader)):
            return self._error(400, "bad reader")
        if not isinstance(story_id, str) or not story_id or len(story_id) > 120:
            return self._error(400, "bad story_id")
        if not isinstance(note, str) or len(note) > MAX_AGENT_NOTE:
            return self._error(400, f"note must be a string of <= {MAX_AGENT_NOTE} chars")
        if not isinstance(by, str) or not by.strip() or len(by) > MAX_BY:
            return self._error(400, f"by must be 1..{MAX_BY} chars")
        by, note = by.strip(), note.strip()
        conn = self.state.connect()
        try:
            row = conn.execute("SELECT name FROM readers WHERE id = ?", (reader,)).fetchone()
            if row is None:
                return self._error(404, "unknown reader")
            ed = self._load_edition(conn, reader, None)
            if ed is None or story_id not in (ed.get("stories") or {}):
                return self._error(400, "story not in the current edition")
            now = int(time.time())
            with self.state.lock:
                # one active save per (reader, by, story); saving again updates the note
                conn.execute(
                    "INSERT OR IGNORE INTO saves "
                    "(reader_id, by_name, story_id, item_id, origin, note, at) "
                    "VALUES (?, ?, ?, NULL, 'gaceta', ?, ?)",
                    (reader, by, story_id, note or None, now),
                )
                conn.execute(
                    "UPDATE saves SET note = ? WHERE reader_id = ? AND by_name = ? "
                    "AND story_id = ? AND removed_at IS NULL AND note IS NOT ?",
                    (note or None, reader, by, story_id, note or None),
                )
                conn.commit()
            return self._json(200, {"ok": True, "story_id": story_id})
        finally:
            conn.close()

    def _api_unsave(self) -> None:
        if not self._origin_ok():
            return self._error(403, "cross-origin")
        obj, err = self._read_json_body()
        if err:
            return self._error(err[0], err[1], close=True)
        reader, story_id, by = obj.get("reader"), obj.get("story_id"), obj.get("by")
        if not (isinstance(reader, str) and READER_RE.match(reader)):
            return self._error(400, "bad reader")
        if not isinstance(story_id, str) or not story_id or len(story_id) > 120:
            return self._error(400, "bad story_id")
        if not isinstance(by, str) or not by.strip() or len(by) > MAX_BY:
            return self._error(400, f"by must be 1..{MAX_BY} chars")
        by = by.strip()
        conn = self.state.connect()
        try:
            row = conn.execute("SELECT name FROM readers WHERE id = ?", (reader,)).fetchone()
            if row is None:
                return self._error(404, "unknown reader")
            with self.state.lock:
                conn.execute(
                    "UPDATE saves SET removed_at = ? WHERE reader_id = ? AND by_name = ? "
                    "AND story_id = ? AND origin = 'gaceta' AND removed_at IS NULL",
                    (int(time.time()), reader, by, story_id),
                )
                conn.commit()
            return self._json(200, {"ok": True, "story_id": story_id})
        finally:
            conn.close()

    # ------------------------------------------------------------ GET /r/<reader>/guardadas

    def _get_guardadas(self, reader: str) -> None:
        """The reading person's saved archive (schema v9, local): GET
        /r/<reader>/guardadas?by=<name>, newest first, same style as editions.
        Each person sees only their own saves; without ?by= the page asks them to
        open it from the edition (where their name is known)."""
        from .render.html import render_guardadas

        by = (parse_qs(urlsplit(self.path).query).get("by") or [""])[0].strip()[:MAX_BY]
        conn = self.state.connect()
        try:
            rrow = conn.execute("SELECT name FROM readers WHERE id = ?", (reader,)).fetchone()
            if rrow is None:
                return self._error(404, "no such reader")
            out = []
            if by:
                for sav in conn.execute(
                    "SELECT s.story_id, s.note, s.at, st.title, st.summary FROM saves s "
                    "JOIN stories st ON st.id = s.story_id AND st.reader_id = s.reader_id "
                    "WHERE s.reader_id = ? AND lower(s.by_name) = lower(?) AND s.origin = 'gaceta' "
                    "AND s.removed_at IS NULL ORDER BY s.at DESC, s.id DESC",
                    (reader, by),
                ):
                    sources = conn.execute(
                        "SELECT i.title, i.url, i.author, s.kind, i.published FROM story_items si "
                        "JOIN items i ON i.id = si.item_id JOIN sources s ON s.id = i.source_id "
                        "WHERE si.story_id = ? ORDER BY i.published",
                        (sav["story_id"],),
                    ).fetchall()
                    out.append({
                        "title": sav["title"], "summary": sav["summary"],
                        "note": sav["note"], "at": sav["at"],
                        "url": next((r["url"] for r in sources if r["url"]), None),
                        "sources": [dict(r) for r in sources],
                    })
        finally:
            conn.close()
        doc = render_guardadas(reader, by, out)
        self._send(200, doc.encode(), "text/html; charset=utf-8", cache="no-cache")

    # ------------------------------------------------------------ GET /api/*

    def _load_edition(self, conn, reader: str, date: str | None):
        row = conn.execute(
            "SELECT date, path FROM editions WHERE reader_id = ? "
            + ("AND date = ?" if date else "")
            + " ORDER BY date DESC, built_at DESC LIMIT 1",
            (reader, date) if date else (reader,),
        ).fetchone()
        if row is None:
            return None
        try:
            ed = json.loads(Path(row["path"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return ed

    def _api_state(self) -> None:
        q = parse_qs(urlsplit(self.path).query)
        reader = (q.get("reader") or [""])[0]
        date = (q.get("date") or [None])[0]
        by = (q.get("by") or [None])[0]
        if not READER_RE.match(reader):
            return self._error(400, "bad reader")
        if date is not None and not re.match(r"^\d{4}-\d{2}-\d{2}$", date or ""):
            return self._error(400, "bad date")
        conn = self.state.connect()
        try:
            rrow = conn.execute("SELECT name FROM readers WHERE id = ?", (reader,)).fetchone()
            if rrow is None:
                return self._error(404, "unknown reader")
            ed = self._load_edition(conn, reader, date)
            if ed is None:
                return self._error(404, "no edition for this reader/date")
            story_ids = list(ed.get("portada") or [])
            for sec in ed.get("sections") or []:
                story_ids.extend(sec.get("stories") or [])
            day_start, day_end = _day_bounds()
            reactions: dict[str, list[str]] = {}
            if by:
                rows = conn.execute(
                    "SELECT story_id, kind FROM reactions WHERE reader_id = ? "
                    "AND by_name = ? AND at >= ? AND at <= ? ORDER BY id",
                    (reader, by, day_start, day_end),
                ).fetchall()
            else:  # no by given: legacy rows without a name
                rows = conn.execute(
                    "SELECT story_id, kind FROM reactions WHERE reader_id = ? "
                    "AND by_name IS NULL AND at >= ? AND at <= ? ORDER BY id",
                    (reader, day_start, day_end),
                ).fetchall()
            for r in rows:
                if r["story_id"] in story_ids or r["story_id"] in ed.get("stories", {}):
                    reactions.setdefault(r["story_id"], []).append(r["kind"])
            reasons = {
                sid: entry.get("score_reasons") or []
                for sid, entry in (ed.get("stories") or {}).items()
                if entry.get("score_reasons")
            }
            in_edition = set(story_ids) | set(ed.get("stories") or {})
            seen: dict[str, str] = {}
            seen_at: dict[str, int] = {}  # when: a resurfaced story says what changed since
            if by:
                for r in conn.execute(
                    "SELECT story_id, rev, at FROM seen WHERE reader_id = ? AND by_name = ?",
                    (reader, by),
                ):
                    if r["story_id"] in in_edition:
                        seen[r["story_id"]] = r["rev"]
                        seen_at[r["story_id"]] = r["at"]
            decisions = {r["source_key"]: r["decision"] for r in conn.execute(
                "SELECT source_key, decision FROM source_decisions WHERE reader_id = ?",
                (reader,),
            )}
            saved: list[str] = []
            if by:
                rows = conn.execute(
                    "SELECT story_id FROM saves WHERE reader_id = ? AND lower(by_name) = lower(?) "
                    "AND removed_at IS NULL AND story_id IS NOT NULL AND origin = 'gaceta'",
                    (reader, by),
                ).fetchall()
                saved = [r["story_id"] for r in rows]  # this person's local saves
            # story_id -> rev of the edition as built now: an open page compares it
            # with what it loaded to announce a newer edition (cross-device sync)
            revs = {sid: entry.get("rev") or ""
                    for sid, entry in (ed.get("stories") or {}).items()}
            return self._json(200, {
                "ok": True, "reader": reader, "date": ed.get("date"), "by": by,
                "owner": rrow["name"], "built_at": ed.get("built_at"), "revs": revs,
                "reactions": reactions, "reasons": reasons, "seen": seen, "seen_at": seen_at,
                "source_decisions": decisions, "saved": saved,
                "agent": _agent_label(reader, self.state.data_dir),
            })
        finally:
            conn.close()

    def _api_readers(self) -> None:
        q = parse_qs(urlsplit(self.path).query)
        reader = (q.get("reader") or [""])[0]
        if not READER_RE.match(reader):
            return self._error(400, "bad reader")
        conn = self.state.connect()
        try:
            row = conn.execute("SELECT name FROM readers WHERE id = ?", (reader,)).fetchone()
            owner = row["name"] if row else reader
        finally:
            conn.close()
        household: list[str] = []
        try:
            cfg = config.load_reader(reader, self.state.data_dir)
            household = [str(n) for n in (cfg.get("serve") or {}).get("household") or []]
        except (OSError, KeyError):
            pass
        return self._json(200, {"ok": True, "owner": owner, "household": household})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    gaceta_port = DEFAULT_PORT

    def __init__(self, addr, state: State):
        super().__init__(addr, Handler)
        self.state = state
        self.gaceta_port = addr[1]


def make_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                data_dir: Path | str | None = None) -> Server:
    validate_host(host)
    state = State(Path(data_dir) if data_dir else config.data_dir())
    return Server((host, port), state)


def cmd_serve(args) -> int:
    try:
        httpd = make_server(args.host, args.port)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"gaceta serve http://{args.host}:{args.port}/ (data: {httpd.state.data_dir})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
