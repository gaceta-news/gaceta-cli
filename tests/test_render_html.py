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
    assert "Por qué te importa" in out  # quiet rubric above the stake paragraph
    assert 'class="summary stake"' in out
    assert 'class="why"' not in out and "why-label" not in out
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


def test_coverage_dates_follow_source_publication_not_edition_or_ingest():
    edition = load_edition()
    edition["built_at"] = "2026-11-17T09:07:00-05:00"
    edition["folio"] = {"code": "01", "window_end": "09:07"}
    sid = edition["portada"][0]
    story = edition["stories"][sid]
    story["sources"] = [
        {"url": "https://example.org/a", "published": "2026-11-15"},
        {"url": "https://example.org/b", "published": "2026-11-17T02:30:00+00:00"},
        {"url": "https://example.org/c", "added": 1794928000},
    ]
    story["last_seen"] = "2026-11-17"
    out = render(edition, "private")
    article = out.split(f'id="{sid}"', 1)[1].split("</article>", 1)[0]
    assert ('Última fuente: <time datetime="2026-11-16">16 de noviembre de 2026</time>'
            in article.split('<p class="summary">', 1)[0])
    assert '<time datetime="2026-11-17T09:07:00-05:00">Actualizada 09:07</time>' in out
    assert '<time datetime="2026-11-16">16 de noviembre de 2026</time>' in article.split('<footer', 1)[1]

    story["sources"] = [{"published": "2026-11-15"}]
    assert 'Publicado <time datetime="2026-11-15">' in render(edition, "private")
    story["sources"] = [{"published": "not a date", "added": 1794928000}]
    article = render(edition, "private").split(f'id="{sid}"', 1)[1].split("</article>", 1)[0]
    assert 'class="coverage-date"' not in article


def test_update_leads_with_change_and_only_uses_a_new_source_image():
    edition = load_edition()
    edition["built_at"] = "2026-11-17T12:00:00+00:00"
    from datetime import datetime
    built = int(datetime.fromisoformat(edition["built_at"]).timestamp())
    sid = edition["portada"][0]
    story = edition["stories"][sid]
    story["summary"] = "El resumen completo"
    story["history"] = [
        {"at": built - 100000, "summary": "Antes"},
        {"at": built - 90000, "what_changed": "Primera revisión"},
        {"at": built - 900, "what_changed": "El concejo aprobó la medida <por un voto>"},
    ]
    old = "media/vieja.jpg"
    new = "media/nueva.jpg"
    story["media"] = [{"type": "image", "src": old}]
    story["sources"] = [
        {"thumb": old, "added": built - 100000, "published": "2026-11-15"},
        {"thumb": old, "added": built - 100, "published": "2026-11-17"},
        {"thumb": new, "added": built - 1100, "published": "2026-11-16"},
        {"thumb": "media/futura.jpg", "added": built + 1, "published": "2026-11-18"},
    ]

    def article():
        return render(edition, "private").split(f'id="{sid}"', 1)[1].split("</article>", 1)[0]

    markup = article()
    assert 'class="story-context"><summary>Leer el contexto</summary>' in markup
    assert markup.index("Qué cambió") < markup.index("El resumen completo")
    assert markup.count("&lt;por un voto&gt;") == 1
    assert "Primera revisión" in markup.split('class="since-steps"', 1)[1]
    assert "Antes" in markup.split('class="since-before"', 1)[1]
    assert 'src="../media/nueva.jpg"' in markup.split('<header', 1)[0]
    story["sources"].pop(2)
    assert "vieja.jpg" not in article().split('<header', 1)[0]
    story["history"][-1]["what_changed"] = None
    assert 'class="story-update"' not in article()
    assert 'src="../media/vieja.jpg"' in article().split('<header', 1)[0]


# ------------------------------------------------------------- stale banner


def _bare_edition(built_at: str, lang: str = "es") -> dict:
    return {"version": 1,
            "reader": {"id": "r", "name": "R", "langs": [lang]},
            "date": built_at[:10], "built_at": built_at, "stale_after_h": 14,
            "max_privacy": "public", "portada": [], "sections": [], "stories": {}}


def _banner(edition: dict, now: str) -> str:
    from datetime import datetime
    doc = render(edition, "public", now=datetime.fromisoformat(now))
    start = doc.index('<p class="stale-banner"')
    return doc[start:doc.index("</p>", start) + 4]


def test_stale_banner_today_yesterday_older_es():
    # built today, older than the 14 h threshold → "Edición de las HH:MM"
    banner = _banner(_bare_edition("2026-11-22T08:00:00-05:00"), "2026-11-22T23:10:00-05:00")
    assert banner.startswith('<p class="stale-banner" role="status" '
                            'data-built="2026-11-22T08:00:00-05:00" data-stale-h="14">')
    assert " hidden" not in banner.split(">", 1)[0]
    assert banner.endswith("Edición de las 08:00</p>")
    # built yesterday → "Edición de ayer HH:MM"
    banner = _banner(_bare_edition("2026-11-21T22:00:00-05:00"), "2026-11-22T13:00:00-05:00")
    assert "Edición de ayer 22:00" in banner and " hidden" not in banner.split(">", 1)[0]
    # older still → "Edición del <fecha>"
    banner = _banner(_bare_edition("2026-11-15T09:30:00-05:00"), "2026-11-22T13:00:00-05:00")
    assert "Edición del 15 de noviembre de 2026" in banner


def test_stale_banner_english_strings():
    ed = _bare_edition("2026-11-22T08:00:00-05:00", lang="en")
    assert "Edition from 08:00 today" in _banner(ed, "2026-11-22T23:10:00-05:00")
    ed = _bare_edition("2026-11-21T22:00:00-05:00", lang="en")
    assert "Edition from 22:00 yesterday" in _banner(ed, "2026-11-22T13:00:00-05:00")
    ed = _bare_edition("2026-11-15T09:30:00-05:00", lang="en")
    assert "Edition from November 15, 2026" in _banner(ed, "2026-11-22T13:00:00-05:00")


def test_stale_banner_hidden_while_fresh_and_threshold_honored():
    from datetime import datetime

    ed = _bare_edition("2026-11-22T20:00:00-05:00")
    banner = _banner(ed, "2026-11-22T23:10:00-05:00")  # 3 h old < 14 h
    assert " hidden" in banner.split(">", 1)[0]          # in the DOM, hidden
    ed["stale_after_h"] = 2
    banner = _banner(ed, "2026-11-22T23:10:00-05:00")     # now older than 2 h
    assert 'data-stale-h="2"' in banner and " hidden" not in banner.split(">", 1)[0]
    # the element sits above the masthead; editions without built_at show none
    doc = render(ed, "public", now=datetime.fromisoformat("2026-11-22T23:10:00-05:00"))
    assert doc.index('<p class="stale-banner"') < doc.index('<header class="masthead">')
    del ed["built_at"]
    doc = render(ed, "public", now=datetime.fromisoformat("2026-11-22T23:10:00-05:00"))
    assert '<p class="stale-banner"' not in doc
