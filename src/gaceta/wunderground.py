"""Condiciones: Weather Underground (The Weather Company PWS API).

Any personal weather station on Earth by id, or any point on the map resolved
to the nearest reporting PWS. A standing desk, not a dashboard: one live
observation, the day's observed high/low from the station itself, and a
two-line forecast credited to its own source.

    [conditions.wunderground]
    station_id = "IEXAMPLE1"          # or lat = 51.5072, lon = -0.1276
    lang = "es"                       # "es" | "en"; es prints decimal commas
    api_key_env = "WU_API_KEY"        # os.environ first, then env_file

Fails closed like every provider: no key, HTTP error, junk JSON, no
temperature or an observation older than ``fresh_min`` → no box. The API key
never echoes: urllib errors are wrapped with the key redacted.

``live(now)`` is the same box for the serve layer: throttled to one
observation fetch per ``live_ttl`` seconds, forecast and daily summary reused
from in-process cache. Instances are reusable and thread-safe (the serve layer
keeps one per reader/box and calls live() from its refresh threads).
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api.weather.com"
PWS_PARAMS = {"format": "json", "units": "m", "numericPrecision": "decimal"}
TIMEOUT = 8
FRESH_MIN = 30          # minutes; older observations are a stale box
LIVE_TTL = 90           # seconds between live() observation fetches (min 30)
DAILY_BUDGET = 1400     # API calls per day (free PWS keys: 1500/day, 30/min)
NEAR_TTL = timedelta(hours=24)
SUMMARY_TTL = timedelta(minutes=15)
FORECAST_TTL = timedelta(minutes=30)
POURING_MM_H = 7.6      # observed rate over which the box icon is 'pouring'

TEXTS = {
    "es": {"title": "El tiempo", "feels": "Sensación", "humidity": "Humedad",
           "wind": "Viento", "rain": "Lluvia hoy", "high_low": "Máx / mín hoy",
           "pressure": "Presión", "today": "Hoy", "tomorrow": "Mañana",
           "raining": "lloviendo", "calm": "en calma", "gust": "ráfaga",
           "note": "máx {hi}° · mín {lo}°",
           "source": "estación {station} · Weather Underground",
           "forecast_source": " · pronóstico TWC"},
    "en": {"title": "Weather", "feels": "Feels like", "humidity": "Humidity",
           "wind": "Wind", "rain": "Rain today", "high_low": "High / low today",
           "pressure": "Pressure", "today": "Today", "tomorrow": "Tomorrow",
           "raining": "raining", "calm": "calm", "gust": "gust",
           "note": "high {hi}° · low {lo}°",
           "source": "station {station} · Weather Underground",
           "forecast_source": " · forecast TWC"},
}
COMPASS = {"es": ("N", "NE", "E", "SE", "S", "SO", "O", "NO"),
           "en": ("N", "NE", "E", "SE", "S", "SW", "W", "NW")}

# TWC forecast iconCode 0-47 (IBM Environmental Intelligence Suite, "Icon
# Codes and Icon Images") mapped onto render/icons.py names. 44 is "not
# available"; unknown codes render no icon at all.
ICON_BY_CODE = {
    0: "exceptional",       # tornado
    1: "exceptional",       # tropical storm
    2: "exceptional",       # hurricane
    3: "lightning-rainy",   # strong storms
    4: "lightning-rainy",   # thunderstorms
    5: "snowy-rainy",       # rain / snow
    6: "snowy-rainy",       # rain / sleet
    7: "snowy-rainy",       # wintry mix
    8: "snowy-rainy",       # freezing drizzle
    9: "rainy",             # drizzle
    10: "snowy-rainy",      # freezing rain
    11: "rainy",            # showers
    12: "rainy",            # rain
    13: "snowy",            # flurries
    14: "snowy",            # snow showers
    15: "snowy",            # blowing / drifting snow
    16: "snowy",            # snow
    17: "hail",             # hail
    18: "snowy-rainy",      # sleet
    19: "fog",              # blowing dust / sandstorm
    20: "fog",              # foggy
    21: "fog",              # haze
    22: "fog",              # smoke
    23: "windy",            # breezy
    24: "windy",            # windy
    25: "snowy",            # frigid / ice crystals
    26: "cloudy",           # cloudy
    27: "cloudy",           # mostly cloudy (night)
    28: "cloudy",           # mostly cloudy (day)
    29: "partlycloudy",     # partly cloudy (night)
    30: "partlycloudy",     # partly cloudy (day)
    31: "clear-night",      # clear
    32: "sunny",            # sunny
    33: "clear-night",      # fair / mostly clear (night)
    34: "sunny",            # fair / mostly sunny (day)
    35: "hail",             # mixed rain and hail
    36: "sunny",            # hot
    37: "lightning-rainy",  # isolated thunderstorms
    38: "lightning-rainy",  # scattered thunderstorms
    39: "rainy",            # scattered showers
    40: "pouring",          # heavy rain
    41: "snowy",            # scattered snow showers
    42: "snowy",            # heavy snow
    43: "snowy",            # blizzard
    44: "",                 # not available → no icon
    45: "rainy",            # scattered showers (night)
    46: "snowy",            # scattered snow showers (night)
    47: "lightning-rainy",  # scattered thunderstorms (night)
}


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc(value) -> datetime | None:
    """The API's "...Z" timestamps → aware UTC datetimes."""
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _pick(arr, i):
    """Array element at i, or None (past dayparts come back as null)."""
    try:
        return arr[i]
    except (TypeError, IndexError):
        return None


