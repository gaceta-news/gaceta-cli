"""Condiciones boxes: provider plugins captured at build time, rendered as a
stamped snapshot after the portada, privacy-filtered (fail closed), escaped,
and never able to block an edition. A provider may offer live(now): the served
page refreshes those in place (LiveConditions, /api/conditions, live.js); a
static export keeps the snapshot, and providers without live() render exactly
as before."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

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


def test_icons_and_ear_render(monkeypatch):
    raw = {"title": "Tiempo", "lead": "10 °C", "icon": "fog", "ear": "10°",
           "ear_note": "niebla", "facts": [["Hoy", "22°", "rainy"], ["Viento", "calma"],
                                           ["Mañana", "x", "<script>"]]}
    box = conditions.clean_box(raw, name="w", label="household")
    assert box["facts"][0] == ["Hoy", "22°", "rainy"] and box["ear"] == "10°"
    ed = {"date": "2026-11-19", "built_at": "2026-11-19T06:30:00-05:00", "stories": {},
          "reader": {"id": "r", "langs": "es"}, "conditions": [box]}
    doc = render_html.render(ed, "household")
    assert '<a class="ear" href="#cond-w"' in doc and 'id="cond-w"' in doc
    assert doc.index('class="ear"') < doc.index("</header>")
    assert doc.count('<svg class="wx"') == 3  # ear + lead + Hoy; unknown icon name renders nothing
    assert "<script>" not in doc.split("<body>")[1].split("</header>")[0]
    # above the page's level: neither ear nor box
    pub = render_html.render(ed, "public")
    assert 'class="ear"' not in pub and '<svg class="wx"' not in pub


def test_every_icon_is_wellformed_svg():
    import xml.etree.ElementTree as ET
    from gaceta.render import icons
    for name in icons.ICONS:
        ET.fromstring(icons.svg(name))
    assert icons.svg("nope") == ""


# ------------------------------------------------------------------ live()

class LiveBox:
    """Live-capable provider with call counters."""
    constructed = 0
    live_calls = 0

    def __init__(self, table):
        self.table = table
        type(self).constructed += 1

    def box(self, now):
        return {"title": "El tiempo", "lead": "10 °C", "ear": "10°",
                "observed_at": "2026-11-19T06:25:00-05:00"}

    def live(self, now):
        type(self).live_calls += 1
        return {"title": "El tiempo", "lead": "12 °C", "ear": "12°",
                "observed_at": now.isoformat(), "source": "estación propia"}


class LiveOnce:
    """Live provider that succeeds once, then raises: the negative-cache path."""
    live_calls = 0

    def __init__(self, table):
        pass

    def box(self, now):
        return {"title": "x", "lead": "y"}

    def live(self, now):
        type(self).live_calls += 1
        if LiveOnce.live_calls == 1:
            return {"title": "Primera", "lead": "ok", "observed_at": now.isoformat()}
        raise RuntimeError("station offline")


class LiveEmpty:
    """Live provider that returns None: skipped and negative-cached, not an error."""
    live_calls = 0

    def __init__(self, table):
        pass

    def box(self, now):
        return {"title": "x", "lead": "y"}

    def live(self, now):
        type(self).live_calls += 1
        return None


def _t0() -> datetime:
    return datetime(2026, 11, 19, 12, 0).astimezone()


@pytest.fixture
def live_providers(monkeypatch):
    monkeypatch.setattr(conditions, "providers", lambda: {
        "good": f"{__name__}:Good", "live": f"{__name__}:LiveBox",
        "wx": f"{__name__}:LiveBox", "w2": f"{__name__}:LiveBox",
        "once": f"{__name__}:LiveOnce", "empty": f"{__name__}:LiveEmpty"})


def test_collect_marks_live_capability(live_providers):
    cfg = {"conditions": {"good": {}, "live": {}}}
    boxes = {b["id"]: b for b in conditions.collect(cfg, max_privacy="private")}
    assert boxes["live"].get("live") is True
    assert "live" not in boxes["good"]  # snapshot-only: no marker, ever


def test_snapshot_only_render_is_byte_identical(env, live_providers):
    """A provider without live() renders exactly the markup the renderer
    always produced — no data hooks, no changed stamp."""
    _fixture(env)
    env.cfg["conditions"] = {"good": {"privacy": "household"}}
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                       max_privacy="private", force=True, data_dir=env.data_dir)
    doc = render_html.render(ed, "private")
    golden = ('<div class="cond-box" id="cond-good">\n'
              '<h2>El tiempo</h2>\n'
              '<p class="cond-lead">10 °C · niebla &lt;b&gt;</p>\n'
              '<dl class="cond-facts">\n'
              '<div><dt>Lluvia 24 h</dt><dd>0 mm</dd></div>\n'
              '</dl>\n'
              '<p class="cond-stamp">a las 06:25, no en vivo · estación propia</p>\n'
              '</div>')
    assert ('<aside class="conditions" aria-label="Condiciones">\n'
            + golden + "\n</aside>") in doc
    assert "data-live" not in doc and "data-reader" not in doc


def test_live_boxes_ttl_cache(live_providers):
    LiveBox.constructed = LiveBox.live_calls = 0
    live = conditions.LiveConditions()
    cfg = {"conditions": {"live": {}}}
    t0 = _t0()
    boxes = live.live_boxes("r", cfg, max_privacy="private", now=t0)
    assert [b["id"] for b in boxes] == ["live"] and LiveBox.live_calls == 1
    # within the default 90 s TTL: the cached box, no new call, no rebuild
    again = live.live_boxes("r", cfg, max_privacy="private", now=t0 + timedelta(seconds=89))
    assert again[0]["lead"] == "12 °C"
    assert LiveBox.live_calls == 1 and LiveBox.constructed == 1
    # past the TTL: called again on the same instance
    live.live_boxes("r", cfg, max_privacy="private", now=t0 + timedelta(seconds=91))
    assert LiveBox.live_calls == 2 and LiveBox.constructed == 1


def test_live_ttl_from_table_and_minimum(live_providers):
    LiveBox.constructed = LiveBox.live_calls = 0
    live = conditions.LiveConditions()
    cfg = {"conditions": {"live": {"live_ttl": 30}}}
    t0 = _t0()
    live.live_boxes("r", cfg, max_privacy="private", now=t0)
    live.live_boxes("r", cfg, max_privacy="private", now=t0 + timedelta(seconds=29))
    assert LiveBox.live_calls == 1
    live.live_boxes("r", cfg, max_privacy="private", now=t0 + timedelta(seconds=31))
    assert LiveBox.live_calls == 2
    # below the 30 s minimum: clamped up, so 5 s never expires the cache
    live2 = conditions.LiveConditions()
    cfg = {"conditions": {"live": {"live_ttl": 5}}}
    live2.live_boxes("r", cfg, max_privacy="private", now=t0)
    live2.live_boxes("r", cfg, max_privacy="private", now=t0 + timedelta(seconds=4))
    assert LiveBox.live_calls == 3


def test_live_negative_cache_after_failure(live_providers, capsys):
    LiveOnce.live_calls = 0
    live = conditions.LiveConditions()
    cfg = {"conditions": {"once": {}}}
    t0 = _t0()
    assert [b["lead"] for b in live.live_boxes("r", cfg, max_privacy="private", now=t0)] == ["ok"]
    # the source breaks: the box drops out, and the failure is negative-cached
    assert live.live_boxes("r", cfg, max_privacy="private", now=t0 + timedelta(seconds=95)) == []
    assert live.live_boxes("r", cfg, max_privacy="private", now=t0 + timedelta(seconds=100)) == []
    assert LiveOnce.live_calls == 2  # one failure; the negative cache held after it
    assert "station offline" in capsys.readouterr().err
    # retried only after max(ttl, 60) = 90 s past the failure
    live.live_boxes("r", cfg, max_privacy="private", now=t0 + timedelta(seconds=186))
    assert LiveOnce.live_calls == 3


def test_live_negative_cache_after_none(live_providers):
    LiveEmpty.live_calls = 0
    live = conditions.LiveConditions()
    cfg = {"conditions": {"empty": {}}}
    t0 = _t0()
    assert live.live_boxes("r", cfg, max_privacy="private", now=t0) == []
    assert live.live_boxes("r", cfg, max_privacy="private", now=t0 + timedelta(seconds=30)) == []
    assert LiveEmpty.live_calls == 1


def test_live_privacy_filter_never_constructs(live_providers):
    LiveBox.constructed = LiveBox.live_calls = 0
    live = conditions.LiveConditions()
    cfg = {"conditions": {"live": {"privacy": "household"}}}
    t0 = _t0()
    assert live.live_boxes("r", cfg, max_privacy="public", now=t0) == []
    assert LiveBox.constructed == 0  # fail closed: never even built
    live.live_boxes("r", cfg, max_privacy="household", now=t0)
    live.live_boxes("r", cfg, max_privacy="household", now=t0)
    assert LiveBox.constructed == 1 and LiveBox.live_calls == 1


def test_live_boxes_skip_snapshot_only_and_one_instance_per_key(live_providers):
    LiveBox.constructed = 0
    live = conditions.LiveConditions()
    cfg = {"conditions": {"good": {}, "live": {}}}
    assert [b["id"] for b in live.live_boxes("r", cfg, max_privacy="private", now=_t0())] == ["live"]
    t0 = _t0()
    for _ in range(3):  # one instance per (reader, box name), process lifetime
        live.live_boxes("ana", cfg, max_privacy="private", now=t0)
        live.live_boxes("bob", cfg, max_privacy="private", now=t0)
    assert LiveBox.constructed == 3  # (r, live), (ana, live), (bob, live)


def test_live_boxes_thread_safety(live_providers):
    LiveBox.constructed = 0
    live = conditions.LiveConditions()
    cfg = {"conditions": {"wx": {}, "w2": {}}}
    errors: list[Exception] = []

    def hammer():
        try:
            for _ in range(25):
                boxes = live.live_boxes("r", cfg, max_privacy="private")
                assert {b["id"] for b in boxes} == {"wx", "w2"}
        except Exception as e:  # pragma: no cover — only on a race bug
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert LiveBox.constructed == 2  # both boxes, built exactly once each


def test_live_fragment_render_and_stale_stamp():
    box = conditions.clean_box(
        {"title": "El tiempo", "lead": "12 °C", "observed_at": "2026-11-19T12:00:00-05:00",
         "source": "estación propia", "ear": "12°"}, name="wx", label="public")
    assert box is not None
    frags = render_html.render_cond_fragments([box], now="2026-11-19T12:01:00-05:00", lang="es")
    assert len(frags) == 1
    frag = frags[0]
    assert frag["id"] == "wx" and frag["observed_at"] == "2026-11-19T12:00:00-05:00"
    assert frag["ear_html"].startswith('<a class="ear" id="cond-ear" data-cond="cond-wx"')
    assert 'id="cond-wx" data-live="1" data-observed="2026-11-19T12:00:00-05:00"' in frag["html"]
    assert "en vivo · 12:00 · estación propia" in frag["html"]
    assert render_html.render_cond_fragments([box], now="2026-11-19T12:01:00-05:00",
                                             lang="en")[0]["html"].count("live · 12:00")
    # a 150-minute-old observation admits its age, muted
    old = conditions.clean_box({"title": "El tiempo", "lead": "12 °C",
                                "observed_at": "2026-11-19T10:00:00-05:00"}, name="wx",
                               label="public")
    assert old is not None
    frag = render_html.render_cond_fragments([old], now="2026-11-19T12:30:00-05:00", lang="es")[0]
    assert "último dato 10:00" in frag["html"] and 'class="cond-stamp stale"' in frag["html"]


def test_build_wires_live_boxes_and_keeps_the_snapshot_stamp(env, live_providers):
    _fixture(env)
    env.cfg["conditions"] = {"good": {"privacy": "household"}, "live": {"privacy": "public"}}
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                       max_privacy="private", force=True, data_dir=env.data_dir)
    assert {b["id"]: b.get("live") for b in ed["conditions"]} == {"good": None, "live": True}
    doc = render_html.render(ed, "private")
    # the aside tells live.js which reader and level this page was rendered for
    assert ('<aside class="conditions" data-reader="testreader" '
            'data-max-privacy="private" aria-label="Condiciones">') in doc
    # only the live box is wired; its ear carries the swap hook
    assert 'id="cond-live" data-live="1" data-observed="2026-11-19T06:25:00-05:00"' in doc
    assert '<div class="cond-box" id="cond-good">' in doc  # snapshot box untouched
    assert '<a class="ear" id="cond-ear" data-cond="cond-live"' in doc
    # the printed snapshot stays honest about being a snapshot
    assert "a las 06:25, no en vivo" in doc
    assert '<p class="cond-stamp">en vivo' not in doc  # no live stamp at print time
