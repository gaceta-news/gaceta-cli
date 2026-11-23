"""[llm.editor] declared headers and the fallback chain (Feature: llm chain).

Offline only: urllib's urlopen is monkeypatched with a scripted router, never
a real request. Headers are extra request headers (${uuid} = one uuid4 per
process, Authorization rejected); ChainChat tries members in order and a
429/5xx/401/403/unreachable member is down for the rest of the process
(sticky), while a per-item refusal (other 4xx) never fails over.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid as uuid_mod

import pytest

from gaceta.editor import writeup
from gaceta.providers.llm import (
    ChainChat,
    LLMRefused,
    OpenAICompatChat,
    from_config,
)

A = "https://api.example-a.test/v4"
B = "https://api.example-b.test/v4"
C = "https://api.example-c.test/v4"


class FakeResp:
    def __init__(self, content="{}", model="m"):
        self._body = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "model": model,
        }).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Router:
    """urlopen stand-in: a per-base_url script of outcomes, popped in order
    (the last one repeats); every request is recorded as (base_url, request)."""

    def __init__(self):
        self.script: dict[str, list] = {}
        self.requests: list[tuple[str, urllib.request.Request]] = []

    def add(self, base, *outcomes):
        self.script[base] = list(outcomes)

    def __call__(self, req, timeout=None):
        base = next(b for b in self.script if req.full_url.startswith(b))
        outcomes = self.script[base]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        self.requests.append((base, req))
        kind, val = outcome
        if kind == "ok":
            return FakeResp(val)
        if kind == "http":
            raise urllib.error.HTTPError(req.full_url, val, "err", {}, None)
        raise val  # ("url", exception): URLError/timeout, as from a dead route


def member(base, model="m", **kw):
    return OpenAICompatChat(base, "", model, retries=0, **kw)


# ---------------------------------------------------------------- headers

def test_headers_sent_and_uuid_stable_within_process(monkeypatch):
    router = Router()
    router.add(A, ("ok", '{"same": true}'))
    monkeypatch.setattr(urllib.request, "urlopen", router)
    chat = from_config({
        "provider": "openai", "base_url": A, "api_key_env": "", "model": "m",
        "headers": {"User-Agent": "gaceta/0.3", "x-opencode-session": "gaceta-${uuid}"},
    })
    assert isinstance(chat, OpenAICompatChat)  # headers alone never change the shape
    chat.complete([{"role": "user", "content": "hi"}])
    chat.complete([{"role": "user", "content": "again"}])
    h = [req.headers for _, req in router.requests]
    assert h[0]["User-agent"] == "gaceta/0.3"
    assert h[0]["Content-type"] == "application/json"
    sid = h[0]["X-opencode-session"]
    parsed = uuid_mod.UUID(sid[len("gaceta-"):])
    assert parsed.version == 4  # the ${uuid} token really is a uuid4
    assert h[1]["X-opencode-session"] == sid  # one uuid4 per process, stable


def test_authorization_header_rejected_any_case():
    # the value must never travel in an error message either
    for headers in ({"Authorization": "Bearer sekrit"},
                    {"authorization": "Bearer sekrit"}):
        with pytest.raises(ValueError, match="Authorization") as e:
            OpenAICompatChat(A, "", "m", headers=headers)
        assert "sekrit" not in str(e.value)
    with pytest.raises(ValueError, match="Authorization"):
        from_config({"provider": "openai", "base_url": A, "model": "m",
                     "headers": {"Authorization": "x"}})


def test_headers_must_be_a_table_of_scalars():
    with pytest.raises(ValueError, match="table"):
        from_config({"provider": "openai", "base_url": A, "model": "m",
                     "headers": ["User-Agent"]})
    with pytest.raises(ValueError, match="one value"):
        OpenAICompatChat(A, "", "m", headers={"X-List": ["a", "b"]})


def test_no_headers_configured_is_today_s_request(monkeypatch):
    router = Router()
    router.add(A, ("ok", "{}"))
    monkeypatch.setattr(urllib.request, "urlopen", router)
    chat = from_config({"provider": "openai", "base_url": A, "api_key_env": "", "model": "m"})
    chat.complete([{"role": "user", "content": "hi"}])
    assert router.requests[0][1].headers == {"Content-type": "application/json"}


# ---------------------------------------------------------------- chain

def test_primary_429_falls_back_and_sticks(monkeypatch):
    router = Router()
    router.add(A, ("http", 429))                    # always 429 (quota gone)
    router.add(B, ("ok", '{"same": true}'))
    monkeypatch.setattr(urllib.request, "urlopen", router)
    chain = ChainChat([member(A), member(B)])
    text, usage = chain.complete([{"role": "user", "content": "x"}])
    assert json.loads(text) == {"same": True}
    assert usage["via"] == "1:m" and usage["served_model"] == "m"
    # sticky: the next call goes straight to the fallback, primary not retried
    chain.complete([{"role": "user", "content": "y"}])
    assert [base for base, _ in router.requests] == [A, B, B]


def test_auth_5xx_and_network_errors_fail_over(monkeypatch):
    for outcome in (("http", 401), ("http", 503),
                    ("url", urllib.error.URLError("no route"))):
        router = Router()
        router.add(A, outcome)
        router.add(B, ("ok", '{"same": false}'))
        monkeypatch.setattr(urllib.request, "urlopen", router)
        chain = ChainChat([member(A), member(B)])
        text, usage = chain.complete([{"role": "user", "content": "x"}])
        assert json.loads(text) == {"same": False}
        assert usage["via"] == "1:m"


def test_refused_is_per_item_never_a_failover(monkeypatch):
    router = Router()
    router.add(A, ("http", 400))                    # content filter → LLMRefused
    router.add(B, ("ok", '{"same": true}'))
    monkeypatch.setattr(urllib.request, "urlopen", router)
    chain = ChainChat([member(A), member(B)])
    with pytest.raises(LLMRefused) as e:
        chain.complete([{"role": "user", "content": "x"}])
    assert e.value.code == 400
    assert [base for base, _ in router.requests] == [A]  # B never asked
    with pytest.raises(LLMRefused):
        chain.complete([{"role": "user", "content": "x"}])
    assert [base for base, _ in router.requests] == [A, A]  # A retried, not skipped


def test_all_members_down_raises_the_last_error(monkeypatch):
    router = Router()
    router.add(A, ("http", 429))
    router.add(B, ("http", 401))
    monkeypatch.setattr(urllib.request, "urlopen", router)
    chain = ChainChat([member(A), member(B)])
    with pytest.raises(urllib.error.HTTPError):     # the last error: 401 from B
        chain.complete([{"role": "user", "content": "x"}])
    n = len(router.requests)
    with pytest.raises(urllib.error.HTTPError):     # still down, no new request
        chain.complete([{"role": "user", "content": "x"}])
    assert len(router.requests) == n


def test_chain_shares_the_primary_name_and_boundary():
    chain = ChainChat([member(A, model="m1", allow_private=False),
                      member(B, model="m2", allow_private=True)])
    assert chain.name == f"openai-compat:m1"       # one cache key across members
    assert chain.allow_private is False             # the primary's boundary


def test_preflight_429_marks_primary_down(monkeypatch):
    router = Router()
    router.add(A, ("http", 429))
    router.add(B, ("ok", '{"same": true}'))
    monkeypatch.setattr(urllib.request, "urlopen", router)
    chain = ChainChat([member(A), member(B)])
    chain.preflight()
    assert router.requests[0][0] == A
    assert json.loads(router.requests[0][1].data)["max_tokens"] == 1  # tiny probe
    text, usage = chain.complete([{"role": "user", "content": "x"}])
    assert usage["via"] == "1:m"
    assert [base for base, _ in router.requests[1:]] == [B]


def test_preflight_success_and_refusal_keep_the_primary(monkeypatch):
    router = Router()
    router.add(A, ("ok", "{}"))
    router.add(B, ("ok", "{}"))
    monkeypatch.setattr(urllib.request, "urlopen", router)
    chain = ChainChat([member(A), member(B)])
    chain.preflight()                               # healthy → nothing marked
    assert chain.complete([{"role": "user", "content": "x"}])[1]["via"] == "0:m"

    router2 = Router()
    router2.add(A, ("http", 400), ("ok", "{}"))     # 'ping' refused, then healthy
    router2.add(B, ("ok", "{}"))
    monkeypatch.setattr(urllib.request, "urlopen", router2)
    chain2 = ChainChat([member(A), member(B)])
    chain2.preflight()                              # never raises
    assert chain2.complete([{"role": "user", "content": "x"}])[1]["via"] == "0:m"


# ---------------------------------------------------------------- from_config

def test_from_config_builds_chain_with_member_defaults():
    cfg = {
        "provider": "openai", "base_url": A, "api_key_env": "K1", "model": "m1",
        "extra_body": {"thinking": {"type": "disabled"}},
        "fallbacks": [
            {"base_url": B, "api_key_env": "K2"},
            {"base_url": C, "api_key_env": "K3", "model": "m2",
             "inherit_extra_body": True, "headers": {"User-Agent": "gaceta/0.3"}},
        ],
    }
    chain = from_config(cfg)
    assert isinstance(chain, ChainChat) and chain.name == "openai-compat:m1"
    m1, m2, m3 = chain.members
    assert m2.base_url == B and m2.api_key_env == "K2"
    assert m2.model == "m1" and m2.extra_body is None      # model defaults, extras NOT inherited
    assert m3.base_url == C and m3.model == "m2"
    assert m3.extra_body == {"thinking": {"type": "disabled"}}  # inherited on request
    assert m3.headers == {"User-Agent": "gaceta/0.3"}       # per-fallback headers
    assert m3.timeout == m1.timeout and m3.allow_private == m1.allow_private


def test_fallback_cannot_widen_allow_private():
    with pytest.raises(ValueError, match="allow_private"):
        from_config({
            "provider": "openai", "base_url": A, "api_key_env": "K", "model": "m",
            "allow_private": False,
            "fallbacks": [{"base_url": B, "api_key_env": "K2", "allow_private": True}],
        })
    # primary private → the chain boundary is the primary's, fallback flags ignored
    chain = from_config({
        "provider": "openai", "base_url": A, "api_key_env": "K", "model": "m",
        "allow_private": True,
        "fallbacks": [{"base_url": B, "api_key_env": "K2"}],
    })
    assert chain.allow_private is True and chain.members[1].allow_private is True


def test_fallbacks_need_base_url_and_tables():
    with pytest.raises(ValueError, match="base_url"):
        from_config({"provider": "openai", "base_url": A, "model": "m",
                     "fallbacks": [{"api_key_env": "K2"}]})
    with pytest.raises(ValueError, match="array of tables"):
        from_config({"provider": "openai", "base_url": A, "model": "m",
                     "fallbacks": "nope"})


def test_write_stories_through_a_chain_shares_the_cache_key(env, monkeypatch):
    """End to end: a 429'd primary fails over inside write_stories, the usage
    note says which member served, and the reply is cached under the chain's
    (primary's) name — a rerun costs 0 calls."""
    from gaceta.editor import writeup
    from test_cluster import T0
    from test_writeup import GOOD, NOW, build_story

    build_story(env, ("a1", "A one", "alpha beta gamma", "noted", "public", T0))
    router = Router()
    router.add(A, ("http", 429))
    router.add(B, ("ok", GOOD))
    monkeypatch.setattr(urllib.request, "urlopen", router)
    monkeypatch.setattr("time.sleep", lambda s: None)  # primary retries don't wait
    env.cfg["llm"] = {"editor": {
        "provider": "openai", "base_url": A, "api_key_env": "", "model": "m",
        "fallbacks": [{"base_url": B, "api_key_env": ""}],
    }}
    r1 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, now=NOW)
    assert r1["written"] == 1 and r1["llm_calls"] == 1
    usage = json.loads(env.conn.execute("SELECT usage FROM llm_decisions").fetchone()[0])
    assert usage["via"] == "1:m"
    # force the story back into the queue: same prompt → served from the cache
    env.conn.execute("UPDATE story_versions SET members = NULL")
    env.conn.commit()
    r2 = writeup.write_stories(env.conn, env.cfg["id"], env.cfg, now=NOW)
    assert r2["llm_calls"] == 0 and r2["llm_cache_hits"] == 1 and r2["written"] == 1


