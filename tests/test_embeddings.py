"""Embeddings: additive v1→v2 migration, incrementality, text-source contract, factory."""
from __future__ import annotations

import pytest

from gaceta import db as gdb
from gaceta.editor.cluster import embed_items, embed_text, text_hash
from gaceta.providers.embed import (FakeEmbedder, OpenAIEmbedder, OllamaEmbedder,
                                  from_config)
from gaceta.sources.sqlite import SqliteAdapter
from gaceta import ingest


def test_migration_v1_to_v3_additive(tmp_path):
    """A db stamped v1 (no embeddings/llm_decisions tables) migrates to v3 additively,
    data intact."""
    path = tmp_path / "gaceta.db"
    conn = gdb.connect(path)
    gdb.init(conn)
    conn.execute("INSERT INTO readers (id, name, langs, created_at) VALUES ('r', 'R', 'en', 0)")
    conn.execute("INSERT INTO items (id, ingested_at, origin) VALUES ('x', 0, 'test')")
    conn.commit()
    # regress to v1: drop the newer tables and stamp 1
    conn.execute("DROP TABLE embeddings")
    conn.execute("DROP TABLE llm_decisions")
    conn.execute("UPDATE meta SET v = '1' WHERE k = 'schema_version'")
    conn.commit()
    conn.close()

    conn = gdb.connect(path)
    gdb.init(conn)  # the migration under test
    version = conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()[0]
    assert version == str(__import__("gaceta.db").db.SCHEMA_VERSION)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t in ("items", "annotations", "stories", "story_items", "story_versions",
              "editions", "reactions", "told", "ingest_runs", "embeddings", "llm_decisions"):
        assert t in tables, t
    assert conn.execute("SELECT COUNT(*) FROM readers").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    conn.close()


def test_migration_stamp_never_downgrades(tmp_path):
    path = tmp_path / "gaceta.db"
    conn = gdb.connect(path)
    gdb.init(conn)
    conn.execute("UPDATE meta SET v = '99' WHERE k = 'schema_version'")
    conn.commit()
    gdb.init(conn)
    assert conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()[0] == "99"
    conn.close()


def _noted_told(env):
    return [
        r[0]
        for r in env.conn.execute(
            "SELECT item_id FROM annotations WHERE reader_id = ? AND verdict IN ('noted','told')",
            (env.cfg["id"],),
        )
    ]


def test_embed_incremental_second_run_embeds_zero(env):
    ingest.ingest(env.conn, SqliteAdapter(env.src_root), env.cfg)

    calls = []

    class Counting(FakeEmbedder):
        def embed(self, texts):
            calls.append(len(texts))
            return super().embed(texts)

    r1 = embed_items(env.conn, env.cfg["id"], Counting())
    assert r1["embedded"] == len(_noted_told(env)) > 0
    assert r1["model"] == "fake:256"

    r2 = embed_items(env.conn, env.cfg["id"], Counting())
    assert r2["embedded"] == 0
    assert calls == [r1["embedded"]]  # second run made no embed calls at all


def test_embed_covers_only_noted_and_told(env):
    ingest.ingest(env.conn, SqliteAdapter(env.src_root), env.cfg)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    got = {r[0] for r in env.conn.execute("SELECT item_id FROM embeddings")}
    assert got == set(_noted_told(env))


def test_embed_text_is_source_content_only_never_note(env):
    """text_hash must be sha256(title + '\\n' + body[:1500]); the annotation note is
    never embedded, even when it carries a canary the content lacks."""
    ingest.ingest(env.conn, SqliteAdapter(env.src_root), env.cfg)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    rows = env.conn.execute(
        "SELECT i.id, i.title, i.body, e.text_hash FROM items i "
        "JOIN embeddings e ON e.item_id = i.id"
    ).fetchall()
    assert rows
    for r in rows:
        assert r["text_hash"] == text_hash(embed_text(r["title"], r["body"]))
    # the note-canary item: note contains 'zorblax', content does not
    canary = env.info["note_canary_ids"][0]
    note = env.annotation(canary)["note"]
    assert "zorblax" in note.lower()
    row = env.conn.execute(
        "SELECT text_hash FROM embeddings WHERE item_id = ?", (canary,)
    ).fetchone()
    assert row["text_hash"] != text_hash(note)  # note was not what got embedded


