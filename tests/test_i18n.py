"""Interface languages: catalogs, rendering, served strings, write-up language."""
from __future__ import annotations

import json
import re
from datetime import date
from importlib import resources

import pytest

from gaceta import i18n
from gaceta.render import html as render_html
from gaceta.serve import inject_react

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _cat(lang: str) -> dict:
    return json.loads((resources.files("gaceta") / "locales" / f"{lang}.json").read_text("utf-8"))


def test_shipped_languages():
    assert {"en", "es", "pt", "fr", "de", "it"} <= set(i18n.supported())


@pytest.mark.parametrize("lang", i18n.supported())
def test_catalog_matches_english(lang):
    en, cat = _cat("en"), _cat(lang)
    for sec in ("strings", "js"):
        assert set(cat[sec]) == set(en[sec]), f"{lang}.{sec} keys differ from en"
        for k, v in en[sec].items():
            assert sorted(PLACEHOLDER.findall(v)) == sorted(PLACEHOLDER.findall(cat[sec][k])), \
                f"{lang}.{sec}.{k} placeholders"
    for k, n in (("weekdays", 7), ("weekdays_short", 7), ("months", 12), ("months_short", 12)):
        assert len(cat["dates"][k]) == n
    day = date(2026, 11, 18)
    for fn in (i18n.long_date, i18n.short_date, i18n.day_month_year):
        assert "18" in fn(lang, day)


def test_reader_lang_resolution():
    assert i18n.reader_lang({"langs": "es,en"}) == "es"
    assert i18n.reader_lang({"langs": "pt-BR"}) == "pt"
    assert i18n.reader_lang({"langs": "es", "ui_lang": "en"}) == "en"
    assert i18n.reader_lang({"langs": ["fr"]}) == "fr"
    assert i18n.reader_lang({"langs": "xx"}) == "en"  # unsupported → English
    assert i18n.reader_lang({}) == "en"


def test_plurals_and_fallback():
    assert i18n.t("es", "sources.more", n=4) == "4 fuentes más"
    assert i18n.t("en", "discovery.trial", 1) == "On trial · 1 entry"
    assert i18n.t("en", "discovery.trial", 3) == "On trial · 3 entries"
    assert i18n.t("fr", "discovery.trial", 0).endswith(i18n.t("fr", "discovery.trial_one", n=0)[-6:])
    assert i18n.t("es", "no.such.key") == "no.such.key"


def test_dates():
    day = date(2026, 11, 18)
    assert i18n.long_date("es", day) == "miércoles 18 de noviembre de 2026"
    assert i18n.short_date("es", day) == "miércoles 18 nov"
    assert i18n.long_date("en", day) == "Wednesday, November 18, 2026"


def _edition(langs):
    return {
        "reader": {"id": "r", "name": "Ana", "langs": langs},
        "date": "2026-11-18", "built_at": "2026-11-18T06:30:00-05:00",
        "portada": ["s1"],
        "stories": {"s1": {"title": "Solar lamps", "summary": "They survived.",
                           "why_it_matters": "Cheap light.", "status": "developing",
                           "privacy": "public",
                           "sources": [{"title": f"src {i}", "url": f"https://e.com/{i}",
                                        "published": "2026-11-1%d" % i} for i in range(5)]}},
        "sections": [], "folio": {"code": "ABC123", "stories_total": 1, "items_total": 5,
                                  "voices": 2, "portada": 1, "window_end": "06:30"},
    }


SPANISH_UI = ("Fuentes", "En desarrollo", "historias", "Para Ana",
              "fuentes más", "miércoles")


def test_render_english_has_no_spanish_interface():
    out = render_html.render(_edition(["en"]), "public")
    assert '<html lang="en">' in out
    for word in SPANISH_UI:
        assert word not in out, word
    for word in ("Sources", "Cheap light.", "Developing", "For Ana", "2 more sources",
                 "Wednesday, November 18, 2026", "Wed, Nov 18", "24 h to 06:30"):
        assert word in out, word


def test_render_spanish_default_house_style():
    out = render_html.render(_edition(["es", "en"]), "public")
    assert '<html lang="es">' in out
    for word in SPANISH_UI:
        assert word in out, word


@pytest.mark.parametrize("lang", ["pt", "fr", "de", "it"])
def test_render_other_languages(lang):
    out = render_html.render(_edition([lang]), "public")
    assert f'<html lang="{lang}">' in out
    assert i18n.t(lang, "sources") in out
    assert i18n.long_date(lang, date(2026, 11, 18)) in out
    assert "Por qué te importa" not in out


def test_masthead_override():
    ed = _edition(["en"])
    ed["reader"]["masthead"] = "The Daily Ana"
    out = render_html.render(ed, "public")
    assert "<h1>The Daily Ana</h1>" in out and "<title>The Daily Ana — " in out


def test_guardadas_languages():
    assert '<html lang="en">' in render_html.render_guardadas("r", "", [], lang="en")
    assert "Nothing saved yet" in render_html.render_guardadas("r", "Ana", [], lang="en")
    assert "Aún no has guardado" in render_html.render_guardadas("r", "Ana", [])


def test_served_page_gets_strings_in_its_language():
    page = inject_react('<!DOCTYPE html>\n<html lang="de"><head></head><body>x</body></html>')
    m = re.search(r"window\.GACETA_L10N=(\{.*?\});</script>", page)
    assert m and page.index("GACETA_L10N") < page.index("/static/react.js")
    strings = json.loads(m.group(1))
    assert strings["_lang"] == "de" and strings["save"] == i18n.js_catalog("de")["save"]
    assert inject_react(page) == page  # idempotent


def test_js_script_cannot_break_out_of_script_tag(monkeypatch):
    monkeypatch.setattr(i18n, "js_catalog", lambda lang: {"x": "</script><b>"})
    assert "</script><b>" not in i18n.js_script("en")[:-9]


def test_every_js_key_used_exists():
    js = (resources.files("gaceta") / "static" / "react.js").read_text("utf-8")
    used = set(re.findall(r'tr\("([\w.]+)"', js)) - {"key"}
    cat = i18n.js_catalog("en")
    for k in used:
        assert k in cat or f"{k}_one" in cat, k
    code = [ln for ln in js.splitlines() if not ln.lstrip().startswith(("/*", "*", "//"))]
    left = [ln for ln in code if re.search(r'"[^"\n]*[áéíóúñ¿¡][^"\n]*"', ln)]
    assert not left, f"Spanish literal left in react.js: {left[:3]}"


# ------------------------------------------------------------- write-up language

ES = "La empresa anunció que los paneles solares de los balcones resistieron el invierno sin fallas graves y que publicará una guía para repararlos."
EN = "The company said the balcony solar panels survived the winter without serious failures and that it will publish a guide to repair them."


def test_detect_is_conservative():
    assert i18n.detect(ES) == "es"
    assert i18n.detect(EN) == "en"
    assert i18n.detect("Apple M5") is None  # too short to judge
    assert i18n.mismatch(EN, "es") == "en"
    assert i18n.mismatch(ES, "es") is None
    assert i18n.mismatch(EN, "ja") is None  # language it can't judge → never flags
