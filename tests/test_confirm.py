"""LLM-confirmed clustering: decision cache, call budget, anti-chaining, model
boundary (annotation note only for allow_private), additive v2→v3 migration.

Vectors are unit vectors at chosen angles: cosine(a, b) = cos(angle difference),
so e.g. uv(0) vs uv(52) is a borderline pair at similarity 0.616.
"""
from __future__ import annotations

import json
import math
import sqlite3
import urllib.error
import urllib.request

import numpy as np
import pytest

import gaceta.db as gdb
from gaceta.editor import cluster, prompts
from gaceta.editor.cluster import Confirmer, _desc_fn, partition
from gaceta.providers.llm import FakeChat, OpenAICompatChat, from_config
from gaceta.evals import clustering as ec
from gaceta.providers.embed import FakeEmbedder

from test_cluster import T0, DAY, add_item

CANARY = "zorblax-canary-note"


def uv(deg: float, dim: int = 4):
    v = [0.0] * dim
    v[0] = math.cos(math.radians(deg))
    v[1] = math.sin(math.radians(deg))
    return v


def _items(*vecs):
    return [(f"i{n}", T0 + n * DAY, np.asarray(v, dtype=np.float32), "public")
            for n, v in enumerate(vecs)]


def _confirmer(env, rules=None, **kw):
    chat = FakeChat(rules or {})
    kw.setdefault("confirm_low", 0.55)
    return chat, Confirmer(chat, env.conn, _desc_fn(env.conn, env.cfg["id"]), **kw)


# ------------------------------------------------------- confirm zone (LLM asked)

def test_confirm_zone_llm_yes_joins_and_no_splits(env):
    add_item(env, "i0", "A one", "alpha beta gamma", published=T0)
    add_item(env, "i1", "B one", "alpha beta gamma", published=T0 + DAY)
    add_item(env, "i2", "C three", "other words", published=T0 + 2 * DAY)
    add_item(env, "i3", "D four", "more words", published=T0 + 3 * DAY)
    # i1 (16° from i0, sim .96) auto-joins; i2 (47° from the {i0,i1} centroid,
    # sim .68) is borderline → LLM says no → own story; i3 (52° from i2, sim .62)
    # is borderline vs that story → LLM says yes → joins it.
    # NB rule order matters: i3's prompt also contains "C three" (it is the
    # origin+latest of story2), so the joiner's own title must be checked first.
    chat, conf = _confirmer(env, rules={"D four": True, "C three": False})
    out = partition(_items(uv(0), uv(16), uv(55), uv(107)), 0.70, 21, confirmer=conf)
    assert out == [["i0", "i1"], ["i2", "i3"]]
    assert conf.calls == 2 and conf.cache_hits == 0
    assert env.conn.execute("SELECT COUNT(*) FROM llm_decisions").fetchone()[0] == 2


def test_unparseable_llm_reply_means_no(env):
    add_item(env, "i0", "A one", "alpha beta gamma", published=T0)
    chat = FakeChat(rules=lambda p: True)

    def garbage(messages, **kw):
        return "the items are clearly the same story", {"prompt_tokens": 1, "completion_tokens": 1}

    chat.complete = garbage  # no JSON anywhere
    conf = Confirmer(chat, env.conn, _desc_fn(env.conn, env.cfg["id"]), confirm_low=0.55)
    out = partition(_items(uv(0), uv(52)), 0.70, 21, confirmer=conf)
    assert out == [["i0"], ["i1"]]  # unparseable → false → no join


def test_unparseable_reply_is_cached_not_repaid(env):
    """Step 0 (cluster-confirm): an unparseable confirm reply IS cached in
    llm_decisions — the same borderline pair is never re-requested next run."""
    add_item(env, "i0", "A one", "alpha beta gamma", published=T0)
    add_item(env, "i1", "B two", "other words", published=T0 + DAY)

    def garbage(messages, **kw):
        return "the items are clearly the same story", {"prompt_tokens": 1}

    chat = FakeChat()
    chat.complete = garbage
    conf = Confirmer(chat, env.conn, _desc_fn(env.conn, env.cfg["id"]), confirm_low=0.55)
    assert partition(_items(uv(0), uv(52)), 0.70, 21, confirmer=conf) == [["i0"], ["i1"]]
    assert conf.calls == 1 and conf.cache_hits == 0
    assert env.conn.execute("SELECT output FROM llm_decisions").fetchone()[0] == \
        "the items are clearly the same story"

    conf2 = Confirmer(FakeChat(), env.conn, _desc_fn(env.conn, env.cfg["id"]), confirm_low=0.55)
    assert partition(_items(uv(0), uv(52)), 0.70, 21, confirmer=conf2) == [["i0"], ["i1"]]
    assert conf2.calls == 0 and conf2.cache_hits == 1  # same key, cached reply, no call


