"""Roadmap 11c: folio/dateline furniture + instrument taste transparency.

folio.code is the edition date (YYYYMMDD); date_label is Spanish with weekday and
no locale dependency; window_end is the local HH:MM of built_at. instrument
counts portada stories that are only there because of the owner's taste
adjustments. Renderers must tolerate editions without either block, escape
everything, and never show the instrument on public surfaces (privacy canaries
in test_privacy.py cover the rest)."""
from __future__ import annotations

import re
import time
from datetime import date as date_cls
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gaceta.edition import build_edition
from gaceta.editor import cluster as run_cluster
from gaceta.editor.cluster import embed_items
from gaceta.print import print_html
from gaceta.providers.embed import FakeEmbedder
from gaceta.render import html as render_html

from test_cluster import T0, DAY, add_item
from test_edition_build import _edition_date, story_of_item

# independent Spanish tables (a wrong name in build.py must fail here)
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")


def _fixture(env):
    """Six single-item recent stories (the base portada) + one older, weaker
    story that only reaches the portada after an owner 'more' reaction."""
    for n in range(6):
        add_item(env, f"hi{n}", f"Hi {n}", f"cuerpo alto numero {n}", "noted", "public",
                 T0 + 5 * DAY)
    add_item(env, "lo", "Lo one", "cuerpo bajo distinto", "noted", "public", T0)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    run_cluster.cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)


def _build(env, **kw):
    return build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                         max_privacy="private", force=True, data_dir=env.data_dir, **kw)


def _react_more(env, story_id):
    env.conn.execute(
        "INSERT INTO reactions (reader_id, story_id, kind, at, by_name) "
        "VALUES (?, ?, 'more', ?, NULL)",
        (env.cfg["id"], story_id, int(time.time())),
    )
    env.conn.commit()


# ---------------------------------------------------------------- folio block

def test_folio_code_is_date_based_and_stable_across_rebuilds(env):
    _fixture(env)
    kw = dict(date_str=_edition_date(), max_privacy="private",
              data_dir=env.data_dir, force=True)
    ed1 = build_edition(env.conn, env.cfg["id"], env.cfg, **kw)
    ed2 = build_edition(env.conn, env.cfg["id"], env.cfg, **kw)
    assert re.fullmatch(r"\d{8}", ed1["folio"]["code"])
    assert ed1["folio"]["code"] == ed1["date"].replace("-", "")
    assert ed2["folio"]["code"] == ed1["folio"]["code"]
    # More items on the same edition day do not invent a new issue number.
    add_item(env, "hi0b", "Hi zero bis", "cuerpo alto numero 0", "noted", "public",
             T0 + 5 * DAY)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    run_cluster.cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)
    ed3 = build_edition(env.conn, env.cfg["id"], env.cfg, **kw)
    assert ed3["folio"]["code"] == ed1["folio"]["code"]


