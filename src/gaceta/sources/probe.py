"""What is this site? (roadmap 6b v2: verify and describe «Fuentes nuevas»).

A cited host is only a candidate. Before the edition shows it, `gaceta sources
probe` fetches its home page once and asks: does it publish (a live RSS/Atom
feed with recent entries)? What does it call itself, in one line? What did it
publish lately? The answer lands in ``source_probes`` and the build reads it
offline (no network at build time).

- Same SSRF-guarded opener as the RSS adapter; short timeouts, small reads.
- Feed discovery: <link rel="alternate" type="application/(rss|atom)+xml"> on
  the home page, then a few conventional paths. Nothing is guessed: no feed
  that parses with entries → ok=0 and the site is never shown.
- A feed whose newest entry is older than STALE_DAYS counts as not publishing.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from . import rss

PAGE_MAX_BYTES = 600_000
TIMEOUT = 8.0
STALE_DAYS = 90
OK_TTL = 14 * 86400        # a publishing site is re-checked every two weeks
FAIL_TTL = 7 * 86400       # a dud gets another look after a week
FEED_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml",
              "/feed/", "/rss/")
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")
_WS = re.compile(r"\s+")


class _Head(HTMLParser):
    """Just enough of <head>: title, descriptions, site name, lang, feed links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.title = ""
        self.meta: dict[str, str] = {}
        self.feeds: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"].split("-")[0].lower()[:5]
        elif tag == "title" and not self.title:
            self._in_title = True
        elif tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k in ("og:site_name", "og:title", "og:description", "description",
                     "twitter:description") and a.get("content") and k not in self.meta:
                self.meta[k] = a["content"]
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if "alternate" in rel and a.get("type", "").lower() in _FEED_TYPES[:2] and a.get("href"):
                self.feeds.append(a["href"])

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):  # noqa: ANN001
        if self._in_title:
            self.title += data


def _clean(text: str, cap: int) -> str:
    text = _WS.sub(" ", html.unescape(text or "")).strip()
    if len(text) <= cap:
        return text
    cut = text[:cap].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.—-") + "…"


def _get(url: str, max_bytes: int) -> bytes:
    rss._refuse_host(url, allow_http=True)
    req = rss.urllib.request.Request(url, headers={
        "User-Agent": rss.UA,
        "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/atom+xml;q=0.9,*/*;q=0.5",
    })
    with rss._opener.open(req, timeout=TIMEOUT) as r:  # noqa: S310 (guarded opener)
        return r.read(max_bytes)


def _try_feed(url: str, fetch) -> list[dict] | None:  # noqa: ANN001
    try:
        entries = rss.parse_feed(fetch(url, 5_000_000))
    except Exception:  # noqa: BLE001 — any fetch/parse failure means "not a feed"
        return None
    return entries or None


def probe_site(site: str, *, fetch=None, now: float | None = None) -> dict:
    """One site → a source_probes row (dict). Never raises."""
    fetch = fetch or _get
    now = time.time() if now is None else now
    row = {"site": site, "checked_at": int(now), "ok": 0, "feed_url": None, "title": None,
           "description": None, "lang": None, "newest": None, "headlines": "[]", "note": None}
    home = f"https://{site}/"
    head = _Head()
    try:
        raw = fetch(home, PAGE_MAX_BYTES)
        head.feed(raw.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        row["note"] = f"home: {type(exc).__name__}"[:120]
    m = head.meta
    row["title"] = _clean(m.get("og:site_name") or head.title or m.get("og:title") or "", 80) or None
    row["description"] = _clean(m.get("og:description") or m.get("description")
                                or m.get("twitter:description") or "", 160) or None
    row["lang"] = head.lang or None
    candidates = [urljoin(home, h) for h in head.feeds[:3]]
    candidates += [urljoin(home, p) for p in FEED_PATHS]
    seen: set[str] = set()
    for url in candidates:
        if url in seen or urlsplit(url).scheme not in ("http", "https"):
            continue
        seen.add(url)
        entries = _try_feed(url, fetch)
        if not entries:
            continue
        dated = [e["published"] for e in entries if e.get("published")]
        newest = max(dated) if dated else None
        row["feed_url"] = url
        row["newest"] = newest
        row["headlines"] = json.dumps(
            [_clean(e["title"], 110) for e in entries if e.get("title")][:3], ensure_ascii=False)
        if newest is not None and now - newest > STALE_DAYS * 86400:
            row["note"] = f"feed stale: newest {int((now - newest) // 86400)} days old"
        else:
            row["ok"] = 1
            row["note"] = None
        break
    else:
        row["note"] = row["note"] or "no feed found"
    return row


def store(conn: sqlite3.Connection, row: dict) -> None:
    cols = ("site", "checked_at", "ok", "feed_url", "title", "description", "lang",
            "newest", "headlines", "note")
    conn.execute(
        f"INSERT INTO source_probes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        "ON CONFLICT(site) DO UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in cols[1:]),
        tuple(row.get(c) for c in cols))


def due(conn: sqlite3.Connection, sites: list[str], now: float | None = None) -> list[str]:
    """Sites with no probe yet or a probe past its TTL, in the given order."""
    now = time.time() if now is None else now
    have = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT site, checked_at, ok FROM source_probes WHERE site IN ({','.join('?' * len(sites))})",
        tuple(sites))} if sites else {}
    out = []
    for s in sites:
        if s not in have:
            out.append(s)
            continue
        at, ok = have[s]
        if now - int(at or 0) >= (OK_TTL if ok else FAIL_TTL):
            out.append(s)
    return out
