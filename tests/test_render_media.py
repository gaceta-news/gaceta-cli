"""Rendering editions WITH media: validated srcs, YouTube lite embed, thumbs,
linked sources. Editions without media keep rendering (sample fixture covers it)."""
from __future__ import annotations

import re

from gaceta.render.html import render

SRC_RE = re.compile(r'src="([^"]+)"')
HREF_RE = re.compile(r'href="([^"]+)"')


def edition_with_media() -> dict:
    def story(title: str, media=None, sources=None, thumb=None):
        src = sources or [{
            "title": "Origen", "url": "https://origin.example.com/a",
            "kind": "rss", "published": "2026-11-16", "role": "origin",
        }]
        if thumb:
            src = [dict(s, thumb=thumb) for s in src]
        entry = {
            "title": title, "summary": "resumen", "why_it_matters": None,
            "status": "settled", "confidence": 0.8,
            "first_seen": "2026-11-10T00:00:00+00:00", "last_seen": "2026-11-16T00:00:00+00:00",
            "privacy": "public", "sources": src, "corrections": [],
        }
        if media is not None:
            entry["media"] = media
        return entry

    return {
        "version": 1,
        "reader": {"id": "r1", "name": "R", "langs": ["es"]},
        "date": "2026-11-17",
        "built_at": "2026-11-17T12:00:00-05:00",
        "max_privacy": "public",
        "portada": ["lead-yt", "card-img"],
        "sections": [{"id": "otros", "title": "Otros", "stories": ["row-story"]}],
        "stories": {
            "lead-yt": story("Portada YouTube", media=[
                {"type": "youtube", "src": "media/abc123.jpg",
                 "video_id": "TWoS5Nmyoc8", "alt": "video", "item_id": "yt:1"},
                {"type": "video", "src": "https://v.nostr.build/clip.mp4",
                 "alt": "clip", "item_id": "nostr:1"},
            ]),
            "card-img": story("Tarjeta con imagen", media=[
                {"type": "image", "src": "media/def456.png",
                 "alt": "foto", "item_id": "feed:2"},
            ], thumb="media/def456.png"),
            "row-story": story("Historia en sección", media=[
                {"type": "image", "src": "media/ghi789.webp",
                 "alt": "otra", "item_id": "feed:3"},
            ]),
        },
    }


def all_srcs(doc: str) -> list[str]:
    return SRC_RE.findall(doc) + [s for s in HREF_RE.findall(doc)]


def test_media_render_valid_and_safe():
    doc = render(edition_with_media(), "public")
    for src in all_srcs(doc):
        if src == "guardadas":  # in-site nav link to the archive page
            continue
        assert src.startswith(("../media/", "https://"))
        assert not src.startswith(("javascript:", "data:", "http://"))
    assert "javascript:" not in doc
    # lead youtube lite embed: thumbnail + validated button, no iframe until click
    assert 'data-yt="TWoS5Nmyoc8"' in doc
    assert 'src="../media/abc123.jpg"' in doc
    assert "<iframe" not in doc
    # the click-to-play loader is the only script
    assert doc.count("<script>") == 1
    assert "youtube-nocookie.com/embed/" in doc  # inside the JS only
    # nostr video renders as <video>, remote https, not downloaded
    assert '<video controls preload="none" src="https://v.nostr.build/clip.mp4"></video>' in doc
    # headline links to the story's origin source; visible Fuente line
    assert '<h2 class="headline"><a href="https://origin.example.com/a"' in doc
    assert "Fuente: <a href=" in doc
    assert ">origin.example.com</a>" in doc
    # source list shows a small thumb
    assert 'class="source-thumb"' in doc
    # lazy images with async decoding
    assert 'loading="lazy"' in doc and 'decoding="async"' in doc


def test_invalid_media_never_emitted():
    edition = edition_with_media()
    bad = [
        {"type": "image", "src": "javascript:alert(1)", "alt": "x", "item_id": "i"},
        {"type": "image", "src": "media/../../etc/passwd", "alt": "x", "item_id": "i"},
        {"type": "image", "src": "http://plain.example.com/x.png", "alt": "x", "item_id": "i"},
        {"type": "youtube", "src": "media/ok.jpg", "video_id": "short", "alt": "x", "item_id": "i"},
    ]
    for st in edition["stories"].values():
        st["media"] = list(bad)
    doc = render(edition, "public")
    assert "javascript:" not in doc
    assert "../etc" not in doc
    assert "http://plain.example.com" not in doc
    assert "data-yt=" not in doc
    assert "<script>" not in doc  # no youtube survived → no JS
    for src in SRC_RE.findall(doc):
        assert src.startswith(("../media/", "https://"))


def test_media_prefix_parameter():
    doc = render(edition_with_media(), "public", media_prefix="/static/m/")
    assert 'src="/static/m/abc123.jpg"' in doc
    assert 'src="/static/m/def456.png"' in doc


def test_edition_without_media_still_renders():
    edition = edition_with_media()
    for story in edition["stories"].values():
        story.pop("media", None)
        for s in story["sources"]:
            s.pop("thumb", None)
    doc = render(edition, "public")
    assert "<img" not in doc
    assert "<script>" not in doc
    assert "Fuente:" in doc
    assert doc.rstrip().endswith("</html>")
