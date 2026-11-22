"""WundergroundBox: the Weather Underground / TWC conditions provider.

Fixture-driven and network-free: every fetch goes through a monkeypatched
``urlopen`` serving the sanitized IEXAMPLE1 (fake London) responses under
tests/fixtures/wunderground/. The fake station and coordinates are examples;
real ids, coordinates and place names never belong in this public repo.
"""
from __future__ import annotations

import importlib.metadata
import io
import json
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from gaceta import wunderground as wu

FIX = Path(__file__).parent / "fixtures" / "wunderground"
# 90 s after the fixtures' observation (2026-11-22T18:03:32Z), same local day
NOW = datetime(2026, 11, 22, 18, 5, tzinfo=timezone.utc)


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def fake(monkeypatch, *, calls=None, current=None, summary=None, forecast=None,
         near=None, fail=()):
    """Serve fixture JSON by URL shape; ``fail`` holds URL substrings whose
    calls raise an URLError carrying the full URL (as the real one can)."""

    def urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if calls is not None:
            calls.append(url)
        path = urlparse(url).path
        if any(bit in path for bit in fail):
            raise urllib.error.URLError(f"boom {url}")
        if "observations/current" in path:
            body = load("current.json") if current is None else current
        elif "dailysummary" in path:
            body = load("daily7.json") if summary is None else summary
        elif "forecast/daily" in path:
            lang = parse_qs(urlparse(url).query).get("language", [""])[0]
            default = "forecast5_es.json" if lang.startswith("es") else "forecast5_en.json"
            body = load(default) if forecast is None else forecast
        elif "location/near" in path:
            body = load("near.json") if near is None else near
        else:
            raise AssertionError(f"unexpected call to {path}")
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        return io.BytesIO(payload)

    monkeypatch.setattr(wu, "urlopen", urlopen)


def make(monkeypatch, table=None, *, calls=None, **bodies):
    monkeypatch.setenv("WU_API_KEY", "TESTKEY123")
    fake(monkeypatch, calls=calls, **bodies)
    return wu.WundergroundBox({"station_id": "IEXAMPLE1", **(table or {})})


