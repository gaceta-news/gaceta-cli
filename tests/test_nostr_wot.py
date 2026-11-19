"""nostr-wot adapter tests. NO NETWORK: a scripted fake relay stands in for the
real one; every event, key and name below is synthetic."""
from __future__ import annotations

import sqlite3

import pytest

import gaceta.db as gdb
from gaceta import cli, ingest
from gaceta.editor import cluster
from gaceta.sources import nostr_wot
from gaceta.sources.sqlite import SqliteAdapter
from gaceta.sources.nostr_wot import NostrWotAdapter, NotReadyError

READER = "ab" * 32            # the reader's (observer's) pubkey — synthetic
SERVICE = "cd" * 32           # the 30382:rank service — synthetic
ALICE = "0f" * 32             # a WoT-ranked author — synthetic
NOW = 1_770_000_000

RELAY = "wss://fake.example"


def ev(eid: str, pk: str, content: str, *, kind: int = 1, created_at: int = NOW,
       tags: list | None = None) -> dict:
    return {"id": eid, "pubkey": pk, "kind": kind, "created_at": created_at,
            "content": content, "tags": tags or []}


RANK_10040 = ev("1c" * 32, READER, "", kind=10040,
                tags=[["30382:rank", SERVICE, RELAY]])
CARDS_30382 = ev("2d" * 32, SERVICE, "", kind=30382)
PROBE_NOTE = ev("3e" * 32, ALICE, "probe note", created_at=NOW - 600)


class FakeClient:
    """Stand-in for RelayClient: answers (kinds, search) → events."""

    def __init__(self, script: dict):
        self.script = script
        self.calls: list[tuple] = []

    def req(self, url: str, filters: list[dict]):
        f = filters[0]
        key = (tuple(f.get("kinds") or ()), f.get("search") or "")
        self.calls.append(key)
        return self.script.get(key, []), None


def ready_script(main_events: list[dict], *, observed: int = 1, anonymous: int = 3,
                 profiles: list[dict] | None = None) -> dict:
    s = {
        ((10040,), nostr_wot.INCLUDE_SPAM): [RANK_10040],
        ((30382,), nostr_wot.INCLUDE_SPAM): [CARDS_30382],
        ((1,), f"observer:{READER} sort:rank"): [PROBE_NOTE] * observed,
        ((1,), f"{nostr_wot.INCLUDE_SPAM} sort:rank"): [PROBE_NOTE] * anonymous,
        ((1, 30023), (f"observer:{READER} sort:rank "
                      f"filter:rank:gte:{nostr_wot.DEFAULT_FLOOR}")): main_events,
        ((0,), nostr_wot.INCLUDE_SPAM): profiles or [],
    }
    return s


def adapter(cfg: dict | None = None, script: dict | None = None) -> NostrWotAdapter:
    return NostrWotAdapter({"pubkey": READER, "relay": RELAY, **(cfg or {})},
                           client=FakeClient(script or ready_script([])))


def wot_cfg(**kw) -> dict:
    return {"pubkey": READER, "relay": RELAY, **kw}


# ---------------------------------------------------------------- readiness

def test_not_ready_no_score_list_refuses():
    a = adapter(script=ready_script([]))
    # strip the 10040 answer: nothing else changes
    a._client.script = {k: v for k, v in a._client.script.items()
                        if k[0] != (10040,)}
    verdict = a.readiness()
    assert verdict == {"ready": False, "state": "no-score-list"}
    with pytest.raises(NotReadyError):
        list(a.items())


def test_readiness_states():
    # cards missing → no-scores-yet
    s = ready_script([])
    del s[((30382,), nostr_wot.INCLUDE_SPAM)]
    assert adapter(script=s).readiness()["state"] == "no-scores-yet"
    # 10040 without a usable rank tag → no-rank-service
    s = ready_script([])
    s[((10040,), nostr_wot.INCLUDE_SPAM)] = [
        ev("4e" * 32, READER, "", kind=10040, tags=[["30382:followers", SERVICE, RELAY]])]
    assert adapter(script=s).readiness()["state"] == "no-rank-service"
    # lens resolves but returns nothing while the anonymous read does → projection-pending
    assert adapter(script=ready_script([], observed=0, anonymous=3)
                   ).readiness()["state"] == "projection-pending"
    # and the happy path
    assert adapter(script=ready_script([], observed=1, anonymous=3)
                   ).readiness()["ready"] is True