# ------------------------------------------------------- cache + budget

def test_cache_prevents_second_call(env):
    add_item(env, "i0", "A one", "alpha beta gamma", published=T0)
    add_item(env, "i1", "B two", "other words", published=T0 + DAY)
    items = _items(uv(0), uv(52))  # sim .616: borderline
    _, conf1 = _confirmer(env, rules={"B two": True})
    assert partition(items, 0.70, 21, confirmer=conf1) == [["i0", "i1"]]
    assert conf1.calls == 1
    _, conf2 = _confirmer(env, rules={"B two": True})
    assert partition(items, 0.70, 21, confirmer=conf2) == [["i0", "i1"]]
    assert conf2.calls == 0 and conf2.cache_hits == 1  # rerun cost 0 calls


def test_budget_exhaustion_falls_back_to_embeddings_only(env):
    for iid, title in (("i0", "A one"), ("i1", "B one"), ("i2", "C two"), ("i3", "D two")):
        add_item(env, iid, title, "words", published=T0)
    chat, conf = _confirmer(env, rules={"C two": True}, max_llm_calls=1)
    e1, e3 = [0.0] * 4, [0.0] * 4
    e1[0] = e3[2] = 1.0
    c = uv(52)
    i2 = [c[0], c[1], 0.0, 0.0]   # 52° from i0's story, 0 from i1's
    i3 = [0.0, 0.0, c[0], c[1]]   # 52° from i1's story only
    out = partition(_items(e1, e3, i2, i3), 0.70, 21, confirmer=conf)
    # first borderline confirmed (call 1); second: budget gone → embeddings-only → no join
    assert out == [["i0", "i2"], ["i1"], ["i3"]]
    assert conf.calls == 1 and conf.budget_exhausted
    r = conf.report()
    assert r["budget_exhausted"] and r["llm_calls"] == 1


# ------------------------------------------------------- anti-chaining

def test_anti_chaining_blocks_chain_far_from_origin(env):
    # A~B join above threshold; C matches the {A,B} centroid above threshold but is
    # far from the ORIGIN A → blocked. Classic centroid-drift chain A~B~C.
    A = np.array([1.0, 0.0, 0.0])
    B = np.array([0.8, 0.6, 0.0])            # sim(A,B) = 0.80 ≥ 0.70 → joins
    C = np.array([0.5, 0.8660254, 0.0])      # sim(C, centroid) ≈ 0.748 ≥ 0.70, sim(C,A) = 0.50 < 0.55
    chat, conf = _confirmer(env, rules=lambda p: True)  # would say yes if asked
    out = partition(_items(A, B, C), 0.70, 21, confirmer=conf)
    assert out == [["i0", "i1"], ["i2"]]  # C blocked without even asking the LLM
    assert conf.calls == 0
    # without the confirmer (embeddings-only, phase-2a behaviour) the chain forms
    assert partition(_items(A, B, C), 0.70, 21) == [["i0", "i1", "i2"]]


# ------------------------------------------------------- model boundary

def test_note_canary_only_with_allow_private():
    item = {"title": "t", "body": "b" * 10, "author": "au", "source_kind": "hn",
            "published": T0, "note": CANARY}
    plain = {"title": "t2", "body": "b2" * 10, "author": "au2", "source_kind": "yt",
             "published": T0, "note": CANARY}
    public_msgs = prompts.confirm_messages(item, plain, plain, allow_private=False)
    blob = json.dumps(public_msgs)
    assert CANARY not in blob and "READER NOTE" not in blob
    private_msgs = prompts.confirm_messages(item, plain, plain, allow_private=True)
    assert CANARY in json.dumps(private_msgs)  # the owner's private lane gets the note
    # source content is always present
    assert "bbbbbbbbbb" in public_msgs[1]["content"] and "au2" in public_msgs[1]["content"]


def test_public_only_confirmer_withholds_nonpublic_source(env):
    add_item(env, "i0", "A one", "alpha beta gamma", published=T0)
    add_item(env, "i1", "B two", "sentinel", privacy="private", published=T0 + DAY)
    chat = FakeChat(rules=lambda _: True, allow_private=False)
    conf = Confirmer(chat, env.conn, _desc_fn(env.conn, env.cfg["id"]), confirm_low=0.55)
    assert not conf.ask("i1", "i0", "i0")
    assert not chat.prompts