def test_gaceta_write_preflights_when_configured(env, monkeypatch, capsys):
    """`gaceta write` with [llm.editor] preflight = true spends one 1-token
    probe on the primary first; a 429'd primary is skipped for the run."""
    import time as time_mod

    from gaceta.cli import main as cli_main
    from gaceta.editor.cluster import cluster as run_cluster
    from gaceta.editor.cluster import embed_items
    from gaceta.providers.embed import FakeEmbedder
    from test_cluster import add_item
    from test_writeup import GOOD

    toml = env.data_dir / "readers" / env.cfg["id"] / "reader.toml"
    toml.write_text(toml.read_text() + f"""
[llm.editor]
provider = "openai"
base_url = "{A}"
api_key_env = ""
model = "m"
preflight = true

[[llm.editor.fallbacks]]
base_url = "{B}"
api_key_env = ""
""")
    add_item(env, "a1", "A one", "alpha beta gamma", "noted", "public",
             int(time_mod.time()) - 3600)  # inside the 14-day window
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    run_cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)
    env.conn.commit()

    router = Router()
    router.add(A, ("http", 429))
    router.add(B, ("ok", GOOD))
    monkeypatch.setattr(urllib.request, "urlopen", router)
    monkeypatch.setattr("time.sleep", lambda s: None)  # primary retries don't wait
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli_main(["write", "--reader", env.cfg["id"], "--days", "14"])
    out = capsys.readouterr().out
    assert rc == 0 and "1 written" in out
    bases = [base for base, _ in router.requests]
    bodies = [json.loads(req.data) for _, req in router.requests]
    # the probe: every attempt tiny (max_tokens 1) and to the primary ...
    assert all(b == A and bodies[i]["max_tokens"] == 1 for i, b in enumerate(bases[:-1]))
    # ... after its own retries it is down, and the run's one real call serves from B
    assert bases[-1] == B and bodies[-1]["max_tokens"] == writeup.WRITEUP_MAX_TOKENS
