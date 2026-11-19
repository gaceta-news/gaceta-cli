"""RSS/Atom source adapter (incl. YouTube channel feeds).

Reader config:

    [sources.rss]
    max_age_days = 14        # older entries are ignored (default 14)
    max_items = 20           # per feed per run, newest first (default 20)
    [[sources.rss.feeds]]
    id = "mongabay-latam"    # reader-local slug for section rules and logging
    name = "Mongabay Latam"
    url = "https://es.mongabay.com/feed/"
    # kind = "youtube"       # optional; inferred from youtube.com/feeds URLs
    # file = "sample.xml"    # offline alternative: read a local feed file instead
                             # of url (relative paths resolve against the data dir)

- No observer layer: every fresh entry is a candidate (verdict 'noted'); ranking and the
  reader's own verdicts decide what surfaces.
- Read-only and polite: one GET per feed per run, short timeout, a failing feed is reported
  and skipped, never fatal. stdlib only (urllib + xml.etree). Fetches and redirects pass the
  same SSRF guard as media (http(s) only, hosts must resolve to global addresses).
- Identity comes from the canonical feed URL, NOT the configured slug: source id is
  'rss:<sha12(url)>' and item ids hash (feed url, guid). `items` is a shared table — two
  readers using the same slug for different feeds must never collide. The slug stays a
  display alias.
- content_public=True only for proven-public evidence: feeds are publicly published content,
  and a video's transcript must carry public provenance (anonymous fetch); cookies-fetched or
  unknown-provenance transcripts get a ':u' source variant labelled private (fail closed).
"""
from __future__ import annotations

import hashlib
import html
import http.client
import ipaddress
import re
import socket
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from urllib.parse import urlsplit

from ..media import MediaError, _refuse_host
from ..transcripts import Transcript, video_id
from .base import RawItem, RawSource

UA = "Mozilla/5.0 (compatible; gaceta-rss/0.1; personal newspaper)"
BODY_CAP = 8000
_TAG = re.compile(r"<[^>]+>")
_NS_TAG = re.compile(r"(<\/?)[A-Za-z_][\w.-]*:")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")
_BLOCK = re.compile(r"</?(p|br|div|li|h[1-6]|blockquote|tr)\b[^>]*>", re.I)
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    t = _SCRIPT.sub("", text)
    t = _BLOCK.sub("\n", t)
    t = html.unescape(_TAG.sub("", t))
    t = "\n".join(_WS.sub(" ", line).strip() for line in t.splitlines())
    return _NL.sub("\n\n", t).strip()


def _parse_date(text: str | None) -> int | None:
    s = (text or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)           # RFC 822 (RSS)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO 8601 (Atom)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.split(":")[-1]


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el.iter():
        if c is not el and _local(c.tag) in names:
            return c
    return None