def test_embed_text_truncates_body_at_1500(env):
    assert len(embed_text("t", "x" * 2000)) == len("t") + 1 + 1500
    assert embed_text("t", None) == "t\n"


def test_embed_dim_stored(env):
    ingest.ingest(env.conn, SqliteAdapter(env.src_root), env.cfg)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    r = env.conn.execute("SELECT dim, LENGTH(vec) FROM embeddings LIMIT 1").fetchone()
    assert r["dim"] == 256 and r[1] == 256 * 4  # float32


def test_from_config_factory():
    fake = from_config({"embed": {"provider": "fake"}})
    assert isinstance(fake, FakeEmbedder)
    ol = from_config({"embed": {"provider": "ollama"}})
    assert isinstance(ol, OllamaEmbedder)
    assert ol.base_url == "http://localhost:11434" and ol.model == "bge-m3"
    ol2 = from_config({"embed": {"provider": "ollama", "base_url": "http://gpu:9000",
                                 "model": "nomic"}})
    assert ol2.base_url == "http://gpu:9000" and ol2.model == "nomic"
    oa = from_config({"embed": {"provider": "openai", "base_url": "https://api.example.com/v1",
                                "model": "text-embedding-3-small", "api_key_env": "MY_KEY"}})
    assert isinstance(oa, OpenAIEmbedder)
    assert oa.base_url == "https://api.example.com/v1" and oa.model == "text-embedding-3-small"
    assert oa.api_key_env == "MY_KEY" and oa.name == "openai:text-embedding-3-small"
    assert oa.batch == 64  # one POST for small batches
    # no silent default: missing/unknown provider is an error, never a fake
    with pytest.raises(ValueError):
        from_config({})
    with pytest.raises(ValueError):
        from_config({"embed": {"provider": "nope"}})
    with pytest.raises(ValueError):  # openai needs base_url + model
        from_config({"embed": {"provider": "openai"}})


# --------------------------------------------------------------- openai embedder


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        import json as _json
        return _json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_openai_embedder_posts_and_reorders(monkeypatch):
    """POST {base_url}/embeddings with bearer key from the named env var;
    embeddings are re-assembled by index even if the API shuffles them."""
    import json as _json

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = _json.loads(req.data.decode())
        return _StubResponse({"data": [
            {"embedding": [0.3], "index": 1},
            {"embedding": [0.1], "index": 0},
        ]})

    monkeypatch.setattr("gaceta.providers.embed.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("GACETA_TEST_KEY", "secret-1")
    e = OpenAIEmbedder(base_url="https://api.example.com/v1/", api_key_env="GACETA_TEST_KEY",
                       model="text-embedding-3-small")
    assert e.embed(["one", "two"]) == [[0.1], [0.3]]
    assert seen["url"] == "https://api.example.com/v1/embeddings"
    assert seen["auth"] == "Bearer secret-1"
    assert seen["body"] == {"model": "text-embedding-3-small", "input": ["one", "two"]}


def test_openai_embedder_batches(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        import json as _json
        body = _json.loads(req.data.decode())
        calls.append(len(body["input"]))
        return _StubResponse({"data": [
            {"embedding": [0.0, float(i)], "index": i} for i, _ in enumerate(body["input"])
        ]})

    monkeypatch.setattr("gaceta.providers.embed.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("GACETA_TEST_KEY", "k")
    e = OpenAIEmbedder(base_url="https://api.example.com", api_key_env="GACETA_TEST_KEY",
                       model="m", batch=2)
    out = e.embed(["a", "b", "c", "d", "e"])
    assert calls == [2, 2, 1]
    assert [v[1] for v in out] == [0.0, 1.0, 0.0, 1.0, 0.0]


def test_openai_embedder_missing_key_names_env_not_value(monkeypatch):
    monkeypatch.delenv("GACETA_TEST_KEY", raising=False)
    e = OpenAIEmbedder(base_url="https://api.example.com", api_key_env="GACETA_TEST_KEY",
                       model="m")
    with pytest.raises(RuntimeError, match="GACETA_TEST_KEY"):
        e.embed(["x"])


def test_openai_embedder_empty_input_is_no_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no HTTP call expected")

    monkeypatch.setattr("gaceta.providers.embed.urllib.request.urlopen", boom)
    assert OpenAIEmbedder("https://x", "K", "m").embed([]) == []
