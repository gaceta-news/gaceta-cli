"""Condiciones: small boxes of moving numbers (weather, a price, a level).

A standing desk, not a story: a box stamped "a las HH:MM, no en vivo" with the
source named, printed after the portada, never in the headline area. Values
are captured once, when the edition is built, and printed as a snapshot — the
static export is honest about being a paper. A provider MAY also implement
``live(now: datetime) -> dict | None`` (same box contract, cheap, thread-safe);
the served page (serve.py) then refreshes its boxes in place via
``LiveConditions`` + /api/conditions, while a static export keeps rendering
exactly the snapshot it always did.

Providers are plugins registered under the entry-point group ``gaceta.conditions``::

    [project.entry-points."gaceta.conditions"]
    weather = "my_package.weather:WeatherBox"

and enabled per reader in reader.toml::

    [conditions.weather]
    privacy = "household"   # optional; default "private" (fail closed)
    live_ttl = 90           # optional; seconds between live() calls (min 30)
    # ...any other keys are the provider's own config

The entry point resolves to a class constructed with that table (a dict) that
implements ``box(now: datetime) -> dict | None`` and may implement ``live``.
The box contract (all strings plain text; the renderer escapes everything and
never trusts HTML):

    {"title": "El tiempo",              # required
     "lead": "10 °C · niebla",          # optional big line
     "facts": [["Lluvia 24 h", "0 mm"], ...],   # optional label/value rows
     "source": "estación propia",       # optional, printed with the stamp
     "observed_at": "<ISO datetime>",   # optional; stamp falls back to build time
     "icon": "fog",                     # optional pictogram (render/icons.py names)
     "ear": "10°", "ear_note": "máx 22° · mín 13°"}  # optional nameplate ear

Fact rows may carry a third element, an icon name for that row (e.g. a forecast
day). The first box with an ``ear`` is also shown beside the nameplate, the way
printed dailies carry the weather in the masthead's "ear". Boxes from providers
with a ``live`` method are marked ``"live": True`` at build time so the renderer
knows which boxes to wire; providers without it stay snapshot-only and render
exactly as before.

A provider that fails, returns None or returns junk is skipped: the edition is
never blocked on a number, and a stale box is worse than no box. The live layer
adds the same rule as a negative cache — a failed source is retried at most
once a minute, never on every request.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime
from importlib.metadata import entry_points

from . import privacy

ENTRY_POINT_GROUP = "gaceta.conditions"
MAX_FACTS = 12
MAX_TEXT = 120
DEFAULT_LIVE_TTL = 90  # seconds between live() calls for one box
MIN_LIVE_TTL = 30      # a shorter table value is clamped up to this
NEG_LIVE_TTL = 60      # a failed live source is retried after max(ttl, this)


def providers() -> dict[str, str]:
    """name -> entry point value string, from the gaceta.conditions group."""
    out: dict[str, str] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        out.setdefault(ep.name, ep.value)
    return out


def _load(spec: str):
    import importlib

    module_name, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def _text(value, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(value or "").split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def clean_box(raw, *, name: str, label: str) -> dict | None:
    """Validate a provider's box into the edition shape, or None."""
    if not isinstance(raw, dict):
        return None
    title = _text(raw.get("title"))
    if not title:
        return None
    box: dict = {"id": name, "title": title, "privacy": label}
    lead = _text(raw.get("lead"))
    if lead:
        box["lead"] = lead
    facts = []
    for row in raw.get("facts") or []:
        if isinstance(row, (list, tuple)) and len(row) in (2, 3):
            k, v = _text(row[0], 40), _text(row[1], 60)
            if k and v:
                icon = _text(row[2], 24) if len(row) == 3 else ""
                facts.append([k, v, icon] if icon else [k, v])
    if facts:
        box["facts"] = facts[:MAX_FACTS]
    for key, limit in (("icon", 24), ("ear", 12), ("ear_note", 40)):
        value = _text(raw.get(key), limit)
        if value:
            box[key] = value
    source = _text(raw.get("source"), 60)
    if source:
        box["source"] = source
    observed = str(raw.get("observed_at") or "").strip()
    if observed:
        try:
            datetime.fromisoformat(observed)
            box["observed_at"] = observed
        except ValueError:
            pass
    if not (lead or facts):
        return None
    return box


def _label(table: dict) -> str:
    """Privacy label of a [conditions.*] table; missing/unknown → private."""
    label = str(table.get("privacy") or "private").strip().lower()
    return label if label in privacy.LEVELS else "private"


def _live_ttl(table: dict) -> int:
    """Seconds between live() calls: table's live_ttl, default 90, min 30."""
    value = table.get("live_ttl")
    if value is None:
        return DEFAULT_LIVE_TTL
    try:
        return max(MIN_LIVE_TTL, int(value))
    except (TypeError, ValueError):
        return DEFAULT_LIVE_TTL


