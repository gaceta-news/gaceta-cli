# Condiciones: Weather Underground (`wunderground`)

A built-in `gaceta.conditions` provider for any personal weather station on
Earth — or any point on the map, resolved to the nearest station that is
actually reporting. One observation, the day's observed high and low from the
station itself, and a two-line forecast credited to its own source: a
standing desk, not a dashboard.

Registered as the `wunderground` entry point in the `gaceta.conditions` group
(`gaceta.wunderground:WundergroundBox`), enabled per reader in reader.toml:

```toml
[conditions.wunderground]
station_id = "IEXAMPLE1"        # your PWS id... or pick the nearest one:
# lat = 51.5072                # any location on Earth; resolved to the
# lon = -0.1276                #   nearest reporting PWS (cached 24 h)
title = "El tiempo"            # default: "El tiempo" (es) / "Weather" (en)
lang = "es"                    # "es" | "en"; es prints decimal commas
api_key_env = "WU_API_KEY"     # read from os.environ, then from env_file
# env_file = "~/.hermes/.env"  # only that one variable is ever read
forecast = true                # false drops the Hoy/Mañana rows
fresh_min = 30                 # older observations → no box
live_ttl = 90                  # seconds between live() fetches (min 30)
daily_budget = 1400           # API calls/day; spent → cached data only
```

A free PWS-owner API key (1500 calls/day, 30/min) is enough: one edition
build costs three calls, and the live layer one per `live_ttl` seconds.

## The wire

All calls are stdlib `urllib`, 8 s timeout, `format=json`, metric units.
`numericPrecision=decimal` is sent **only** to the v2 PWS endpoints — the v3
endpoints reject it with HTTP 400.

| Call | Params | Cache |
|---|---|---|
| `GET /v2/pws/observations/current` | `stationId`, `format=json`, `units=m`, `numericPrecision=decimal` | — (fresh every time) |
| `GET /v2/pws/dailysummary/7day` | same as above | 15 min |
| `GET /v3/wx/forecast/daily/5day` | `geocode={lat},{lon}` (from the observation), `language=es-ES` \| `en-US`, `format=json`, `units=m` | 30 min |
| `GET /v3/location/near` | `geocode={lat},{lon}`, `product=pws`, `format=json` | resolution 24 h |

With `lat`/`lon`, the nearest station that passes QC (`qcStatus >= 0`) and
reported within the hour wins; failing that, the nearest QC-passing station
of any age; stations that failed QC are never used. Every HTTP call counts
against `daily_budget` (per instance, per local day); once it is spent the
box is served from cache until its observation is itself stale.

## The box

* **lead** — temperature, plus `lloviendo` only when the station measures
  rain (`18,7 °C · lloviendo`). A PWS cannot describe the sky, so a forecast
  phrase never goes in the lead (it would print a storm over a sunny
  afternoon); forecast phrases live in the Hoy / Mañana rows.
* **facts** (a newspaper box, ≤ 12 rows, missing values are omitted, never
  zeroed): Sensación (only when heat index/wind chill differs by ≥ 1 °C),
  Humedad, Viento (8-point compass, "en calma" under 1 km/h, · ráfaga),
  Lluvia hoy (mm, plus mm/h while raining), Máx / mín hoy (observed, from
  the station's own daily summary for its local date), Presión, UV (only
  when > 0), then Hoy / Mañana — `20° / 12° · phrase · 94 %` with the
  daypart's pictogram.
* **icon** — `rainy`/`pouring` (≥ 7.6 mm/h) from observed rain, else the
  current forecast daypart's `iconCode`.
* **ear / ear_note** — rounded temperature / the day's expected range.
* **source** — `estación IEXAMPLE1 · Weather Underground` plus
  ` · pronóstico TWC` (The Weather Company) whenever forecast data was used;
  kept under the engine's 60-character source limit.
* **observed_at** — `obsTimeUtc`, as ISO with offset.

Spanish and English are both first-class; Spanish uses the decimal comma,
`66 %`, and `N NE E SE S SO O NO` bearings.

## `live(now)` for the serve layer

The same full box shape, meant to be called often (every ~60–90 s). It
fetches **only** the current observation — the forecast and daily summary are
reused from in-process cache — and throttles itself to one fetch attempt per
`live_ttl` seconds, failures included. Instances are reusable and
thread-safe (one lock): the server keeps one instance per (reader, box) and
calls `live()` from its refresh threads.

## Fail closed

No key, HTTP error, junk JSON, missing temperature, or an observation older
than `fresh_min` → the box is skipped (None); a stale box is worse than no
box. When the budget is spent or a call fails, the last good box is served
only while its observation is still fresh. The API key never appears in any
return value, exception message, repr or log: urllib errors are wrapped with
the key redacted.

## `iconCode` → pictograms

The Weather Company's forecast `iconCode` (0–47; IBM Environmental
Intelligence Suite, "Icon Codes and Icon Images") mapped onto
`gaceta.render.icons`:

| Code(s) | TWC meaning | gaceta icon |
|---|---|---|
| 0, 1, 2 | tornado, tropical storm, hurricane | `exceptional` |
| 3, 4, 37, 38, 47 | strong storms / thunderstorms / scattered t-storms | `lightning-rainy` |
| 5, 6, 7, 8, 10, 18, 35 | wintry mixes, freezing drizzle/rain, sleet, rain + hail | `snowy-rainy` (35 → `hail`) |
| 9, 11, 12, 39, 45 | drizzle, showers, rain, scattered showers | `rainy` |
| 13–16, 25, 41–43, 46 | flurries, snow, heavy snow, blizzard, frigid, snow showers | `snowy` |
| 17 | hail | `hail` |
| 19–22 | blowing dust, foggy, haze, smoke | `fog` |
| 23, 24 | breezy, windy | `windy` |
| 26, 27, 28 | cloudy, mostly cloudy (night/day) | `cloudy` |
| 29, 30 | partly cloudy (night/day) | `partlycloudy` |
| 31, 33 | clear / fair (night) | `clear-night` |
| 32, 34, 36 | sunny / fair (day) / hot | `sunny` |
| 40 | heavy rain | `pouring` |
| 44 | not available | — (no icon) |
| other | unknown | — (no icon) |

## Privacy

This is a public repo: never write real station ids, real coordinates, place
names or personal names into code, tests or docs. The tests run against the
sanitized fixtures under `tests/fixtures/wunderground/` (fake station
`IEXAMPLE1`, London-ish coordinates) with the network monkeypatched away;
keep it that way.