def params(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# ------------------------------------------------------------- the box, es
def test_station_id_box_in_spanish(monkeypatch):
    box = make(monkeypatch).box(NOW)
    assert box["title"] == "El tiempo"
    assert box["lead"] == "18,7 °C"   # decimal comma; no forecast phrase in the lead
    assert box["icon"] == "lightning-rainy"                   # daypart iconCode 4
    assert box["observed_at"] == "2026-11-22T13:03:32-05:00"   # station wall clock, not UTC
    assert datetime.fromisoformat(box["observed_at"]) == datetime.fromisoformat("2026-11-22T18:03:32+00:00")
    assert box["facts"] == [
        ["Humedad", "66 %"],
        ["Viento", "3 km/h N · ráfaga 12"],
        ["Lluvia hoy", "0,3 mm"],
        ["Máx / mín hoy", "18,7° / 11,6°"],                    # observed, 7-day summary
        ["Presión", "1004 hPa"],
        ["UV", "3"],
        ["Hoy", "20° / 12° · Tormentas eléctricas · 94 %", "lightning-rainy"],
        ["Mañana", "21° / 11° · Tormentas el. por la tarde · 77 %", "lightning-rainy"],
    ]
    assert box["source"] == "estación IEXAMPLE1 · Weather Underground · pronóstico TWC"
    assert box["ear"] == "19°" and box["ear_note"] == "máx 20° · mín 12°"


def test_box_in_english(monkeypatch):
    box = make(monkeypatch, {"lang": "en"}).box(NOW)
    assert box["title"] == "Weather"
    assert box["lead"] == "18.7 °C"
    assert box["facts"] == [
        ["Humidity", "66%"],
        ["Wind", "3 km/h N · gust 12"],
        ["Rain today", "0.3 mm"],
        ["High / low today", "18.7° / 11.6°"],
        ["Pressure", "1004 hPa"],
        ["UV", "3"],
        ["Today", "18° / 10° · T-Storms · 91%", "lightning-rainy"],
        ["Tomorrow", "19° / 9° · PM T-Storms · 77%", "lightning-rainy"],
    ]
    assert box["source"] == "station IEXAMPLE1 · Weather Underground · forecast TWC"
    assert box["ear_note"] == "high 18° · low 10°"


def test_builtin_urls_match_the_real_api(monkeypatch):
    """v2/pws endpoints take numericPrecision; v3 forecast does not (HTTP 400);
    v3 location/near takes neither units nor numericPrecision."""
    calls = []
    make(monkeypatch, calls=calls).box(NOW)
    by_path = {urlparse(u).path: u for u in calls}
    assert params(by_path["/v2/pws/observations/current"]) == {
        "format": "json", "units": "m", "numericPrecision": "decimal",
        "stationId": "IEXAMPLE1", "apiKey": "TESTKEY123"}
    assert params(by_path["/v2/pws/dailysummary/7day"]) == {
        "format": "json", "units": "m", "numericPrecision": "decimal",
        "stationId": "IEXAMPLE1", "apiKey": "TESTKEY123"}
    assert params(by_path["/v3/wx/forecast/daily/5day"]) == {
        "geocode": "51.5072,-0.1276", "format": "json", "units": "m",
        "language": "es-ES", "apiKey": "TESTKEY123"}
    calls.clear()
    make(monkeypatch, {"lang": "en"}, calls=calls).box(NOW)
    forecast = [u for u in calls if "forecast" in urlparse(u).path][0]
    assert params(forecast)["language"] == "en-US"
    calls.clear()
    make(monkeypatch, {"station_id": "", "lat": 51.5072, "lon": -0.1276},
         calls=calls).box(NOW)
    near = [u for u in calls if "location/near" in urlparse(u).path][0]
    assert params(near) == {
        "geocode": "51.5072,-0.1276", "product": "pws", "format": "json",
        "apiKey": "TESTKEY123"}


# ------------------------------------------------------- lat/lon resolution
def test_latlon_resolves_nearest_station_and_caches_it(monkeypatch):
    """near.json has no station updated within the hour, so the qc-passing
    ladder resolves the nearest one; the resolution is cached for 24 h."""
    calls = []
    monkeypatch.setenv("WU_API_KEY", "TESTKEY123")
    fake(monkeypatch, calls=calls)
    wub = wu.WundergroundBox({"lat": 51.5072, "lon": -0.1276})
    box = wub.box(NOW)
    assert box is not None and box["ear"] == "19°"
    current = [u for u in calls if "observations/current" in u][0]
    assert "stationId=IEXAMPLE1" in current
    assert sum("location/near" in u for u in calls) == 1
    wub.box(NOW + timedelta(minutes=10))          # near + sides stay cached
    assert sum("location/near" in u for u in calls) == 1
    assert sum("dailysummary" in u for u in calls) == 1
    assert sum("forecast/daily" in u for u in calls) == 1


def test_near_prefers_fresh_then_qc(monkeypatch):
    calls = []
    epoch = int(NOW.timestamp())
    near = load("near.json")
    loc = near["location"]
    monkeypatch.setenv("WU_API_KEY", "TESTKEY123")

    # a fresh qc-passing station beats a nearer one that failed QC
    loc["stationId"], loc["qcStatus"] = ["IBADQC1", "IGOODFRESH"], [-1, 1]
    loc["updateTimeUtc"] = [epoch - 600, epoch - 600]
    fake(monkeypatch, calls=calls, near=near)
    wub = wu.WundergroundBox({"lat": 51.51, "lon": -0.13})
    assert wub.box(NOW)["source"].startswith("estación IGOODFRESH ")
    assert "stationId=IGOODFRESH" in [u for u in calls if "current" in u][0]

    # nothing fresh: the first qc-passing station of any age still wins
    calls.clear()
    loc["stationId"], loc["qcStatus"] = ["IBADQC1", "ISTALEOK"], [-1, 0]
    loc["updateTimeUtc"] = [epoch - 90_000, epoch - 90_000]
    assert wu.WundergroundBox({"lat": 51.51, "lon": -0.13}).box(NOW) is not None
    assert "stationId=ISTALEOK" in [u for u in calls if "current" in u][0]

    # every station failed QC: fail closed, and no observation is fetched
    calls.clear()
    loc["stationId"], loc["qcStatus"] = ["IBADQC1"], [-1]
    assert wu.WundergroundBox({"lat": 51.51, "lon": -0.13}).box(NOW) is None
    assert not [u for u in calls if "current" in u]


# ------------------------------------------------------------- fail closed
def test_stale_observation_is_skipped(monkeypatch):
    wub = make(monkeypatch)
    assert wub.box(NOW) is not None
    assert wub.box(NOW + timedelta(minutes=31)) is None


def test_missing_key_returns_none_and_fetches_nothing(monkeypatch):
    monkeypatch.delenv("WU_API_KEY", raising=False)
    calls = []
    fake(monkeypatch, calls=calls)
    wub = wu.WundergroundBox({"station_id": "IEXAMPLE1"})
    assert wub.box(NOW) is None and wub.live(NOW) is None
    assert calls == []


def test_env_file_and_os_environ_precedence(monkeypatch, tmp_path):
    """The env file is consulted for the configured variable only, and a
    value already in os.environ wins over whatever the file says."""
    envf = tmp_path / "example.env"
    envf.write_text("OTHER=x\nTEST_WU_KEY=FILE9KEY\n", encoding="utf-8")
    calls = []
    monkeypatch.delenv("TEST_WU_KEY", raising=False)
    fake(monkeypatch, calls=calls)
    table = {"station_id": "IEXAMPLE1", "env_file": str(envf),
             "api_key_env": "TEST_WU_KEY"}
    wub = wu.WundergroundBox(table)
    assert wub.box(NOW) is not None
    assert "apiKey=FILE9KEY" in calls[0]
    # a variable in os.environ wins over the file
    monkeypatch.setenv("TEST_WU_KEY", "ENVKEY7")
    calls.clear()
    assert wu.WundergroundBox(table).box(NOW) is not None
    assert "apiKey=ENVKEY7" in calls[0]


def test_http_error_is_none_and_key_is_redacted(monkeypatch):
    monkeypatch.setenv("WU_API_KEY", "SECRETK")

    def boom(req, timeout=None):
        raise urllib.error.URLError(f"cannot reach {req.full_url}")

    monkeypatch.setattr(wu, "urlopen", boom)
    wub = wu.WundergroundBox({"station_id": "IEXAMPLE1"})
    assert wub.box(NOW) is None               # skipped, nothing raises to the engine
    with pytest.raises(RuntimeError) as ei:   # and the wrapped error is clean
        wub._get("/v2/pws/observations/current", {}, NOW)
    assert "SECRETK" not in str(ei.value)
    assert "apiKey=***" in str(ei.value)


def test_fail_closed_on_junk(monkeypatch):
    monkeypatch.setenv("WU_API_KEY", "TESTKEY123")
    no_temp = load("current.json")
    no_temp["observations"][0]["metric"]["temp"] = None
    for current in (b"{nope: not json", {"observations": []}, no_temp):
        fake(monkeypatch, current=current)
        assert wu.WundergroundBox({"station_id": "IEXAMPLE1"}).box(NOW) is None


# ------------------------------------------------------------ rain and sky
def test_raining_lead_and_icons(monkeypatch):
    cur = load("current.json")
    obs = cur["observations"][0]
    obs["metric"]["precipRate"], obs["metric"]["precipTotal"] = 2.4, 5.2
    box = make(monkeypatch, current=cur).box(NOW)
    assert box["lead"] == "18,7 °C · lloviendo"      # observed, not forecast
    assert box["icon"] == "rainy"
    assert box["facts"][2] == ["Lluvia hoy", "5,2 mm · 2,4 mm/h"]
    obs["metric"]["precipRate"] = 9.0                 # heavy rain pours
    assert make(monkeypatch, current=cur).box(NOW)["icon"] == "pouring"


def test_forecast_disabled(monkeypatch):
    calls = []
    box = make(monkeypatch, {"forecast": False}, calls=calls).box(NOW)
    assert box["lead"] == "18,7 °C"                   # a PWS cannot name the sky
    assert box["source"] == "estación IEXAMPLE1 · Weather Underground"
    assert "icon" not in box and "ear_note" not in box
    labels = [row[0] for row in box["facts"]]
    assert "Hoy" not in labels and "Mañana" not in labels
    assert not [u for u in calls if "forecast" in urlparse(u).path]


def test_forecast_failure_degrades_gracefully(monkeypatch):
    box = make(monkeypatch, fail=("forecast/daily",)).box(NOW)
    assert box is not None and box["lead"] == "18,7 °C"
    assert "pronóstico" not in box["source"]
    assert not [row for row in box["facts"] if row[0] in ("Hoy", "Mañana")]


# ------------------------------------------------- live(): cache and budget
def test_live_refetches_the_observation_only(monkeypatch):
    calls = []
    wub = make(monkeypatch, calls=calls)
    first = wub.live(NOW)
    assert first is not None

    def n(sub):
        return sum(sub in u for u in calls)

    assert (n("observations/current"), n("forecast/daily"), n("dailysummary")) == (1, 1, 1)
    second = wub.live(NOW + timedelta(seconds=95))    # past the TTL, sides cached
    assert second == first
    assert (n("observations/current"), n("forecast/daily"), n("dailysummary")) == (2, 1, 1)


def test_live_ttl_throttles(monkeypatch):
    calls = []
    wub = make(monkeypatch, calls=calls)
    assert wub.live(NOW) is not None
    base = len(calls)
    assert wub.live(NOW + timedelta(seconds=30)) is not None   # within the TTL
    assert len(calls) == base
    assert wub.live(NOW + timedelta(seconds=95)) is not None  # past it
    assert sum("observations/current" in u for u in calls) == 2
    assert wu.WundergroundBox({"station_id": "IEXAMPLE1", "live_ttl": 5}).live_ttl == 30


def test_daily_budget_serves_cached_data_only(monkeypatch):
    calls = []
    wub = make(monkeypatch, {"daily_budget": 3}, calls=calls)
    first = wub.box(NOW)                      # current + summary + forecast
    assert first is not None and len(calls) == 3
    later = wub.box(NOW + timedelta(minutes=5))     # budget spent: cached box
    assert later == first and len(calls) == 3
    assert wub.box(NOW + timedelta(minutes=35)) is None  # and never a stale one
    assert len(calls) == 3
    # a new day resets the budget (the observation is now too old to print)
    assert wub.box(NOW + timedelta(hours=26)) is None
    assert len(calls) == 4


def test_threads_share_one_fetch_and_one_box(monkeypatch):
    calls = []
    wub = make(monkeypatch, calls=calls)
    out: list = []

    def worker():
        out.append(wub.live(NOW))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 8 and all(b is not None and b == out[0] for b in out)
    assert sum("observations/current" in u for u in calls) == 1
    assert sum("forecast/daily" in u for u in calls) == 1


# --------------------------------------------------------- plugin plumbing
def test_registered_as_conditions_provider():
    try:
        dist = importlib.metadata.distribution("gaceta")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("gaceta distribution not installed in this environment")
    eps = [ep for ep in dist.entry_points if ep.group == "gaceta.conditions"]
    assert any(ep.name == "wunderground"
               and ep.value == "gaceta.wunderground:WundergroundBox" for ep in eps), eps


def test_box_passes_the_engine_contract(monkeypatch):
    from gaceta import conditions
    from gaceta.render import icons
    box = conditions.clean_box(make(monkeypatch).box(NOW), name="wu", label="private")
    assert box is not None and len(box["facts"]) == 8 <= conditions.MAX_FACTS
    assert icons.svg(box["icon"]) and box["ear"] == "19°"
    datetime.fromisoformat(box["observed_at"])


def test_every_mapped_icon_exists_in_render():
    from gaceta.render import icons
    for name in wu.ICON_BY_CODE.values():
        assert name == "" or name in icons.ICONS
