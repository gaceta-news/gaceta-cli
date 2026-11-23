"""Reader-facing explanations are prose, not ranking debug output."""
import copy
import json
import re
from pathlib import Path

import pytest

from gaceta import i18n
from gaceta.edition.build import _score_and_reasons
from gaceta.editor.prompts import writeup_messages
from gaceta.render.html import render
from test_render_html import load_edition

ROOT = Path(__file__).parents[1]
LANGS = ["es", "en", "fr", "de", "it", "pt"]


def test_writeup_demands_specific_stake_and_bans_defensive_contrast():
    system = writeup_messages([], langs="en", profile=None, allow_private=False)[0]["content"]
    for rule in ("return null for most stories", "concrete, specific stake", "General interest",
                 "Ban defensive contrast", "es X, no un Y", "X, not Y", "state the evidence",
                 "Never speculate about the reader’s infrastructure"):
        assert rule in system


@pytest.mark.parametrize("lang", LANGS)
def test_reason_catalogs_and_base_scoring_do_not_expose_score_arithmetic(lang):
    catalog = json.loads((ROOT / f"src/gaceta/locales/{lang}.json").read_text())["strings"]
    for key, text in catalog.items():
        if key.startswith(("reason.", "taste.")):
            assert "{v}" not in text, key
            assert not re.search(r"[+−-]\d+\.\d", text), key
    score, reasons = _score_and_reasons(
        {"n_items": 4, "n_told": 1, "last_seen": 100, "summary": "A summary"},
        100, sites=3, lang=lang,
    )
    assert score > 0 and reasons
    assert not re.search(r"[+−-]\d+\.\d", " ".join(reasons))
    for key in ("taste.author_pos", "taste.author_neg"):
        assert "Pat Writer" in i18n.t(lang, key, author="Pat Writer")


def test_stake_is_an_escaped_plain_paragraph_and_null_renders_nothing():
    edition = load_edition()
    for story in edition["stories"].values():
        story["why_it_matters"] = None
    assert 'class="summary stake"' not in render(edition, "private")
    story = next(iter(edition["stories"].values()))
    story["why_it_matters"] = "Your deadline is Friday <script>"
    doc = render(edition, "private")
    assert '<p class="summary stake">Your deadline is Friday &lt;script&gt;</p>' in doc
    assert 'class="why"' not in doc and "why-label" not in doc
    assert "Por qué te importa" not in doc


def test_floating_bars_are_solid_with_hairline_borders():
    script = (ROOT / "src/gaceta/static/react.js").read_text()
    assert "backdrop-filter" not in script
    for name in ("bybar", "topbar"):
        css = script.split(f'".gaceta-{name}' + '{', 1)[1].split('}', 1)[0]
        assert "background:var(--bg,#fff)" in css
        assert "border:1px solid var(--rule,#ccc)" in css
        assert "color-mix" not in css


def test_cached_folio_hash_is_replaced_and_masthead_has_no_dot_chains():
    edition = copy.deepcopy(load_edition())
    edition["folio"] = {"code": "ABC123", "items_total": 99, "voices": 45}
    edition["next_update"] = "2026-11-17T14:30:00-05:00"
    doc = render(edition, "private")
    assert "No. 20261117" in doc and "ABC123" not in doc
    masthead = doc.split('<header class="masthead">', 1)[1].split('</header>', 1)[0]
    assert " · " not in masthead
    assert "ítems" not in masthead and "voces" not in masthead
