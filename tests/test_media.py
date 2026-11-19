"""Media discovery/caching (gaceta.media) and edition media emission. No network:
every fetch is monkeypatched."""
from __future__ import annotations

import socket
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin

import pytest

import gaceta.db as gdb
import gaceta.media as media_mod
from gaceta.edition.build import build_edition

# 1x1 transparent PNG (width=1, height=1 in IHDR)
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a4944415478da6360000002000154a24f5b"
    "0000000049454e44ae426082"
)
JPEG_MIN = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def fake_resolver(ip: str):
    def resolve(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    return resolve


# --------------------------------------------------------------- SSRF guard


def test_download_refuses_plain_http(monkeypatch):
    monkeypatch.setattr(media_mod.socket, "getaddrinfo", fake_resolver("93.184.216.34"))
    with pytest.raises(media_mod.MediaError, match="scheme"):
        media_mod._refuse_host("http://example.com/pic.png")


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.5", "192.168.1.9",
                                "169.254.1.1", "::1", "fd00::5"])
def test_refuses_non_global_addresses(monkeypatch, ip):
    monkeypatch.setattr(media_mod.socket, "getaddrinfo", fake_resolver(ip))
    with pytest.raises(media_mod.MediaError, match="non-global"):
        media_mod._refuse_host(f"https://host.example.com/pic.png")


def test_localhost_name_resolving_private_is_refused(monkeypatch):
    monkeypatch.setattr(media_mod.socket, "getaddrinfo", fake_resolver("127.0.0.1"))
    with pytest.raises(media_mod.MediaError, match="non-global"):
        media_mod._refuse_host("https://localhost/pic.png")


def test_allows_global_https(monkeypatch):
    monkeypatch.setattr(media_mod.socket, "getaddrinfo", fake_resolver("93.184.216.34"))
    media_mod._refuse_host("https://example.com/pic.png")  # no raise


# -------------------------------------------------------- magic bytes etc.


def test_download_rejects_non_image_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(media_mod, "_read_url", lambda url, **kw: b"<html>not an image</html>")
    store = media_mod.MediaStore(tmp_path)
    with pytest.raises(media_mod.MediaError, match="magic"):
        store.download("https://example.com/pic.png")


def test_download_validates_and_parses_png(monkeypatch, tmp_path):
    monkeypatch.setattr(media_mod, "_read_url", lambda url, **kw: PNG_1x1)
    store = media_mod.MediaStore(tmp_path)
    name, w, h = store.download("https://example.com/pic.png")
    assert name.endswith(".png")
    assert (w, h) == (1, 1)
    assert (tmp_path / name).read_bytes() == PNG_1x1


def test_page_images_and_relative_resolution():
    page = ('<html><head><meta property="og:image" content="/img/rel.png">'
            '<meta name="twitter:image" content="https://cdn.example.com/t.jpg"></head></html>')
    images = media_mod.parse_page_images(page)
    assert images == ["/img/rel.png", "https://cdn.example.com/t.jpg"]
    assert urljoin("http://example.com/post/1", "/img/rel.png") == "http://example.com/img/rel.png"


def test_youtube_id_extraction():
    assert media_mod.youtube_id("yt:TWoS5Nmyoc8", None) == "TWoS5Nmyoc8"
    assert media_mod.youtube_id("i", "https://www.youtube.com/watch?v=TWoS5Nmyoc8") == "TWoS5Nmyoc8"
    assert media_mod.youtube_id("i", "https://youtu.be/TWoS5Nmyoc8?t=10") == "TWoS5Nmyoc8"
    assert media_mod.youtube_id("i", "https://www.youtube.com/watch?v=short") is None
    assert media_mod.youtube_id("yt:bad!id", None) is None
    assert media_mod.youtube_id("yt:with/sub:dir", None) is None
    assert media_mod.youtube_id("i", "https://evil.example.com/watch?v=TWoS5Nmyoc8") is None


# -------------------------------------------------------------- pipeline


