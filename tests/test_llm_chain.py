"""[llm.editor] declared headers (Feature: llm chain, part 1).

Offline only: urllib's urlopen is monkeypatched with a scripted router, never
a real request. Headers are extra request headers (${uuid} = one uuid4 per
process, Authorization rejected).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid as uuid_mod

import pytest

from gaceta.providers.llm import OpenAICompatChat, from_config

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
