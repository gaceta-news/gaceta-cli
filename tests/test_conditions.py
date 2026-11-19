"""Condiciones boxes: provider plugins captured at build time, rendered as a
stamped snapshot after the portada, privacy-filtered (fail closed), escaped,
and never able to block an edition."""
from __future__ import annotations

from datetime import datetime

import pytest

from gaceta import conditions
from gaceta.edition import build_edition
from gaceta.render import html as render_html

from test_edition_build import _edition_date
from test_folio import _fixture


class Good:
    def __init__(self, table):
        self.table = table

    def box(self, now):
        return {"title": self.table.get("title", "El tiempo"),
                "lead": "10 °C · niebla <b>",
                "facts": [["Lluvia 24 h", "0 mm"], ["bad row"], ["", "x"]],
                "source": "estación propia",
                "observed_at": "2026-11-19T06:25:00-05:00"}


class Boom:
    def __init__(self, table):
        pass

    def box(self, now):
        raise RuntimeError("station offline")


class Junk:
    def __init__(self, table):
        pass

    def box(self, now):
        return {"title": "only a title"}


@pytest.fixture
def fake_providers(monkeypatch):
    monkeypatch.setattr(conditions, "providers", lambda: {
        "good": f"{__name__}:Good", "boom": f"{__name__}:Boom", "junk": f"{__name__}:Junk"})


def test_no_config_no_boxes():
    assert conditions.collect({}, max_privacy="private") == []


def test_collect_validates_and_skips_failures(fake_providers, capsys):
    cfg = {"conditions": {"boom": {}, "good": {"privacy": "household"},
                          "junk": {}, "missing": {}}}
    boxes = conditions.collect(cfg, max_privacy="private")
    assert [b["id"] for b in boxes] == ["good"]
    box = boxes[0]
    assert box["privacy"] == "household"
    assert box["facts"] == [["Lluvia 24 h", "0 mm"]]
    err = capsys.readouterr().err
    assert "station offline" in err and "missing" in err


def test_privacy_default_private_and_filtered(fake_providers):
    cfg = {"conditions": {"good": {}}}
    assert conditions.collect(cfg, max_privacy="household") == []
    assert conditions.collect(cfg, max_privacy="private")[0]["privacy"] == "private"
    cfg = {"conditions": {"good": {"privacy": "nonsense"}}}
    assert conditions.collect(cfg, max_privacy="household") == []


def test_provider_alias(fake_providers):
    cfg = {"conditions": {"casa": {"provider": "good", "title": "Casa"}}}
    boxes = conditions.collect(cfg, max_privacy="private")
    assert boxes[0]["id"] == "casa" and boxes[0]["title"] == "Casa"


def test_edition_carries_boxes_and_render_stamps_them(env, fake_providers):
    _fixture(env)
    env.cfg["conditions"] = {"good": {"privacy": "household"}}
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                       max_privacy="private", force=True, data_dir=env.data_dir)
    assert ed["conditions"][0]["id"] == "good"
    doc = render_html.render(ed, "private")
    assert '<aside class="conditions"' in doc
    assert "a las 06:25, no en vivo · estación propia" in doc
    assert "niebla &lt;b&gt;" in doc and "niebla <b>" not in doc
    # after the portada, before the first section
    assert doc.index('class="portada"') < doc.index('class="conditions"') < doc.index('class="section"')


def test_render_drops_boxes_above_max_level():
    ed = {"date": "2026-11-19", "built_at": "2026-11-19T06:30:00-05:00", "stories": {},
          "reader": {"id": "r", "langs": "es"},
          "conditions": [{"id": "w", "title": "T", "lead": "x", "privacy": "household"},
                         {"id": "p", "title": "P", "lead": "y"}]}
    assert '<aside class="conditions"' not in render_html.render(ed, "public")
    house = render_html.render(ed, "household")
    assert "<h2>T</h2>" in house and "<h2>P</h2>" not in house
    # stamp falls back to build time, in the page language
    assert "a las 06:30, no en vivo" in house
    assert 'aria-label="Condiciones"' in house
    ed["reader"]["langs"] = "en"
    assert "at 06:30, not live" in render_html.render(ed, "household")


def test_public_build_never_calls_private_provider(env, monkeypatch):
    called = []

    class Spy:
        def __init__(self, table):
            called.append(table)

        def box(self, now):
            return {"title": "x", "lead": "y"}

    monkeypatch.setattr(conditions, "_load", lambda spec: Spy)
    monkeypatch.setattr(conditions, "providers", lambda: {"spy": "x:y"})
    conditions.collect({"conditions": {"spy": {}}}, max_privacy="public",
                       now=datetime(2026, 11, 19, 6, 30).astimezone())
    assert called == []