def _phrase(arr, i) -> str:
    return " ".join(str(_pick(arr, i) or "").split())


def _station_local(observed: datetime, obs: dict) -> datetime:
    """The observation in the station's own wall-clock offset (obsTimeLocal −
    obsTimeUtc), so the paper prints the reader's HH:MM, not UTC, anywhere in
    the world. Falls back to UTC when obsTimeLocal is missing or odd."""
    try:
        local = datetime.fromisoformat(str(obs.get("obsTimeLocal") or "").replace(" ", "T"))
        offset = local - observed.replace(tzinfo=None)
        minutes = round(offset.total_seconds() / 900) * 15   # quarter-hour zones
        if abs(minutes) <= 14 * 60:
            return observed.astimezone(timezone(timedelta(minutes=minutes)))
    except (TypeError, ValueError):
        pass
    return observed


class WundergroundBox:
    """One instance per [conditions.wunderground] table; reusable, thread-safe."""

    def __init__(self, table: dict):
        lang = str(table.get("lang") or "es").strip().lower()[:2]
        self.lang = lang if lang in TEXTS else "es"
        self.t = TEXTS[self.lang]
        self.title = str(table.get("title") or "").strip() or self.t["title"]
        self.station_id = str(table.get("station_id") or "").strip()
        self.lat, self.lon = _num(table.get("lat")), _num(table.get("lon"))
        self.api_key_env = str(table.get("api_key_env") or "WU_API_KEY").strip()
        self.key = os.environ.get(self.api_key_env) or self._env_file(table.get("env_file"))
        self.forecast_on = table.get("forecast", True) not in (False, "false", 0)
        self.fresh_min = int(_num(table.get("fresh_min")) or FRESH_MIN)
        self.live_ttl = max(30, int(_num(table.get("live_ttl")) or LIVE_TTL))
        self.daily_budget = int(_num(table.get("daily_budget")) or DAILY_BUDGET)
        # one lock guards every cache below: live() runs on refresh threads
        self._lock = threading.Lock()
        self._near: str | None = None
        self._near_at: datetime | None = None
        self._summ: list | None = None
        self._summ_station = ""
        self._summ_at: datetime | None = None
        self._fc: dict | None = None
        self._fc_geo = ""
        self._fc_at: datetime | None = None
        self._box: dict | None = None            # last good box
        self._box_obs: datetime | None = None
        self._tried_at: datetime | None = None   # last observation attempt
        self._day: date | None = None            # budget counter, per day
        self._calls = 0

    # ------------------------------------------------------------- secrets
    def _env_file(self, path) -> str:
        """Read ONLY the configured variable from an env file, if any."""
        if not path:
            return ""
        try:
            lines = Path(os.path.expanduser(str(path))).read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        for line in lines:
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == self.api_key_env:
                return value.strip().strip("'\"")
        return ""

    def _redact(self, text: str) -> str:
        text = re.sub(r"apiKey=[^&\s'\"]*", "apiKey=***", str(text))
        return text.replace(self.key, "***") if self.key else text

    # ------------------------------------------------------------ the wire
    def _spend(self, now: datetime) -> bool:
        """Count one call against the day's budget; False when it is spent."""
        if self._day != now.date():
            self._day, self._calls = now.date(), 0
        if self._calls >= self.daily_budget:
            return False
        self._calls += 1
        return True

    def _get(self, path: str, params: dict, now: datetime):
        """One GET → parsed JSON; None when the day's budget is spent,
        RuntimeError (key redacted) when the call or payload is broken."""
        if not self._spend(now):
            return None
        url = f"{BASE}{path}?{urlencode({**params, 'apiKey': self.key})}"
        try:
            with urlopen(Request(url), timeout=TIMEOUT) as resp:
                return json.load(resp)
        except Exception as e:  # noqa: BLE001 — never let the key echo
            raise RuntimeError(f"{type(e).__name__}: {self._redact(str(e))}") from None

    # ------------------------------------------------------------ station
    def _station(self, now: datetime) -> str | None:
        """The configured station, or the nearest reporting PWS (24 h cache).
        Stations reporting within the hour and passing QC win; QC-passing
        stations of any age come next; the rest are not good enough."""
        if self.station_id:
            return self.station_id
        if self._near and self._near_at and (now - self._near_at) <= NEAR_TTL:
            return self._near
        if self.lat is None or self.lon is None:
            return None
        loc = (self._get("/v3/location/near",
                         {"geocode": f"{self.lat},{self.lon}", "product": "pws",
                          "format": "json"}, now) or {}).get("location") or {}
        ids, fresh, ok = loc.get("stationId") or [], [], []
        for i in range(len(ids)):
            qc, up = _num(_pick(loc.get("qcStatus"), i)), _num(_pick(loc.get("updateTimeUtc"), i))
            if qc is None or qc < 0:
                continue
            ok.append(i)
            if up is not None and now.timestamp() - up <= 3600:
                fresh.append(i)
        for i in fresh or ok:
            self._near, self._near_at = str(ids[i]), now
            return self._near
        return None

    def _observation(self, station: str, now: datetime) -> dict | None:
        obs = (self._get("/v2/pws/observations/current",
                         {**PWS_PARAMS, "stationId": station}, now)
               or {}).get("observations") or []
        return obs[0] if isinstance(obs, list) and obs else None

    # -------------------------------------------------------- cached sides
    def _summaries(self, station: str, now: datetime) -> list:
        """7-day observed summaries, cached 15 min; failures keep the cache."""
        if (self._summ is not None and self._summ_station == station and self._summ_at
                and (now - self._summ_at) <= SUMMARY_TTL):
            return self._summ
        try:
            data = self._get("/v2/pws/dailysummary/7day",
                             {**PWS_PARAMS, "stationId": station}, now)
        except RuntimeError:
            data = None
        if data is None:      # budget spent or the call failed: cached data only
            return self._summ if self._summ_station == station else []
        rows = (data or {}).get("summaries") or []
        self._summ, self._summ_station, self._summ_at = (
            list(rows) if isinstance(rows, list) else []), station, now
        return self._summ

    def _today_summary(self, obs: dict, station: str, now: datetime) -> dict:
        """The summary for the station's own local day, not ours."""
        today = str(obs.get("obsTimeLocal") or "")[:10]
        for row in self._summaries(station, now):
            if str(row.get("obsTimeLocal") or "")[:10] == today:
                return row
        return {}

    def _forecast(self, obs: dict, now: datetime) -> dict:
        """TWC 5-day forecast at the station's own geocode, cached 30 min."""
        geo = f"{obs.get('lat')},{obs.get('lon')}"
        if (self._fc is not None and self._fc_geo == geo and self._fc_at
                and (now - self._fc_at) <= FORECAST_TTL):
            return self._fc
        try:
            data = self._get("/v3/wx/forecast/daily/5day",
                             {"geocode": geo, "format": "json", "units": "m",
                              "language": "es-ES" if self.lang == "es" else "en-US"}, now)
        except RuntimeError:
            data = None
        if not isinstance(data, dict):
            return self._fc if self._fc_geo == geo else {}
        self._fc, self._fc_geo, self._fc_at = data, geo, now
        return data

    # ----------------------------------------------------------------- box
    def box(self, now: datetime) -> dict | None:
        """The edition snapshot: a fresh observation, or nothing."""
        return self._edition(now)

    def live(self, now: datetime) -> dict | None:
        """The same box for the serve layer: at most one observation fetch
        per live_ttl seconds, forecast and summary served from cache."""
        return self._edition(now, live=True)

    def _edition(self, now: datetime, *, live: bool = False) -> dict | None:
        if now.tzinfo is None:
            now = now.astimezone()
        if not self.key:
            return None
        try:
            with self._lock:
                if live and self._tried_at is not None and \
                        0 <= (now - self._tried_at).total_seconds() < self.live_ttl:
                    return self._fresh(now)
                self._tried_at = now        # one attempt per TTL, failures too
                station = self._station(now)
                obs = self._observation(station, now) if station else None
                if obs is None:             # budget spent or the call failed
                    return self._fresh(now)
                built = self._build(station, obs, now)
                if built is not None:
                    self._box, self._box_obs = built, _utc(obs.get("obsTimeUtc"))
                return built
        except Exception:  # noqa: BLE001 — a number never blocks the paper
            return None

    def _fresh(self, now: datetime) -> dict | None:
        """The last good box, while its observation is still fresh."""
        if self._box is None or self._box_obs is None:
            return None
        return self._box if (now - self._box_obs) <= timedelta(minutes=self.fresh_min) else None

    def _build(self, station: str, obs: dict, now: datetime) -> dict | None:
        m = obs.get("metric") or {}
        temp, observed = _num(m.get("temp")), _utc(obs.get("obsTimeUtc"))
        if temp is None or observed is None:      # missing values are not zero
            return None
        if (now - observed) > timedelta(minutes=self.fresh_min):
            return None                          # a stale box is worse than none
        t, fmt = self.t, self._fmt
        rate, total = _num(m.get("precipRate")), _num(m.get("precipTotal"))
        raining = rate is not None and rate > 0
        summary = self._today_summary(obs, station, now)
        fc = self._forecast(obs, now) if self.forecast_on else {}
        dp = ((fc.get("daypart") or [{}])[0] or {})
        i = next((k for k, c in enumerate(dp.get("iconCode") or []) if c is not None), None)
        # a PWS observation cannot describe the sky: the lead only states what
        # was measured (rain). The forecast phrase lives in the Hoy/Mañana rows,
        # never in the lead, so a sunny afternoon isn't printed as a storm.
        state = t["raining"] if raining else ""

        facts: list[list[str]] = []
        heat, chill = _num(m.get("heatIndex")), _num(m.get("windChill"))
        feels = next((c for c in ([chill, heat] if temp < 24 else [heat, chill])
                      if c is not None), None)
        if feels is not None and abs(feels - temp) >= 1:
            facts.append([t["feels"], f"{fmt(feels)} °C"])
        hum = _num(obs.get("humidity"))
        if hum is not None:
            facts.append([t["humidity"], self._pct(hum)])
        wind, gust, bearing = _num(m.get("windSpeed")), _num(m.get("windGust")), _num(obs.get("winddir"))
        if wind is not None:
            text = t["calm"] if wind < 1 else f"{wind:.0f} km/h"
            if wind >= 1 and bearing is not None:
                text += f" {COMPASS[self.lang][round(bearing / 45) % 8]}"
            if gust is not None and gust >= 1:
                text += f" · {t['gust']} {gust:.0f}"
            facts.append([t["wind"], text])
        if total is not None:
            facts.append([t["rain"], f"{fmt(total)} mm" + (f" · {fmt(rate)} mm/h" if raining else "")])
        sm = summary.get("metric") or {}
        hi, lo = _num(sm.get("tempHigh")), _num(sm.get("tempLow"))
        if hi is not None and lo is not None:
            facts.append([t["high_low"], f"{fmt(hi)}° / {fmt(lo)}°"])
        pressure = _num(m.get("pressure"))
        if pressure is not None:
            facts.append([t["pressure"], f"{pressure:.0f} hPa"])
        uv = _num(obs.get("uv"))
        if uv is not None and uv > 0:            # at night there is no UV row
            facts.append(["UV", f"{uv:.0f}"])
        facts.extend(self._forecast_rows(fc, dp))

        box = {"title": self.title,
               "lead": f"{fmt(temp)} °C" + (f" · {state}" if state else ""),
               "facts": facts,
               "source": t["source"].format(station=station)
                        + (t["forecast_source"] if fc else ""),
               "observed_at": _station_local(observed, obs).isoformat(timespec="seconds")}
        if raining:
            box["icon"] = "pouring" if rate >= POURING_MM_H else "rainy"
        else:
            icon = ICON_BY_CODE.get(_pick(dp.get("iconCode"), i))
            if icon:
                box["icon"] = icon
        box["ear"] = f"{temp:.0f}°"
        fhi, flo = _num(_pick(fc.get("calendarDayTemperatureMax"), 0)), \
            _num(_pick(fc.get("calendarDayTemperatureMin"), 0))
        if fhi is not None and flo is not None:
            box["ear_note"] = t["note"].format(hi=f"{fhi:.0f}", lo=f"{flo:.0f}")
        elif state:
            box["ear_note"] = state
        return box

    def _forecast_rows(self, fc: dict, dp: dict) -> list[list[str]]:
        """Hoy/Mañana: expected range, sky phrase, rain chance, its icon."""
        t = self.t
        rows = []
        for d in (0, 1):
            hi = _num(_pick(fc.get("calendarDayTemperatureMax"), d))
            lo = _num(_pick(fc.get("calendarDayTemperatureMin"), d))
            if hi is None or lo is None:
                continue
            i = 2 * d                        # the daytime daypart
            text = f"{hi:.0f}° / {lo:.0f}°"
            phrase = _phrase(dp.get("wxPhraseShort"), i) or _phrase(dp.get("wxPhraseLong"), i)
            if phrase:
                text += f" · {phrase}"
            chance = _num(_pick(dp.get("precipChance"), i))
            if chance is not None and chance > 0:
                text += f" · {self._pct(chance)}"
            label = t["today"] if d == 0 else t["tomorrow"]
            icon = ICON_BY_CODE.get(_pick(dp.get("iconCode"), i))
            rows.append([label, text, icon] if icon else [label, text])
        return rows

    # ---------------------------------------------------------- formatting
    def _fmt(self, value: float, decimals: int = 1) -> str:
        """Decimal comma in Spanish, as the reference provider prints."""
        text = f"{value:.{decimals}f}"
        return text.replace(".", ",") if self.lang == "es" else text

    def _pct(self, value: float) -> str:
        return f"{value:.0f} %" if self.lang == "es" else f"{value:.0f}%"