def _age(new: datetime, old: datetime) -> float:
    """Seconds from old to new; a naive/aware mix counts as expired."""
    try:
        return (new - old).total_seconds()
    except TypeError:
        return float("inf")


def collect(cfg: dict, *, max_privacy: str, now: datetime | None = None) -> list[dict]:
    """Boxes for this reader's [conditions.*] tables, in config order, filtered
    by privacy. Every failure is reported on stderr and skipped. A provider
    with a live method gets ``"live": True`` on its box, so the renderer knows
    which boxes to wire for live refresh."""
    tables = cfg.get("conditions") or {}
    if not isinstance(tables, dict) or not tables:
        return []
    now = now or datetime.now().astimezone()
    known = providers()
    out: list[dict] = []
    for name, table in tables.items():
        if not isinstance(table, dict):
            continue
        label = _label(table)
        if not privacy.allowed(label, max_privacy):
            continue
        spec = known.get(str(table.get("provider") or name))
        if spec is None:
            print(f"conditions: no provider for {name!r} (entry point group {ENTRY_POINT_GROUP})",
                  file=sys.stderr)
            continue
        provider = None
        try:
            provider = _load(spec)(dict(table))
            raw = provider.box(now)
        except Exception as e:  # noqa: BLE001 — a number never blocks the paper
            print(f"conditions: {name!r} skipped ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        box = clean_box(raw, name=str(name), label=label)
        if box is not None:
            if hasattr(provider, "live"):
                box["live"] = True
            out.append(box)
    return out


class LiveConditions:
    """The served page's live layer. One provider instance per (reader, box
    name) for the server process lifetime, a lock, a per-box TTL cache of the
    cleaned box (live_ttl from the table) and a negative cache after a failure
    or None — a broken source can't burn API quota. Providers without live()
    are snapshot-only and never appear here. The privacy filtering is the same
    as collect()'s (fail closed), and clean_box() is reused, so a live box is
    the same shape as a captured one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: dict[tuple[str, str], object] = {}
        self._cache: dict[tuple[str, str], tuple[dict | None, datetime, bool]] = {}
        self._snapshot_only: set[tuple[str, str]] = set()
        self._no_spec: set[tuple[str, str]] = set()

    def live_boxes(self, reader: str, cfg: dict, *, max_privacy: str,
                   now: datetime | None = None) -> list[dict]:
        """Live boxes for this reader's [conditions.*] tables, in config
        order: cleaned, privacy-filtered, cached per box. Never raises — a
        failing provider is skipped and negative-cached."""
        tables = cfg.get("conditions") or {}
        if not isinstance(tables, dict) or not tables:
            return []
        now = now or datetime.now().astimezone()
        known = providers()
        out: list[dict] = []
        for name, table in tables.items():
            if not isinstance(table, dict):
                continue
            label = _label(table)
            if not privacy.allowed(label, max_privacy):
                continue
            key = (str(reader), str(name))
            if key in self._snapshot_only or key in self._no_spec:
                continue
            ttl = _live_ttl(table)
            with self._lock:
                cached = self._cache.get(key)
                if cached is not None:
                    box, at, ok = cached
                    if _age(now, at) < (ttl if ok else max(ttl, NEG_LIVE_TTL)):
                        if ok and box is not None:
                            out.append(box)
                        continue
                provider = self._providers.get(key)
                if provider is None:
                    spec = known.get(str(table.get("provider") or name))
                    if spec is None:
                        self._no_spec.add(key)
                        print(f"conditions: no provider for {name!r} "
                              f"(entry point group {ENTRY_POINT_GROUP})", file=sys.stderr)
                        continue
                    try:
                        provider = _load(spec)(dict(table))
                    except Exception as e:  # noqa: BLE001
                        print(f"conditions: live {name!r} skipped "
                              f"({type(e).__name__}: {e})", file=sys.stderr)
                        self._cache[key] = (None, now, False)
                        continue
                    if not hasattr(provider, "live"):
                        self._snapshot_only.add(key)
                        continue
                    self._providers[key] = provider
            # the live contract promises cheap + thread-safe: call it outside
            # the lock, so one slow source never blocks the other boxes
            live = getattr(provider, "live", None)
            try:
                raw = live(now) if callable(live) else None
            except Exception as e:  # noqa: BLE001 — a number never breaks the page
                print(f"conditions: live {name!r} skipped ({type(e).__name__}: {e})",
                      file=sys.stderr)
                raw = None
            box = clean_box(raw, name=str(name), label=label) if raw is not None else None
            with self._lock:
                self._cache[key] = (box, now, box is not None)
            if box is not None:
                out.append(box)
        return out