def test_confirmer_respects_model_boundary(env):
    add_item(env, "i0", "A one", "alpha beta gamma", published=T0)
    add_item(env, "i1", "B two", "other words", published=T0 + DAY)
    env.conn.execute(
        "UPDATE annotations SET note = ? WHERE item_id = ?", (CANARY, "i0"))
    env.conn.commit()
    chat = FakeChat(rules={"x": True}, allow_private=False)
    conf = Confirmer(chat, env.conn, _desc_fn(env.conn, env.cfg["id"]), confirm_low=0.55)
    partition(_items(uv(0), uv(52)), 0.70, 21, confirmer=conf)
    assert chat.prompts, "the LLM was asked"
    assert CANARY not in "\n".join(chat.prompts)


# ------------------------------------------------------- migration v2 → v3

def test_migration_v2_to_v3_is_additive(tmp_path):
    # build a v2 db: full schema minus llm_decisions, stamped v2, with data
    import importlib.resources
    schema = (importlib.resources.files("gaceta") / "schema.sql").read_text()
    v2 = schema.split("-- v3 (additive)")[0]
    p = tmp_path / "v2.db"
    raw = sqlite3.connect(p)
    raw.executescript(v2)
    raw.execute("UPDATE meta SET v = '2' WHERE k = 'schema_version'")
    raw.execute("INSERT INTO readers (id, name, langs, created_at) VALUES ('r', 'R', 'en', 0)")
    raw.commit()
    raw.close()

    conn = gdb.connect(p)
    gdb.init(conn)  # additive migration
    assert conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()[0] == str(__import__("gaceta.db").db.SCHEMA_VERSION)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "llm_decisions" in names and "embeddings" in names
    assert conn.execute("SELECT COUNT(*) FROM readers").fetchone()[0] == 1  # data intact
    conn.execute("INSERT INTO llm_decisions (key, model, input_hash, output, usage, at) "
                 "VALUES ('k', 'm', 'h', '{}', '{}', 0)")
    conn.commit()
    gdb.init(conn)  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM llm_decisions").fetchone()[0] == 1
    conn.close()


# ------------------------------------------------------- eval --confirm path

def _seed_zone_pair(env):
    """Two items whose FakeEmbedder similarity lands in [.55, .70)."""
    add_item(env, "a", "first", "alpha beta mu gamma", published=T0)
    add_item(env, "b", "second", "alpha beta mu delta", published=T0 + DAY)
    from gaceta.editor.cluster import embed_items
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    va, vb = (np.frombuffer(env.conn.execute(
        "SELECT vec FROM embeddings WHERE item_id = ?", (i,)).fetchone()[0],
        dtype=np.float32) for i in ("a", "b"))
    sim = float(np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb)))
    assert 0.55 <= sim < 0.70, f"fixture wording drifted: sim={sim}"
    return sim


def test_eval_confirm_writes_only_cache_tables(env, capsys):
    _seed_zone_pair(env)
    env.cfg["llm"] = {"editor": {"provider": "fake", "allow_private": False}}
    golden = env.data_dir / "golden.json"
    golden.write_text(json.dumps({"items": ["a", "b"], "clusters": [["a", "b"]]}))
    before_stories = env.conn.execute(
        "SELECT COUNT(*) FROM stories WHERE reader_id = ?", (env.cfg["id"],)).fetchone()[0]
    line = ec.run(env.conn, env.cfg["id"], env.cfg, golden, confirm=True)
    out = json.loads(capsys.readouterr().out.strip())
    assert out["llm_calls"] == 1 and out["llm_cache_hits"] == 0
    assert "precision" in out and "b3_f1" in out and out["confirm_low"] == 0.55
    after_stories = env.conn.execute(
        "SELECT COUNT(*) FROM stories WHERE reader_id = ?", (env.cfg["id"],)).fetchone()[0]
    assert before_stories == after_stories == 0  # stories untouched
    # the one real confirm call is accounted: decision cache + day counter, nothing else
    assert env.conn.execute("SELECT COUNT(*) FROM llm_decisions").fetchone()[0] == 1
    assert env.conn.execute(
        "SELECT calls FROM llm_usage WHERE reader_id = ?", (env.cfg["id"],)).fetchone()[0] == 1
    assert line == out


# ------------------------------------------------------- providers

def test_fake_chat_deterministic_and_ordered_rules():
    chat = FakeChat(rules={"first": True, "second": False})
    text, usage = chat.complete([{"role": "user", "content": "the first one"}])
    assert json.loads(text) == {"same": True}
    text, _ = chat.complete([{"role": "user", "content": "the second one"}])
    assert json.loads(text) == {"same": False}
    text, _ = chat.complete([{"role": "user", "content": "neither"}])
    assert json.loads(text) == {"same": False}  # no match → False


