"""Media discovery/caching (gaceta.media) and edition media emission. No network:
every fetch is monkeypatched."""
from __future__ import annotations

import json
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


# ------------------------------------------------------ min-free-gb + prune

def test_min_free_gb_skips_fetch_and_off_by_default(tmp_path, monkeypatch):
    conn = make_db(tmp_path)
    monkeypatch.setattr(media_mod, "free_gb", lambda path: 0.4)
    result = media_mod.collect_media(conn, "r1", tmp_path, max_fetch=10, min_free_gb=1.5)
    assert result["skipped_low_disk"] is True
    assert result["planned"] == 0 and result["bytes_downloaded"] == 0
    assert result["free_gb"] == 0.4
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    # 0 (default) = off: the fetch proceeds as before
    result = media_mod.collect_media(conn, "r1", tmp_path, max_fetch=10, min_free_gb=0)
    assert "skipped_low_disk" not in result and result["candidates"] == 3


def _media_file(tmp_path, name, size=100, mtime=None):
    media = tmp_path / "site" / "media"
    media.mkdir(parents=True, exist_ok=True)
    path = media / name
    path.write_bytes(b"x" * size)
    import os
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_edition(conn, tmp_path, names, built_at, day="2026-11-01"):
    """An editions row whose snapshot references the given cached names."""
    snap = tmp_path / "editions" / "r1" / f"{day}-private.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({
        "date": day, "built_at": "2026-11-01T06:00:00-05:00",
        "stories": {"s": {"title": "T", "privacy": "public",
                          "media": [{"type": "image", "src": f"media/{n}"} for n in names],
                          "sources": [{"title": "t", "url": "https://example.com/x",
                                       "thumb": f"media/{names[0]}"} if names else {}]}}
    }), encoding="utf-8")
    conn.execute(
        "INSERT OR REPLACE INTO editions (reader_id, date, max_privacy, path, built_at) "
        "VALUES ('r1', ?, 'private', ?, ?)", (day, str(snap), built_at))
    conn.commit()
    return snap


NOW = int(time.time())
OLD = NOW - 40 * 86400  # beyond every window used below


def _prune_fixture(tmp_path):
    conn = make_db(tmp_path)
    # an edition built inside the window references aaaa…; one built 40 days
    # ago (outside it) references dddd…
    _write_edition(conn, tmp_path, ["aaaaaaaaaaaaaaaa.png"], NOW - 3600, day="2026-11-01")
    _write_edition(conn, tmp_path, ["dddddddddddddddd.webp"], OLD - 3600, day="2026-10-01")
    files = {
        "kept-referenced-old": _media_file(tmp_path, "aaaaaaaaaaaaaaaa.png", 100, OLD),
        "gone-unreferenced-old": _media_file(tmp_path, "bbbbbbbbbbbbbbbb.jpg", 150_000, OLD),
        "kept-young": _media_file(tmp_path, "cccccccccccccccc.gif", 300, NOW - 100),
        "gone-only-old-edition": _media_file(tmp_path, "dddddddddddddddd.webp", 250_000, OLD),
        "not-a-cache-name": _media_file(tmp_path, "notes.txt", 50, OLD),
    }
    conn.execute(
        "INSERT INTO media (item_id, kind, status, local_path, fetched_at) "
        "VALUES ('yt:AAAAAAAAAAA', 'image', 'ok', 'bbbbbbbbbbbbbbbb.jpg', ?)", (OLD,))
    conn.commit()
    return conn, files


def test_prune_keeps_referenced_and_recent_deletes_old_unreferenced(tmp_path):
    conn, files = _prune_fixture(tmp_path)
    result = media_mod.prune_media(conn, tmp_path, days=30, now=NOW)
    assert result["deleted"] == 2 and result["bytes_freed"] == 400_000
    assert result["considered"] == 4 and result["referenced"] == 1
    assert files["kept-referenced-old"].exists()      # an edition in the window needs it
    assert files["kept-young"].exists()               # younger than the window by mtime
    assert not files["gone-unreferenced-old"].exists()
    assert not files["gone-only-old-edition"].exists()  # its edition is outside the window
    assert files["not-a-cache-name"].exists()         # not gaceta's naming: untouched
    # the deleted file's row is gone; nothing else lost a row
    assert conn.execute(
        "SELECT COUNT(*) FROM media WHERE local_path = 'bbbbbbbbbbbbbbbb.jpg'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


def test_prune_dry_run_deletes_nothing(tmp_path):
    conn, files = _prune_fixture(tmp_path)
    result = media_mod.prune_media(conn, tmp_path, days=30, dry_run=True, now=NOW)
    assert result["dry_run"] is True and result["deleted"] == 2
    assert all(p.exists() for p in files.values())
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1


def test_prune_refuses_when_a_window_edition_is_unreadable(tmp_path):
    conn, files = _prune_fixture(tmp_path)
    (tmp_path / "editions" / "r1" / "2026-11-01-private.json").unlink()
    with pytest.raises(media_mod.MediaError, match="refuses to guess"):
        media_mod.prune_media(conn, tmp_path, days=30, now=NOW)
    assert all(p.exists() for p in files.values())


def test_prune_cli_and_legacy_media_invocation(tmp_path, monkeypatch, capsys):
    import gaceta.db as gdb2
    from gaceta.cli import main as cli_main

    monkeypatch.setenv("GACETA_DATA", str(tmp_path))
    conn, files = _prune_fixture(tmp_path)
    conn.close()

    assert cli_main(["media", "prune", "--days", "30", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "media prune days=30" in out and "dry-run, nothing deleted" in out
    assert all(p.exists() for p in files.values())

    assert cli_main(["media", "prune", "--days", "30"]) == 0
    out = capsys.readouterr().out
    assert "2 files" in out and "0.4 MB freed" in out
    assert not files["gone-unreferenced-old"].exists()

    # the pre-prune spelling still works: gaceta media --reader r (fetch, no network here)
    conn2 = gdb2.connect(tmp_path / "gaceta.db")
    conn2.execute("UPDATE stories SET last_seen = ? WHERE reader_id = 'r1'", (NOW - 400,))
    conn2.commit()
    conn2.close()
    monkeypatch.setattr(media_mod, "_read_url", fake_fetch)
    assert cli_main(["media", "--reader", "r1", "--max-fetch", "0"]) == 0
    assert "media reader=r1" in capsys.readouterr().out
    # and the subcommand spelling parses the same options
    assert cli_main(["media", "fetch", "--reader", "r1", "--max-fetch", "0"]) == 0
    capsys.readouterr()
    # a bare `gaceta media` says what it needs instead of crashing
    assert cli_main(["media"]) == 2
    assert "--reader" in capsys.readouterr().err
