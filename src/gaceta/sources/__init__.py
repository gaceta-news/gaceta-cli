"""Source adapters and the plugin registry.

Built-in adapters: ``rss`` (sources/rss.py), ``nostr_wot`` (sources/nostr_wot.py)
and ``sqlite`` (sources/sqlite.py). External packages can register more via the
entry-point group ``gaceta.sources``::

    [project.entry-points."gaceta.sources"]
    mysource = "my_package.sources:MyAdapter"

The entry point must resolve to a class implementing the SourceAdapter contract
of sources/base.py (attribute ``name``; ``items(since=None)`` yielding RawItem;
constructor taking the adapter's ``[sources.<name>]`` config table as a dict).
``gaceta ingest <name>`` and ``gaceta ingest all`` dispatch to built-ins and
entry points alike.
"""
from __future__ import annotations

from importlib.metadata import entry_points

ENTRY_POINT_GROUP = "gaceta.sources"

# Adapters constructible from just their config table. rss and nostr_wot need
# richer wiring (transcripts, readiness gating) and are special-cased in the CLI.
_TABLE_ADAPTERS = {
    "sqlite": "gaceta.sources.sqlite:SqliteAdapter",
}


def entry_point_adapters() -> dict[str, str]:
    """name -> entry point value string, from the gaceta.sources group."""
    out: dict[str, str] = {}
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover - py<3.10 selective interface
        eps = entry_points().get(ENTRY_POINT_GROUP, [])
    for ep in eps:
        out.setdefault(ep.name, ep.value)
    return out


def _load(spec: str):
    module_name, _, attr = spec.partition(":")
    import importlib

    return getattr(importlib.import_module(module_name), attr)


def adapter_classes() -> dict[str, str]:
    """Every adapter class usable via `gaceta ingest <name>`: table-built-in
    adapters plus entry-point plugins (plugins win on name clashes)."""
    out = dict(_TABLE_ADAPTERS)
    out.update(entry_point_adapters())
    return out


def available_adapters() -> list[str]:
    """All dispatchable adapter names, including the CLI-wired built-ins."""
    # Order matters for `ingest all`: plugins first, then rss, nostr_wot LAST so a
    # note another adapter already ingested keeps that adapter's metadata/origin.
    return [*sorted(adapter_classes()), "rss", "nostr_wot"]


def build(name: str, table: dict):
    """Instantiate a table-adapter or entry-point plugin by name.

    Raises KeyError for unknown names (rss/nostr_wot are CLI-wired, not
    table-adapter constructible — see cli.cmd_ingest).
    """
    spec = adapter_classes().get(name)
    if spec is None:
        raise KeyError(name)
    return _load(spec)(table or {})


def discovery_adapters(cfg: dict) -> list:
    """Instantiated adapters from the reader's [sources.*] config that expose
    the optional ``discovery_candidates`` hook (discovery.suggestions)."""
    out = []
    for name, table in (cfg.get("sources") or {}).items():
        if name in ("rss", "nostr_wot") or not isinstance(table, dict):
            continue
        try:
            adapter = build(name, table)
        except Exception:  # noqa: BLE001 — a broken plugin never breaks the edition
            continue
        if getattr(adapter, "discovery_candidates", None) is not None:
            out.append(adapter)
    return out