def test_openai_compat_chat_retries_429_then_succeeds(monkeypatch):
    calls = {"n": 0}

    class FakeResponse:
        def __init__(self, body):
            self._body = json.dumps(body).encode()
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, None)
        return FakeResponse({"model": "glm-test", "choices": [{"message": {"content": "{\"same\": true}"}}],
                             "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)  # no real waiting
    monkeypatch.setenv("TEST_CHAT_KEY", "k")
    chat = OpenAICompatChat("http://x/v1", "TEST_CHAT_KEY", "glm-test")
    text, usage = chat.complete([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    assert prompts.parse_same(text) is True
    assert usage["served_model"] == "glm-test" and usage["prompt_tokens"] == 5


def test_openai_compat_chat_raises_on_4xx(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, None)
    from gaceta.providers.llm import LLMRefused
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("TEST_CHAT_KEY", "k")
    chat = OpenAICompatChat("http://x/v1", "TEST_CHAT_KEY", "glm-test")
    with pytest.raises(LLMRefused):
        chat.complete([{"role": "user", "content": "hi"}])


def test_openai_compat_chat_missing_key_names_env_not_values(monkeypatch):
    monkeypatch.delenv("TEST_CHAT_KEY", raising=False)
    chat = OpenAICompatChat("http://x/v1", "TEST_CHAT_KEY", "glm-test")
    with pytest.raises(RuntimeError) as e:
        chat.complete([{"role": "user", "content": "hi"}])
    assert "TEST_CHAT_KEY" in str(e.value)  # names the env var, no values anywhere


def test_from_config():
    with pytest.raises(ValueError):
        from_config({"provider": "nope"})
    with pytest.raises(ValueError):
        from_config({"provider": "openai", "model": "m"})  # missing base_url
    chat = from_config({"provider": "openai", "base_url": "http://x/v1",
                        "api_key_env": "K", "model": "m", "allow_private": True})
    assert chat.allow_private is True and chat.name == "openai-compat:m"
    assert isinstance(from_config({"provider": "fake"}), FakeChat)


def test_refused_request_skips_item_and_run_continues(monkeypatch):
    import io, urllib.error, urllib.request
    from gaceta.providers.llm import OpenAICompatChat, LLMRefused
    monkeypatch.setenv("FAKE_KEY", "x")
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":{"code":"1301"}}'))
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    chat = OpenAICompatChat("http://x", "FAKE_KEY", "m", allow_private=False)
    try:
        chat.complete([{"role": "user", "content": "hi"}])
        assert False, "expected LLMRefused"
    except LLMRefused as e:
        assert e.code == 400 and "1301" in e.detail


def test_auth_error_still_raises(monkeypatch):
    import io, urllib.error, urllib.request
    import pytest
    from gaceta.providers.llm import OpenAICompatChat, LLMRefused
    monkeypatch.setenv("FAKE_KEY", "x")
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    chat = OpenAICompatChat("http://x", "FAKE_KEY", "m", allow_private=False)
    with pytest.raises(urllib.error.HTTPError) as ei:
        chat.complete([{"role": "user", "content": "hi"}])
    assert not isinstance(ei.value, LLMRefused)


def test_quota_error_is_not_a_no(env):
    """ROADMAP 14: a 429 used to be cached as '' → parse_same False → a permanent
    'not the same story'. Now nothing is cached and the pair is asked next run."""
    from gaceta.providers.llm import LLMRefused
    add_item(env, "i0", "A one", "alpha beta gamma", published=T0)
    add_item(env, "i1", "B two", "other words", published=T0 + DAY)
    chat = FakeChat()

    def quota(messages, **kw):
        raise LLMRefused(429, "Weekly/Monthly Limit Exhausted")

    chat.complete = quota
    conf = Confirmer(chat, env.conn, _desc_fn(env.conn, env.cfg["id"]), confirm_low=0.55)
    assert partition(_items(uv(0), uv(52)), 0.70, 21, confirmer=conf) == [["i0"], ["i1"]]
    assert conf.throttled and conf.budget_exhausted
    assert env.conn.execute("SELECT COUNT(*) FROM llm_decisions").fetchone()[0] == 0
    _, conf2 = _confirmer(env, rules={"B two": True})
    assert partition(_items(uv(0), uv(52)), 0.70, 21, confirmer=conf2) == [["i0", "i1"]]
    assert conf2.calls == 1