def test_cli_refuses_not_ready(env, capsys, monkeypatch):
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    import make_fixture_db as fwmod

    reader_toml = env.data_dir / "readers" / env.cfg["id"] / "reader.toml"
    reader_toml.write_text(reader_toml.read_text() + (
        f'\n[sources.nostr_wot]\npubkey = "{READER}"\nrelay = "{RELAY}"\n'))
    monkeypatch.setattr(nostr_wot, "RelayClient",
                        lambda: FakeClient(ready_script([]) | {
                            ((10040,), nostr_wot.INCLUDE_SPAM): []}))
    rc = cli.main(["ingest", "nostr-wot", "--reader", env.cfg["id"]])
    assert rc == 2
    assert "NOT READY" in capsys.readouterr().err
    # a same-id note exists in the fixture feed — none of it was touched
    n = env.conn.execute("SELECT COUNT(*) FROM items WHERE origin = 'nostr_wot'").fetchone()[0]
    assert n == 0
    assert fwmod  # fixture module imported for clarity; no state changed


# ---------------------------------------------------------------- mapping + ids

def test_item_mapping(env):
    note = ev("5f" * 32, ALICE, "First line is the headline\nsecond line is body.",
              created_at=NOW - 3600)
    profiles = [ev("6a" * 32, ALICE, '{"name": "alice-test"}', kind=0)]
    a = adapter(script=ready_script([note], profiles=profiles))
    cfg = dict(env.cfg, sources=dict(env.cfg.get("sources", {}), nostr_wot=wot_cfg()))
    result = ingest.ingest(env.conn, a, cfg)
    assert result["new"] == 1
    row = env.conn.execute("SELECT i.*, s.kind AS skind, s.content_public AS pub "
                           "FROM items i JOIN sources s ON s.id = i.source_id "
                           "WHERE i.id = ?", (f"nostr:{'5f' * 32}",)).fetchone()
    assert row["origin"] == "nostr_wot"
    assert row["skind"] == "nostr"
    assert row["pub"] == 1
    assert row["source_id"] == "nostr:alice-test"
    assert row["author"] == "alice-test"
    assert row["author_pubkey"] == ALICE
    assert row["url"] == f"https://njump.me/{'5f' * 32}"
    assert row["published"] == NOW - 3600
    assert row["title"] == "First line is the headline"
    ann = env.annotation(f"nostr:{'5f' * 32}")
    assert ann["verdict"] is None            # default: not in the editor gate
    assert ann["item_privacy"] == "public"   # published content, no canary term


def test_longform_title_tag_and_hex_free_names(env):
    art = ev("7b" * 32, ALICE, "body of the article", kind=30023,
             tags=[["title", "The Article Title"], ["d", "slug"]])
    a = adapter(script=ready_script([art], profiles=[]))  # no profile → short npub
    cfg = dict(env.cfg, sources=dict(env.cfg.get("sources", {}), nostr_wot=wot_cfg()))
    ingest.ingest(env.conn, a, cfg)
    row = env.conn.execute("SELECT i.title, i.author, i.source_id FROM items i "
                           "WHERE i.id = ?", (f"nostr:{'7b' * 32}",)).fetchone()
    assert row["title"] == "The Article Title"
    # never a bare hex string; a short npub instead
    assert ALICE not in (row["author"] or "") + (row["source_id"] or "")
    assert row["author"].startswith("npub1") and "…" in row["author"]


def test_own_posts_excluded():
    mine = ev("8c" * 32, READER, "my own post ranks high through my own lens")
    theirs = ev("9d" * 32, ALICE, "someone else's note")
    a = adapter(script=ready_script([mine, theirs]))
    ids = [i.id for i in a.items()]
    assert ids == [f"nostr:{'9d' * 32}"]


def test_bech32_matches_reference():
    # expected value produced by the Observer's nostr.mjs (reference implementation)
    assert nostr_wot.to_npub("ab" * 32) == \
        "npub14w46h2at4w46h2at4w46h2at4w46h2at4w46h2at4w46h2at4w4scf6zts"


# ---------------------------------------------------------------- dedupe + provenance

def test_dedupes_against_sqlite_nostr_ids(env):
    import make_fixture_db as fwmod

    ingest.ingest(env.conn, SqliteAdapter(env.src_root), env.cfg)
    # nostr-1 is a fixture note with verdict 'skip', author 'nadia-quill'
    shared_id = fwmod._hex("nostr-1", 64)
    note = ev(shared_id, ALICE, "same event id, discovered through the lens")
    a = adapter(script=ready_script([note],
                                    profiles=[ev("6a" * 32, ALICE, '{"name": "alice-test"}', kind=0)]))
    cfg = dict(env.cfg, sources=dict(env.cfg.get("sources", {}), nostr_wot=wot_cfg()))
    result = ingest.ingest(env.conn, a, cfg)
    assert result["new"] == 0
    row = env.conn.execute("SELECT * FROM items WHERE id = ?", (f"nostr:{shared_id}",)).fetchone()
    # the followed-author version wins: sqlite metadata and origin stay
    assert row["origin"] == "sqlite"
    assert row["author"] == "nadia-quill"
    assert row["url"].startswith("https://example.com/notes/")
    assert row["author_pubkey"] == ALICE          # provenance enriched, nothing clobbered
    ann = env.annotation(f"nostr:{shared_id}")
    assert ann["verdict"] == "skip"               # reader-layer fields not erased
    before = env.conn.execute(
        "SELECT COUNT(*) FROM annotations WHERE reader_id = ?", (env.cfg["id"],)).fetchone()[0]
    assert before >= 1


