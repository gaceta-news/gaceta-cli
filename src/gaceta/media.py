"""Media discovery + local caching for edition items (schema v5 ``media`` table).

Three sources of media, in priority order:
- YouTube: item id ``yt:<ID>`` or a youtube.com/youtu.be watch URL → thumbnail
  ``https://i.ytimg.com/vi/<ID>/hqdefault.jpg`` (downloaded).
- Nostr: first https image URL in the item body → downloaded; first .mp4/.webm →
  ``kind='video'`` (remote src only, never downloaded).
- Other http(s) items: GET the page, parse og:image / twitter:image
  (html.parser), resolve relative URLs → ``kind='image'`` (downloaded).

Downloads are https-only, magic-byte validated, capped at 3 MB, and stored as
``<data>/site/media/<sha1(src_url)[:16]>.<ext>``. Never fetches private, loopback,
link-local or tailnet (100.64/10) hosts — the hostname is resolved and every
address refused unless globally routable (SSRF guard). Incremental: an item with
a terminal media row (ok/none) is skipped; error rows are retried after 7 days.
Politeness: at most 4 concurrent requests and at most 1 per host at a time.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import shutil
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

UA = "gaceta/0.1 (+personal reader)"
PAGE_TIMEOUT = 8
PAGE_MAX_BYTES = 1_500_000
IMAGE_MAX_BYTES = 3_000_000
MAX_CONCURRENCY = 4
ERROR_RETRY_SECONDS = 7 * 86400

YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# a cached file on disk: <sha1(src_url)[:16]>.<ext> (local_name below)
CACHED_NAME_RE = re.compile(r"^[a-f0-9]{16}\.(?:jpg|png|webp|gif)$")
# how editions reference cached files, inside their immutable snapshots:
# story media src / source thumbs, both "media/<name>"
_REF_IN_SNAPSHOT_RE = re.compile(r"media/([a-f0-9]{16}\.(?:jpg|png|webp|gif))")
_EXT = r"[^\s<>\"']"
NOSTR_IMAGE_RE = re.compile(rf"https://{_EXT}+\.(?:jpe?g|png|webp|gif)(?:[?#]{_EXT}*)?", re.IGNORECASE)
NOSTR_VIDEO_RE = re.compile(rf"https://{_EXT}+\.(?:mp4|webm)(?:[?#]{_EXT}*)?", re.IGNORECASE)
THUMB_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif)(?:[?#].*)?$", re.IGNORECASE)


class MediaError(Exception):
    """Fetch/validation refused or failed. Message is stored in media.error."""


# ---------------------------------------------------------------- SSRF guard


def _refuse_host(url: str, *, allow_http: bool = False) -> None:
    """Refuse non-http(s) schemes and any host that resolves to a non-global
    address (loopback, private, link-local, tailnet 100.64/10, ...)."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise MediaError(f"bad url: {url!r}") from exc
    scheme = parts.scheme.lower()
    allowed = ("http", "https") if allow_http else ("https",)
    if scheme not in allowed:
        raise MediaError(f"scheme {scheme or '(none)'!r} not allowed for {url!r}")
    host = parts.hostname
    if not host:
        raise MediaError(f"no host in {url!r}")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise MediaError(f"dns lookup failed for {host}: {exc}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise MediaError(f"host {host} resolves to non-global address {addr}")


def _read_url(url: str, *, allow_http: bool = False, max_bytes: int) -> bytes:
    """Fetch at most ``max_bytes`` after the SSRF guard. Raises MediaError."""
    _refuse_host(url, allow_http=allow_http)
    req = Request(url, headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=PAGE_TIMEOUT) as resp:  # noqa: S310 (scheme guarded above)
            return resp.read(max_bytes + 1)
    except MediaError:
        raise
    except (URLError, OSError, ValueError) as exc:
        raise MediaError(f"fetch failed: {exc}") from exc


# ------------------------------------------------------------ page parsing


class _MetaParser(HTMLParser):
    """Collects og:image / twitter:image meta content from the page head."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        d = dict(attrs)
        key = (d.get("property") or d.get("name") or "").strip().lower()
        if key in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
            content = (d.get("content") or "").strip()
            if content:
                self.images.append(content)


def parse_page_images(html_text: str) -> list[str]:
    parser = _MetaParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    return parser.images


# ------------------------------------------------------- image inspection

_MAGIC_EXTS = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),
)


def sniff_image(data: bytes) -> str | None:
    """Return the extension implied by the magic bytes, or None."""
    if len(data) < 12:
        return None
    for magic, ext in _MAGIC_EXTS:
        if data.startswith(magic):
            if ext == "webp" and data[8:12] != b"WEBP":
                return None
            return ext
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Cheap header parse: PNG, GIF and JPEG only (webp → None)."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data.startswith(b"\xff\xd8\xff"):
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seglen = int.from_bytes(data[i + 2:i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + seglen
    except (ValueError, IndexError):
        return None
    return None


def local_name(src_url: str, ext: str) -> str:
    digest = hashlib.sha1(src_url.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{digest}.{ext}"


# --------------------------------------------------------- id extraction


def youtube_id(item_id: str, url: str | None) -> str | None:
    """11-char video id from 'yt:<ID>' ids or watch/youtu.be URLs, else None."""
    text = str(item_id or "")
    if text.startswith("yt:"):
        candidate = text[3:]
        return candidate if YOUTUBE_ID_RE.fullmatch(candidate) else None
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    query = parts.query
    if host in ("youtube.com", "youtube-nocookie.com"):
        if parts.path == "/watch":
            for pair in query.split("&"):
                if pair.startswith("v="):
                    candidate = pair[2:]
                    return candidate if YOUTUBE_ID_RE.fullmatch(candidate) else None
        for pair in query.split("&"):
            if pair.startswith("vi=") or pair.startswith("video_id="):
                candidate = pair.split("=", 1)[1]
                if YOUTUBE_ID_RE.fullmatch(candidate):
                    return candidate
    if host == "youtu.be":
        candidate = parts.path.lstrip("/")[:11]
        return candidate if YOUTUBE_ID_RE.fullmatch(candidate) else None
    return None


# ------------------------------------------------------------- downloader


class MediaStore:
    """Downloads validated images into <data>/site/media/."""

    def __init__(self, media_dir: Path):
        self.media_dir = media_dir
        self.bytes_downloaded = 0

    def download(self, src_url: str) -> tuple[str, int | None, int | None]:
        """Fetch, validate and store one image. Returns (filename, w, h)."""
        if THUMB_EXT_RE.search(src_url) is None:
            raise MediaError(f"not an image url: {src_url!r}")
        data = _read_url(src_url, allow_http=False, max_bytes=IMAGE_MAX_BYTES)
        if len(data) > IMAGE_MAX_BYTES:
            raise MediaError(f"image larger than {IMAGE_MAX_BYTES} bytes")
        ext = sniff_image(data)
        if ext is None:
            raise MediaError("magic bytes are not jpeg/png/webp/gif")
        name = local_name(src_url, ext)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        (self.media_dir / name).write_bytes(data)
        self.bytes_downloaded += len(data)
        w, h = image_dimensions(data) or (None, None)
        return name, w, h


# --------------------------------------------------------------- pipeline


def _candidate_items(conn: sqlite3.Connection, reader_id: str, cutoff: int) -> list[dict]:
    """Items of the reader's stories with last_seen >= cutoff (noted/told only)."""
    return [dict(r) for r in conn.execute(
        """
        SELECT DISTINCT si.item_id, i.url, i.body, i.title, s.kind AS source_kind
        FROM story_items si
        JOIN stories st ON st.id = si.story_id
        JOIN items i ON i.id = si.item_id
        LEFT JOIN sources s ON s.id = i.source_id
        JOIN annotations a ON a.item_id = si.item_id AND a.reader_id = st.reader_id
        WHERE st.reader_id = ? AND st.last_seen >= ?
          AND a.verdict IN ('noted', 'told')
        ORDER BY st.last_seen DESC, si.item_id
        """,
        (reader_id, cutoff),
    )]


def _existing_rows(conn: sqlite3.Connection, item_ids: list[str]) -> dict[tuple[str, str], dict]:
    rows: dict[tuple[str, str], dict] = {}
    for chunk_start in range(0, len(item_ids), 500):
        chunk = item_ids[chunk_start:chunk_start + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT item_id, kind, status, fetched_at, error FROM media WHERE item_id IN ({marks})",
            chunk,
        ):
            rows[(r["item_id"], r["kind"])] = dict(r)
    return rows


def _write_row(conn: sqlite3.Connection, item_id: str, kind: str, *, status: str,
               src_url: str | None = None, local_path: str | None = None,
               video_id: str | None = None, width=None, height=None,
               error: str | None = None, now: int) -> None:
    conn.execute(
        """
        INSERT INTO media (item_id, kind, src_url, local_path, video_id, width, height,
                           status, error, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id, kind) DO UPDATE SET
          src_url = excluded.src_url, local_path = excluded.local_path,
          video_id = excluded.video_id, width = excluded.width, height = excluded.height,
          status = excluded.status, error = excluded.error, fetched_at = excluded.fetched_at
        """,
        (item_id, kind, src_url, local_path, video_id, width, height, status, error, now),
    )


def free_gb(path) -> float:
    """Free space (decimal GB) on the filesystem holding ``path``, walking up
    to the nearest existing ancestor. Stat failures fail open (infinite): a
    disk check must never be the reason media goes missing."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 1e9
    except OSError:
        return float("inf")


def collect_media(conn: sqlite3.Connection, reader_id: str, data_dir: Path, *,
                  days: int = 14, max_fetch: int = 300, min_free_gb: float = 0.0,
                  now: int | None = None) -> dict:
    """Fill the media table for the reader's recent items. One network request
    budget of ``max_fetch`` page GETs + image downloads, 4 workers, 1 per host.
    ``min_free_gb`` > 0 skips the whole fetch (one line for the caller to log)
    when the media dir's filesystem is below that much free space."""
    now = int(now if now is not None else time.time())
    cutoff = now - days * 86400
    data_dir = Path(data_dir)
    store = MediaStore(data_dir / "site" / "media")

    if min_free_gb and min_free_gb > 0:
        free = free_gb(store.media_dir)
        if free < min_free_gb:
            return {"candidates": 0, "planned": 0, "bytes_downloaded": 0,
                    "skipped_low_disk": True, "free_gb": round(free, 2)}

    items = _candidate_items(conn, reader_id, cutoff)
    existing = _existing_rows(conn, [it["item_id"] for it in items])

    def settled(item_id: str, kind: str) -> bool:
        row = existing.get((item_id, kind))
        if row is None:
            return False
        if row["status"] == "error":
            age = now - (row["fetched_at"] or 0)
            return age < ERROR_RETRY_SECONDS
        return True

    # plan: (item, kind, action) — action is 'thumb' (download), 'page' (GET then
    # download), or 'video' (nothing to fetch)
    plan: list[tuple[dict, str, str]] = []
    budget = max_fetch
    for it in items:
        vid = youtube_id(it["item_id"], it["url"])
        if vid:
            if not settled(it["item_id"], "youtube"):
                if budget < 1:
                    break
                plan.append((it, "youtube", "thumb"))
                budget -= 1
            continue
        if str(it.get("source_kind") or "") == "nostr":
            body = str(it.get("body") or "")
            img = NOSTR_IMAGE_RE.search(body)
            mp4 = NOSTR_VIDEO_RE.search(body)
            if img and not settled(it["item_id"], "image"):
                if budget < 1:
                    break
                plan.append((dict(it, _nostr_url=img.group(0)), "image", "thumb"))
                budget -= 1
            if mp4 and not settled(it["item_id"], "video"):
                plan.append((dict(it, _nostr_url=mp4.group(0)), "video", "video"))
            continue
        url = str(it.get("url") or "").strip()
        if url.startswith(("http://", "https://")) and not settled(it["item_id"], "image"):
            if budget < 2:  # page + image
                break
            plan.append((it, "image", "page"))
            budget -= 2

    host_locks: dict[str, threading.Lock] = {}
    lock_guard = threading.Lock()
    results: list[tuple] = []  # (item_id, kind, fields-dict) — written by the main thread

    def host_key(url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    def run_one(entry) -> None:
        it, kind, action = entry
        item_id = it["item_id"]
        fields: dict = {}
        try:
            if kind == "youtube":
                vid = youtube_id(item_id, it["url"])
                if not vid:
                    raise MediaError("youtube id vanished")
                thumb_url = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                with lock_guard:
                    lock = host_locks.setdefault("i.ytimg.com", threading.Lock())
                with lock:
                    name, w, h = store.download(thumb_url)
                fields = {"status": "ok", "src_url": thumb_url, "local_path": name,
                          "video_id": vid, "width": w, "height": h}
            elif action == "thumb":
                src = it["_nostr_url"]
                with lock_guard:
                    lock = host_locks.setdefault(host_key(src), threading.Lock())
                with lock:
                    name, w, h = store.download(src)
                fields = {"status": "ok", "src_url": src,
                          "local_path": name, "width": w, "height": h}
            elif action == "video":
                src = it["_nostr_url"]
                fields = {"status": "ok", "src_url": src}
            else:  # page fetch
                html_bytes = _read_url(it["url"], allow_http=True, max_bytes=PAGE_MAX_BYTES)
                if len(html_bytes) > PAGE_MAX_BYTES:
                    raise MediaError(f"page larger than {PAGE_MAX_BYTES} bytes")
                images = parse_page_images(html_bytes.decode("utf-8", "replace"))
                og = None
                for candidate in images:
                    absolute = urljoin(it["url"], candidate)
                    if absolute.startswith("http://"):  # most sites serve both; downloads stay https-only
                        absolute = "https://" + absolute[len("http://"):]
                    if absolute.startswith("https://"):
                        og = absolute
                        break
                if og is None:
                    if images and all(
                        not urljoin(it["url"], c).startswith("https://") for c in images
                    ):
                        raise MediaError("og:image is not https")
                    fields = {"status": "none"}
                else:
                    with lock_guard:
                        lock = host_locks.setdefault(host_key(og), threading.Lock())
                    with lock:
                        name, w, h = store.download(og)
                    fields = {"status": "ok", "src_url": og,
                              "local_path": name, "width": w, "height": h}
        except MediaError as exc:
            fields = {"status": "error", "error": str(exc)[:300]}
        with lock_guard:
            results.append((item_id, kind, fields))

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        list(pool.map(run_one, plan))
    for item_id, kind, fields in results:
        _write_row(conn, item_id, kind, now=now, **fields)
    conn.commit()

    return {
        "candidates": len(items),
        "planned": len(plan),
        "bytes_downloaded": store.bytes_downloaded,
    }


# ------------------------------------------------------------------ prune


def prune_media(conn: sqlite3.Connection, data_dir: Path, *,
                days: int = 30, dry_run: bool = False,
                now: int | None = None) -> dict:
    """Delete cached media files nothing needs anymore, and nothing else.

    A file goes only when BOTH hold: no edition built in the last ``days``
    references it (story media and source thumbs, read from the immutable
    snapshots — the media dir is shared across readers, so every reader's
    editions count) and its mtime is older than ``days``. Editions inside the
    window are read fail-closed: an unreadable snapshot aborts the prune
    rather than guessing at what it references.

    Deleted names also drop their ``media`` rows, so if a still-active story
    wants one back, the next `gaceta media` fetch re-downloads it instead of
    pointing at a hole. ``dry_run`` reports without touching anything.
    """
    now = int(now if now is not None else time.time())
    cutoff = now - int(days) * 86400
    data_dir = Path(data_dir)
    media_dir = data_dir / "site" / "media"

    referenced: set[str] = set()
    rows = conn.execute(
        "SELECT path FROM editions WHERE built_at >= ? ORDER BY built_at", (cutoff,)
    ).fetchall()
    for row in rows:
        path = Path(row["path"])
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MediaError(
                f"cannot read the edition snapshot {path} built inside the last "
                f"{days} days — prune refuses to guess what it references") from exc
        referenced.update(_REF_IN_SNAPSHOT_RE.findall(text))

    deleted = freed = considered = kept = 0
    if media_dir.is_dir():
        for entry in sorted(media_dir.iterdir()):
            if not entry.is_file() or not CACHED_NAME_RE.fullmatch(entry.name):
                continue
            considered += 1
            if entry.name in referenced:
                kept += 1
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff:  # younger than the window: never touched
                continue
            if not dry_run:
                entry.unlink(missing_ok=True)
                conn.execute("DELETE FROM media WHERE local_path = ?", (entry.name,))
            deleted += 1
            freed += st.st_size
    if not dry_run and deleted:
        conn.commit()

    return {"days": int(days), "deleted": deleted, "bytes_freed": freed,
            "considered": considered, "referenced": kept, "dry_run": bool(dry_run)}
