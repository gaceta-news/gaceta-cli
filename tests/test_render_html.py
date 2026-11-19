"""Rendering an edition snapshot to one self-contained HTML document."""
from __future__ import annotations

import copy
import html
import json
from pathlib import Path

import pytest

from gaceta.render.html import render

FIXTURE = Path(__file__).parent / "fixtures" / "edition_sample.json"


def load_edition() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def without_private_stories(edition: dict) -> dict:
    clean = copy.deepcopy(edition)
    private_ids = {
        story_id
        for story_id, story in clean["stories"].items()
        if story.get("privacy") == "private"
    }
    for story_id in private_ids:
        del clean["stories"][story_id]
    clean["portada"] = [sid for sid in clean["portada"] if sid not in private_ids]
    for section in clean["sections"]:
        section["stories"] = [sid for sid in section["stories"] if sid not in private_ids]
    return clean


def test_renders_full_edition_at_private() -> None:
    edition = load_edition()
    out = render(edition, "private")

    assert out.startswith("<!DOCTYPE html>")
    assert '<html lang="es">' in out
    assert out.rstrip().endswith("</html>")
    assert "La Gaceta" in out
    assert "Ana" in out
    assert "martes 17 de noviembre de 2026" in out

    for story_id in edition["stories"]:
        assert f'id="{story_id}"' in out

    for section in edition["sections"]:
        assert section["title"] in out

    assert "En desarrollo" in out
    assert "Corregido" in out
    assert "Por qué te importa" in out
    assert "Antes dijimos…" in out
    assert "Ahora sabemos…" in out


def test_refuses_edition_above_max_level() -> None:
    edition = load_edition()

    with pytest.raises(ValueError):
        render(edition, "public")


def test_public_render_escapes_malicious_text_and_skips_private_story() -> None:
    edition = without_private_stories(load_edition())
    out = render(edition, "public")

    assert "La receta de la abuela" not in out
    assert "Es un recuerdo familiar" not in out

    assert "<script>alert" not in out
    assert 'href="javascript:' not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "Consulta ciudadana" in out


def test_every_http_source_url_is_a_link() -> None:
    edition = without_private_stories(load_edition())
    out = render(edition, "public")

    for story in edition["stories"].values():
        for source in story["sources"]:
            url = source["url"]
            if url.startswith(("http://", "https://")):
                assert f'href="{html.escape(url, quote=True)}"' in out

    assert 'rel="noopener noreferrer"' in out
    assert "martes 17 de noviembre de 2026" in out


def test_long_source_lists_fold():
    from gaceta.render.html import render, SOURCES_SHOWN
    srcs = [{"title": f"s{i}", "url": f"https://example.com/{i}", "author": "a", "kind": "rss",
             "published": f"2026-11-{i+1:02d}", "role": "update"} for i in range(9)]
    ed = {"version": 1, "reader": {"id": "r", "name": "R", "langs": ["es"]}, "date": "2026-11-17",
          "max_privacy": "public", "portada": ["x"], "sections": [],
          "stories": {"x": {"title": "T", "summary": "S", "privacy": "public", "status": "developing", "sources": srcs}}}
    out = render(ed, "public")
    assert f"{9 - SOURCES_SHOWN} fuentes más" in out
    foot = out[out.index('<footer class="sources">'):]
    assert foot.index("example.com/8") < foot.index("fuentes más") < foot.index("example.com/0")


def test_data_rev_data_section_and_updated_line():
    from gaceta.render.html import render
    story = {"title": "T", "summary": "S", "privacy": "public", "status": "settled",
             "sources": [], "rev": "abc123def456"}
    ed = {"version": 1, "reader": {"id": "r", "name": "R", "langs": ["es"]},
          "date": "2026-11-17", "built_at": "2026-11-17T12:00:00-05:00",
          "next_update": "2026-11-17T14:30:00-05:00", "max_privacy": "public",
          "portada": ["x"], "sections": [{"id": "sec1", "title": "S1", "stories": ["y"]}],
          "stories": {"x": dict(story), "y": dict(story)}}
    out = render(ed, "public")
    assert out.count('data-rev="abc123def456"') == 2
    assert 'data-section="sec1"' in out
    assert 'data-section="portada"' in out
    assert "Actualizada 12:00" in out
    assert "próxima ~14:30" in out

    del ed["next_update"]
    out = render(ed, "public")
    assert "Actualizada 12:00" in out
    assert "próxima" not in out

    del ed["built_at"]
    out = render(ed, "public")
    assert "Actualizada" not in out
