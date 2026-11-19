"""Source adapter registry: built-ins, entry-point plugins, dispatch helpers."""
from __future__ import annotations

import importlib.metadata
from importlib.metadata import EntryPoint

import pytest

from gaceta import sources as registry
from gaceta.sources.sqlite import SqliteAdapter


def test_builtin_names_available():
    names = registry.available_adapters()
    assert "rss" in names and "nostr_wot" in names and "sqlite" in names


def test_build_table_builtin():
    adapter = registry.build("sqlite", {"path": "/tmp/nowhere"})
    assert isinstance(adapter, SqliteAdapter)
    assert adapter.name == "sqlite"
    assert adapter.db_path.name == "items.db"


def test_build_unknown_raises():
    with pytest.raises(KeyError):
        registry.build("no-such-adapter", {})


def test_entry_point_adapter_registration(monkeypatch, tmp_path):
    """A package registering a `gaceta.sources` entry point is dispatched like
    a built-in: by name, constructed with its [sources.<name>] config table."""
    # the plugin class (module-level so the entry-point value can import it)
    plugin_src = (
        "from gaceta.sources.base import RawItem, RawSource\n"
        "class DummyAdapter:\n"
        "    name = 'dummy'\n"
        "    def __init__(self, cfg):\n"
        "        self.cfg = cfg\n"
        "    def items(self, since=None):\n"
        "        return iter([RawItem(id='d1', source=RawSource(id='s', kind='other'),"
        " author=None, title='t', url=None, published=None, body='b')])\n"
    )
    import sys

    pkg = tmp_path / "gaceta_dummy_plugin"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(plugin_src, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    ep = EntryPoint(name="dummy", value="gaceta_dummy_plugin:DummyAdapter",
                    group="gaceta.sources")
    monkeypatch.setattr(registry, "entry_points",
                        lambda group=None: [ep] if group == "gaceta.sources" else [])

    # visible in the registry, in available_adapters, and dispatchable by name
    assert registry.adapter_classes().get("dummy") == "gaceta_dummy_plugin:DummyAdapter"
    assert "dummy" in registry.available_adapters()
    adapter = registry.build("dummy", {"anything": 1})
    assert type(adapter).__name__ == "DummyAdapter"
    assert adapter.cfg == {"anything": 1}
    assert [i.id for i in adapter.items()] == ["d1"]

    # entry points win the name; built-ins keep theirs
    clash = EntryPoint(name="sqlite", value="gaceta_dummy_plugin:DummyAdapter",
                       group="gaceta.sources")
    monkeypatch.setattr(registry, "entry_points",
                        lambda group=None: [clash] if group == "gaceta.sources" else [])
    assert isinstance(registry.build("sqlite", {}), registry._load("gaceta_dummy_plugin:DummyAdapter"))


def test_own_distribution_registers_sqlite_entry_point():
    """gaceta itself ships one plugin via pyproject [project.entry-points.'gaceta.sources']."""
    try:
        dist = importlib.metadata.distribution("gaceta")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("gaceta distribution not installed in this environment")
    eps = [ep for ep in dist.entry_points if ep.group == "gaceta.sources"]
    assert any(ep.name == "sqlite" and ep.value == "gaceta.sources.sqlite:SqliteAdapter"
               for ep in eps), eps
    assert "sqlite" in registry.entry_point_adapters()


def test_discovery_adapters_only_hooked_plugins(env):
    """[sources.*] tables resolving to adapters with discovery_candidates are
    instantiated; broken or hook-less sources are skipped."""
    cfg = dict(env.cfg)
    cfg["sources"] = {
        "sqlite": {"path": str(env.src_root)},          # has the hook
        "rss": {"feeds": []},                            # CLI-wired, skipped here
        "bogus": {"x": 1},                               # unresolvable, skipped
    }
    adapters = registry.discovery_adapters(cfg)
    assert [type(a).__name__ for a in adapters] == ["SqliteAdapter"]
    assert adapters[0].discovery_candidates(limit=2) == []  # no kv candidates seeded


def test_nostr_wot_runs_last_in_ingest_all():
    # WoT must not claim notes another adapter ingests (origin/metadata stay theirs)
    names = registry.available_adapters()
    assert names[-1] == "nostr_wot" and names.index("rss") < names.index("nostr_wot")
    assert names.index("sqlite") < names.index("rss")