def test_folio_counts_date_label_and_window_end(env, monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("TZ", "America/Lima")
        time.tzset()
        try:
            _fixture(env)
            now = T0 + 6 * DAY - 3600  # inside the edition day, fixed
            ed = _build(env, now=now)
            folio = ed["folio"]
            assert folio["stories_total"] == len(ed["stories"])
            assert folio["portada"] == len(ed["portada"]) == 6
            expected_items = len({r[0] for r in env.conn.execute(
                "SELECT DISTINCT si.item_id FROM story_items si "
                "JOIN stories s ON s.id = si.story_id WHERE s.reader_id = ?",
                (env.cfg["id"],))})
            assert folio["items_total"] == expected_items
            assert folio["voices"] == 0  # fixture items carry no author
            d = date_cls.fromisoformat(ed["date"])
            assert folio["date_label"] == (
                f"{WEEKDAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]} de {d.year}")
            assert folio["window_end"] == datetime.fromtimestamp(
                now, ZoneInfo("America/Lima")).strftime("%H:%M")
        finally:
            m.undo()
            time.tzset()


def test_folio_voices_count_distinct_authors(env):
    _fixture(env)
    env.conn.executemany(
        "UPDATE items SET author = ? WHERE id = ?",
        [("Autor Uno", "hi0"), ("Autor Dos", "hi1"), ("Autor Uno", "lo")],
    )
    env.conn.commit()
    ed = _build(env)
    assert ed["folio"]["voices"] == 2


# ------------------------------------------------------------- instrument k

def test_instrument_zero_without_taste_and_counts_a_promoted_story(env):
    _fixture(env)
    ed = _build(env)
    lo = story_of_item(env.conn, "lo")
    assert lo not in ed["portada"]  # the weak story misses the base portada
    assert ed["instrument"] == {"portada_by_taste": 0, "portada": 6}

    _react_more(env, lo)  # owner's 'Más así' (legacy NULL by_name counts)
    ed2 = _build(env)
    assert lo in ed2["portada"]  # +2.0 lifts it past the single-item leaders
    assert ed2["instrument"]["portada_by_taste"] == 1
    assert ed2["instrument"]["portada"] == 6


# ------------------------------------------------------------------ rendering

def _rendered_private_with_instrument(env):
    _fixture(env)
    lo = story_of_item(env.conn, "lo")
    _react_more(env, lo)
    return _build(env)


def test_render_shows_folio_dateline_rows_and_instrument_when_private(env):
    ed = _rendered_private_with_instrument(env)
    folio = ed["folio"]
    doc = render_html.render(ed, "private")
    assert f'No. {folio["code"]}' in doc
    assert f"24 h a las {folio['window_end']}" in doc
    assert folio["date_label"] in doc
    match = re.search(r'<p class="dateline-row">(.*?)</p>', doc)
    assert match
    row = match.group(1)
    assert f'{folio["portada"]} de {folio["stories_total"]} historias' in row
    assert re.findall(r"\d+", row) == [str(folio["portada"]), str(folio["stories_total"])]
    assert "ítems" not in row and "voces" not in row
    assert " · " not in row
    assert "Para Test Reader" in doc
    # folio row sits above the masthead, dateline row below it
    assert (doc.index('class="folio-row"') < doc.index("<h1>La Gaceta</h1>")
            < doc.index('class="dateline-row"'))
    # instrument: one small line near the portada, above the first story
    assert "1 de 6 en portada por tus reacciones" in doc
    assert doc.index('class="instrument"') < doc.index('data-section="portada"')
    # no prices, no headline teasers in the furniture rows
    folio_html = doc[doc.index('class="folio-row"'):doc.index("<h1>")]
    assert "<h" not in folio_html and "€" not in folio_html and "$" not in folio_html
    # household page keeps it too
    assert "por tus reacciones" in render_html.render(ed, "household")


def test_public_render_and_print_show_furniture_but_never_the_instrument(env):
    ed = _rendered_private_with_instrument(env)
    doc = render_html.render(ed, "public")
    assert f'No. {ed["folio"]["code"]}' in doc
    assert "historias" in doc and "voces" not in doc
    assert "por tus reacciones" not in doc
    printed = print_html(ed, "public")
    assert f'No. {ed["folio"]["code"]}' in printed
    assert "24 h a las" in printed
    assert "por tus reacciones" not in printed


def test_renderers_tolerate_missing_blocks_and_fall_back(env):
    ed = _rendered_private_with_instrument(env)
    del ed["folio"]
    del ed["instrument"]
    doc = render_html.render(ed, "private")
    assert "No. " not in doc
    assert "por tus reacciones" not in doc
    assert "24 h a las" not in doc
    # legacy furniture still there: reader, Spanish date from edition["date"]
    d = date_cls.fromisoformat(ed["date"])
    label = f"{WEEKDAYS[d.weekday()]} {d.day} de {MONTHS[d.month - 1]} de {d.year}"
    assert label in doc and "Para Test Reader" in doc
    printed = print_html(ed, "private")
    assert "No. " not in printed


def test_furniture_escapes_hostile_values(env):
    ed = _rendered_private_with_instrument(env)
    ed["folio"]["code"] = '<svg onload="alert(1)">'
    ed["folio"]["date_label"] = 'martes <script>alert("x")</script>'
    ed["folio"]["window_end"] = '14:30" onmouseover="alert(2)'
    ed["reader"]["name"] = "Alice <b>negrita</b>"
    for doc in (render_html.render(ed, "private"), print_html(ed, "private")):
        assert "<svg" not in doc and "<script>alert" not in doc
        assert "onload=" not in doc
        assert "&lt;script&gt;" in doc
        # the quote is escaped to &quot;, so no attribute breakout is possible
        assert 'onmouseover="' not in doc
        assert "14:30&quot; onmouseover=" in doc
        assert "Alice &lt;b&gt;negrita&lt;/b&gt;" in doc


def test_instrument_line_absent_when_k_zero_or_missing(env):
    _fixture(env)
    ed = _build(env)  # no reactions → k = 0
    assert 'class="instrument"' not in render_html.render(ed, "private")
    ed["instrument"] = {"portada_by_taste": 0, "portada": 6}
    assert 'class="instrument"' not in render_html.render(ed, "private")
    del ed["instrument"]
    assert 'class="instrument"' not in render_html.render(ed, "private")