def _direct(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _body_text(el: ET.Element | None) -> str:
    """Atom content/summary text. XHTML content (type="xhtml") carries its prose in
    child elements, so serialize the children (namespaces stripped so block tags
    stay recognizable) and let strip_html remove the markup; plain/text constructs
    just concatenate their text nodes."""
    if el is None:
        return ""
    if el.get("type") == "xhtml" and len(el):
        raw = "".join(ET.tostring(c, encoding="unicode") for c in el)
        return _NS_TAG.sub(r"\1", raw).strip()
    return "".join(el.itertext()).strip()


def _atom_link(entry: ET.Element) -> str | None:
    best = None
    for c in entry:
        if _local(c.tag) == "link":
            rel = c.get("rel", "alternate")
            if rel == "alternate" and c.get("href"):
                return c.get("href")
            best = best or c.get("href")
    return best


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """RSS 2.0 / RSS 1.0 / Atom → [{guid, title, url, published, body, author}] (feed order)."""
    root = ET.fromstring(xml_bytes)
    out: list[dict] = []
    entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    for e in entries:
        is_atom = _local(e.tag) == "entry"
        title = strip_html(_text(_direct(e, "title")))
        if is_atom:
            url = _atom_link(e)
            guid = _text(_direct(e, "id")) or url
            published = _parse_date(_text(_direct(e, "published")) or _text(_direct(e, "updated")))
            # never `el or el`: an Element with no children is falsy
            body = _body_text(_direct(e, "content")) or _body_text(_direct(e, "summary"))
            if not body:   # YouTube: media:group/media:description
                body = _text(_child(e, "description"))
            author = _text(_child(e, "name")) or None
        else:
            url = _text(_direct(e, "link")) or None
            guid = _text(_direct(e, "guid")) or url
            published = _parse_date(_text(_direct(e, "pubDate")) or _text(_direct(e, "date")))
            enc = _direct(e, "encoded")             # content:encoded beats description
            body = _text(enc) or _text(_direct(e, "description"))
            author = _text(_direct(e, "creator")) or _text(_direct(e, "author")) or None
        if not (guid or url or title):
            continue
        out.append({
            "guid": guid or f"{title}|{published}", "title": title or None, "url": url,
            "published": published, "body": strip_html(body)[:BODY_CAP] or None, "author": author,
        })
    return out


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Fail fast on redirect targets that are not http(s) or whose host is not
    global; every hop's connection is validated again by the guarded handlers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _refuse_host(newurl, allow_http=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _checked_socket(host: str, port: int, timeout, source_address):  # noqa: ANN001
    """Connect only to addresses that resolve globally — the check is bound to the
    connection itself, not to a pre-flight lookup a rebinding DNS could sidestep."""
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise MediaError(f"host {host} resolves to non-global address {info[4][0]}")
    return socket.create_connection((str(infos[0][4][0]), port), timeout=timeout,
                                    source_address=source_address)


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        # source_address exists at runtime; http.client's stubs leave it untyped.
        self.sock = _checked_socket(self.host, self.port or 80, self.timeout,
                                    getattr(self, "source_address", None))


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = _checked_socket(self.host, self.port or 443, self.timeout,
                               getattr(self, "source_address", None))
        # TLS/SNI keep the original hostname identity while the socket is bound
        # to the validated address; default context verifies certificates.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # context only: check_hostname kwarg was dropped from http.client in 3.14
        return self.do_open(_GuardedHTTPSConnection, req,
                            context=getattr(self, "_context", None))


_opener = urllib.request.build_opener(_GuardedHTTPHandler, _GuardedHTTPSHandler, _GuardedRedirect)


def _fetch(url: str, timeout: float = 20.0) -> bytes:
    _refuse_host(url, allow_http=True)  # fail fast on scheme; addresses enforced at connect
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with _opener.open(req, timeout=timeout) as r:
        return r.read(5_000_000)


def _feed_key(url: str) -> str:
    """Canonical feed identity: lowercase host, IPv6 re-bracketed before a nondefault
    port (``[v6]:8080`` and ``[v6:8080]`` must not merge), default ports dropped, no
    fragment. Two readers pasting the same feed get the same key; the same slug with
    different feeds does not collide (items is a shared table)."""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = p.port
    if port and not (port == 80 and p.scheme == "http") and not (port == 443 and p.scheme == "https"):
        host = f"{host}:{port}"
    return f"{p.scheme}://{host}{p.path or '/'}" + (f"?{p.query}" if p.query else "")


def source_identity(feed: dict) -> tuple[str, str]:
    """Return the shared RSS source id and canonical feed URL."""
    key = _feed_key(str(feed["url"]))
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return f"rss:{h[:12]}", key


def _is_youtube(url: str | None) -> bool:
    try:
        host = urlsplit(url or "").hostname or ""
    except ValueError:
        return False
    return host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com")


def _kind(feed: dict) -> str:
    if _is_youtube(feed.get("url")):
        return "youtube"
    return str(feed.get("kind") or "rss").strip().lower()


class RssAdapter:
    name = "rss"

    def __init__(self, cfg: dict, fetch: Callable[[str], bytes] = _fetch, now: Callable[[], float] = time.time,
                 log=None, transcript: Callable[[str], "Transcript | None"] | None = None):
        self.feeds = []
        for f in cfg.get("feeds") or []:
            if f.get("file") and not f.get("url"):  # local feed: identity from its file URI
                f = {**f, "url": Path(str(f["file"])).resolve().as_uri()}
            if f.get("id") and f.get("url"):
                self.feeds.append(f)
        self.max_age = int(cfg.get("max_age_days", 14)) * 86400
        self.max_items = int(cfg.get("max_items", 20))
        self.fetch = fetch
        self.now = now
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.errors: list[tuple[str, str]] = []
        self.transcript = transcript
        self.videos_without_transcript = 0

    def items(self, since: int | None = None) -> Iterator[RawItem]:
        cutoff = int(self.now()) - self.max_age
        if since is not None:
            cutoff = max(cutoff, since)
        for feed in self.feeds:
            fid = str(feed["id"])
            sid, key = source_identity(feed)
            try:
                if feed.get("file"):  # offline feeds: local file, no network
                    raw = Path(str(feed["file"])).read_bytes()
                    entries = parse_feed(raw)
                else:
                    entries = parse_feed(self.fetch(feed["url"]))
            except Exception as exc:  # noqa: BLE001 one bad feed never stops the run
                self.errors.append((fid, type(exc).__name__))
                self.log(f"rss: feed {fid} failed ({type(exc).__name__})")
                continue
            src = RawSource(id=sid, kind=_kind(feed), name=feed.get("name") or fid,
                            url=feed.get("site") or feed["url"], content_public=True)
            fresh = [e for e in entries if e["published"] is None or e["published"] >= cutoff]
            fresh.sort(key=lambda e: e["published"] or 0, reverse=True)
            for e in fresh[: self.max_items]:
                item_source = src
                is_video = src.kind == "youtube" or _is_youtube(e["url"])
                body = e["body"]
                if is_video:
                    vid = video_id(e["url"])
                    t = self.transcript(vid) if (vid and self.transcript) else None
                    if not t or not t.text:
                        self.videos_without_transcript += 1
                        continue
                    body = t.text
                    # Item identity must not depend on provenance (the same video,
                    # refetched anonymously later, updates the same row); the ':u'
                    # SOURCE variant keeps unproven transcripts out of the public label.
                    video_sid = sid if src.kind == "youtube" else f"{sid}:videos"
                    item_source = RawSource(id=video_sid + ("" if t.public else ":u"),
                                            kind="youtube", name=src.name, url=src.url,
                                            content_public=t.public)
                h = hashlib.sha1(f"{key}\x00{e['guid']}".encode("utf-8")).hexdigest()[:16]
                yield RawItem(
                    id=f"{sid}:{h}", source=item_source, author=e["author"] or src.name, title=e["title"],
                    url=e["url"], published=e["published"], body=body, verdict="noted",
                )
