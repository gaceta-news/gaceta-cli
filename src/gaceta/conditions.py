"""Condiciones: small boxes of moving numbers (weather, a price, a level).

A standing desk, not a story: a box stamped "a las HH:MM, no en vivo" with the
source named, printed after the portada, never in the headline area. Values are
captured once, when the edition is built, and printed as a snapshot.

Providers are plugins registered under the entry-point group ``gaceta.conditions``::

    [project.entry-points."gaceta.conditions"]
    weather = "my_package.weather:WeatherBox"

and enabled per reader in reader.toml::

    [conditions.weather]
    privacy = "household"   # optional; default "private" (fail closed)
    # ...any other keys are the provider's own config

The entry point resolves to a class constructed with that table (a dict) that
implements ``box(now: datetime) -> dict | None``. The box contract (all strings
plain text; the renderer escapes everything and never trusts HTML):

    {"title": "El tiempo",              # required
     "lead": "10 °C · niebla",          # optional big line
     "facts": [["Lluvia 24 h", "0 mm"], ...],   # optional label/value rows
     "source": "estación propia",       # optional, printed with the stamp
     "observed_at": "<ISO datetime>"}   # optional; stamp falls back to build time

A provider that fails, returns None or returns junk is skipped: the edition is
never blocked on a number, and a stale box is worse than no box.
"""
from __future__ import annotations

import sys
from datetime import datetime
from importlib.metadata import entry_points

from . import privacy

ENTRY_POINT_GROUP = "gaceta.conditions"
MAX_FACTS = 12
MAX_TEXT = 120


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
        if isinstance(row, (list, tuple)) and len(row) == 2:
            k, v = _text(row[0], 40), _text(row[1], 60)
            if k and v:
                facts.append([k, v])
    if facts:
        box["facts"] = facts[:MAX_FACTS]
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


def collect(cfg: dict, *, max_privacy: str, now: datetime | None = None) -> list[dict]:
    """Boxes for this reader's [conditions.*] tables, in config order, filtered
    by privacy. Every failure is reported on stderr and skipped."""
    tables = cfg.get("conditions") or {}
    if not isinstance(tables, dict) or not tables:
        return []
    now = now or datetime.now().astimezone()
    known = providers()
    out: list[dict] = []
    for name, table in tables.items():
        if not isinstance(table, dict):
            continue
        label = str(table.get("privacy") or "private").strip().lower()
        if label not in privacy.LEVELS:
            label = "private"
        if not privacy.allowed(label, max_privacy):
            continue
        spec = known.get(str(table.get("provider") or name))
        if spec is None:
            print(f"conditions: no provider for {name!r} (entry point group {ENTRY_POINT_GROUP})",
                  file=sys.stderr)
            continue
        try:
            raw = _load(spec)(dict(table)).box(now)
        except Exception as e:  # noqa: BLE001 — a number never blocks the paper
            print(f"conditions: {name!r} skipped ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        box = clean_box(raw, name=str(name), label=label)
        if box is not None:
            out.append(box)
    return out