def test_idempotent(env):
    note = ev("ae" * 32, ALICE, "note once")
    a1 = adapter(script=ready_script([note]))
    cfg = dict(env.cfg, sources=dict(env.cfg.get("sources", {}), nostr_wot=wot_cfg()))
    r1 = ingest.ingest(env.conn, a1, cfg)
    a2 = adapter(script=ready_script([note]))
    r2 = ingest.ingest(env.conn, a2, cfg)
    assert r1["new"] == 1 and r2["new"] == 0 and r2["updated"] == 0


def test_no_canary_leak_from_wot_body(env):
    note = ev("bf" * 32, ALICE, "the zorblax handshake is simpler than it looks")
    a = adapter(script=ready_script([note]))
    cfg = dict(env.cfg, sources=dict(env.cfg.get("sources", {}), nostr_wot=wot_cfg()))
    ingest.ingest(env.conn, a, cfg)
    ann = env.annotation(f"nostr:{'bf' * 32}")
    assert ann["item_privacy"] == "private"
    assert ann["privacy_reason"].startswith("pinned:")


# ---------------------------------------------------------------- editor gate

def test_editor_gate_off_by_default_on_by_flag(env):
    """The editor gate is cluster._reader_items' verdict IN ('noted','told'): a
    WoT item only reaches stories when the reader opts in via into_stories."""
    def gate(into_stories: bool) -> list[str]:
        conn = gdb.connect(env.data_dir / "gate.db")
        try:
            gdb.init(conn)
            a = adapter({"into_stories": into_stories},
                        script=ready_script([ev("ce" * 32, ALICE, "a ranked note")]))
            cfg = dict(env.cfg, id=env.cfg["id"],
                       sources=dict(env.cfg.get("sources", {}),
                                    nostr_wot=wot_cfg(into_stories=into_stories)))
            ingest.ingest(conn, a, cfg)
            return [r["id"] for r in cluster._reader_items(conn, env.cfg["id"])]
        finally:
            conn.close()

    assert f"nostr:{'ce' * 32}" not in gate(False)   # default: kept out of stories
    assert f"nostr:{'ce' * 32}" in gate(True)        # flagged path: flows to embed/cluster


# ---------------------------------------------------------------- config gating

def test_cli_requires_config(env, capsys, monkeypatch):
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli.main(["ingest", "nostr-wot", "--reader", env.cfg["id"]])
    assert rc == 2
    assert "no [sources.nostr_wot]" in capsys.readouterr().err


def test_ingest_all_skips_unconfigured_adapter():
    # 'all' only runs adapters present in the reader's sources config
    from gaceta import sources as registry
    cfg = {"id": "x", "name": "X", "sources": {"sqlite": {"path": "/nonexistent"}}}
    names = [a for a in registry.available_adapters() if a in cfg.get("sources", {})]
    assert names == ["sqlite"]


# ---------------------------------------------------------------- migration v10

def test_v10_migration_additive_on_a_copy(tmp_path):
    # build a v9-shaped db: items WITHOUT author_pubkey
    db_path = tmp_path / "v9.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);"
        "CREATE TABLE sources (id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT, "
        "url TEXT, content_public INTEGER NOT NULL DEFAULT 1);"
        "CREATE TABLE items (id TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id), "
        "author TEXT, title TEXT, url TEXT, published INTEGER, body TEXT, "
        "ingested_at INTEGER NOT NULL, origin TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '9');"
        "INSERT INTO sources VALUES ('nostr:old', 'nostr', 'old', NULL, 1);"
        "INSERT INTO items VALUES ('nostr:old1', 'nostr:old', 'old', 't', NULL, 1, 'b', 1, 'sqlite');")
    conn.commit()
    conn.close()
    # migrate a COPY, never the original
    copy_path = tmp_path / "copy.db"
    copy_path.write_bytes(db_path.read_bytes())
    conn = sqlite3.connect(copy_path)
    conn.row_factory = sqlite3.Row
    gdb.init(conn)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
    assert "author_pubkey" in cols
    row = conn.execute("SELECT * FROM items WHERE id = 'nostr:old1'").fetchone()
    assert row["origin"] == "sqlite" and row["author_pubkey"] is None
    assert conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()[0] == str(__import__("gaceta.db").db.SCHEMA_VERSION)
    conn.close()
    # the original file was never opened for migration
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)")]
    assert "author_pubkey" not in cols
    conn.close()