def make_db(tmp_path: Path) -> sqlite3.Connection:
    conn = gdb.connect(tmp_path / "gaceta.db")
    gdb.init(conn)
    conn.execute("INSERT INTO readers (id, name, langs, created_at) VALUES ('r1', 'R', 'es', 0)")
    now = int(time.time())
    conn.executemany(
        "INSERT INTO sources (id, kind) VALUES (?, ?)",
        [("youtube:c", "youtube"), ("nostr:n", "nostr"), ("feed:p", "rss")],
    )
    conn.executemany(
        "INSERT INTO items (id, source_id, title, url, published, body, ingested_at, origin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'test')",
        [
            ("yt:AAAAAAAAAAA", "youtube:c", "Video uno",
             "https://www.youtube.com/watch?v=AAAAAAAAAAA", now, "body", now),
            ("nostr:1", "nostr:n", "Nota con media", "https://nostr.example/p/1", now,
             "mira https://nostr.build/i/abc123.png y https://v.nostr.build/clip.mp4 fin", now),
            ("feed:post1", "feed:p", "Un artículo", "http://example.com/post/1", now, "", now),
        ],
    )
    conn.executemany(
        "INSERT INTO annotations (reader_id, item_id, verdict, item_privacy, privacy_reason) "
        "VALUES ('r1', ?, 'noted', 'public', 'default')",
        [("yt:AAAAAAAAAAA",), ("nostr:1",), ("feed:post1",)],
    )
    conn.execute(
        "INSERT INTO stories (id, reader_id, title, privacy, first_seen, last_seen) "
        "VALUES ('s1', 'r1', 'Historia', 'public', ?, ?)", (now, now),
    )
    conn.executemany(
        "INSERT INTO story_items (story_id, item_id, role) VALUES ('s1', ?, 'origin')",
        [("yt:AAAAAAAAAAA",), ("nostr:1",), ("feed:post1",)],
    )
    conn.commit()
    return conn


def fake_fetch(url, *, allow_http=False, max_bytes=0):
    pages = {
        "http://example.com/post/1":
            b'<html><head><meta property="og:image" content="/img/og.png"></head></html>',
    }
    if url in pages:
        return pages[url]
    if url == "https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg":
        return JPEG_MIN
    if url == "https://nostr.build/i/abc123.png":
        return PNG_1x1
    if url == "https://example.com/img/og.png":
        return PNG_1x1
    raise AssertionError(f"unexpected fetch: {url}")


def test_collect_media_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(media_mod, "_read_url", fake_fetch)
    conn = make_db(tmp_path)
    result = media_mod.collect_media(conn, "r1", tmp_path, max_fetch=10)
    assert result["candidates"] == 3 and result["planned"] == 4  # nostr item yields image + video jobs

    rows = {(r["item_id"], r["kind"]): dict(r) for r in conn.execute("SELECT * FROM media")}
    yt = rows[("yt:AAAAAAAAAAA", "youtube")]
    assert yt["status"] == "ok" and yt["video_id"] == "AAAAAAAAAAA"
    assert yt["local_path"] and (tmp_path / "site" / "media" / yt["local_path"]).exists()
    nostr_img = rows[("nostr:1", "image")]
    assert nostr_img["status"] == "ok" and nostr_img["src_url"] == "https://nostr.build/i/abc123.png"
    nostr_vid = rows[("nostr:1", "video")]
    assert nostr_vid["status"] == "ok" and nostr_vid["src_url"] == "https://v.nostr.build/clip.mp4"
    og = rows[("feed:post1", "image")]
    assert og["status"] == "ok" and og["src_url"] == "https://example.com/img/og.png"

    # incremental: nothing left to do on a rerun
    again = media_mod.collect_media(conn, "r1", tmp_path, max_fetch=10)
    assert again["planned"] == 0


def test_collect_media_none_page(tmp_path, monkeypatch):
    monkeypatch.setattr(media_mod, "_read_url", lambda url, **kw: b"<html><head></head></html>")
    conn = make_db(tmp_path)
    media_mod.collect_media(conn, "r1", tmp_path, max_fetch=10)
    row = conn.execute(
        "SELECT status FROM media WHERE item_id='feed:post1' AND kind='image'"
    ).fetchone()
    assert row["status"] == "none"


def test_collect_media_respects_max_fetch(tmp_path, monkeypatch):
    calls = []

    def fetch(url, **kw):
        calls.append(url)
        return fake_fetch(url, **kw)

    monkeypatch.setattr(media_mod, "_read_url", fetch)
    conn = make_db(tmp_path)
    # 3 units need 1 (yt) + 1 (nostr img) + 2 (page+og) = 4; give only 1
    result = media_mod.collect_media(conn, "r1", tmp_path, max_fetch=1)
    assert result["planned"] <= 1
    assert len(calls) <= 1


# --------------------------------------------------- edition media emission


def test_build_edition_includes_media(tmp_path, monkeypatch):
    monkeypatch.setattr(media_mod, "_read_url", fake_fetch)
    conn = make_db(tmp_path)
    media_mod.collect_media(conn, "r1", tmp_path, max_fetch=10)
    edition = build_edition(conn, "r1", {"name": "R"}, data_dir=tmp_path, force=True)
    story = edition["stories"]["s1"]
    assert [m["type"] for m in story["media"][:2]] == ["youtube", "image"]
    lead = story["media"][0]
    assert lead["video_id"] == "AAAAAAAAAAA"
    assert lead["src"].startswith("media/") and ".." not in lead["src"]
    video = [m for m in story["media"] if m["type"] == "video"]
    assert video and video[0]["src"] == "https://v.nostr.build/clip.mp4"
    with_thumb = [s for s in story["sources"] if "thumb" in s]
    assert with_thumb and all(s["thumb"].startswith("media/") for s in with_thumb)
