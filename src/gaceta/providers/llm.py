"""LLM chat providers: adapters with per-reader credentials (DECISIONS #9).

The core never hardcodes a model or reads Hermes config; everything comes from
reader config [llm.editor]. API keys are read from the environment at call time
and are never logged, printed or stored.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Protocol


def _err_detail(e) -> str:
    try:
        return (e.read() or b"")[:200].decode("utf-8", "replace")
    except Exception:
        return ""


class LLMRefused(Exception):
    """The provider refused one request (4xx other than auth). Callers skip the item; the run continues."""

    def __init__(self, code: int, detail: str = ""):
        super().__init__(f"HTTP {code}: {detail}")
        self.code, self.detail = code, detail


class ChatModel(Protocol):
    """Anything that can complete a chat. `allow_private` is the model boundary
    (WORKFLOW §3): only True lets prompts carry the reader's annotation note."""

    name: str
    allow_private: bool

    def complete(
        self, messages: list[dict], *, max_tokens: int = 300, temperature: float = 0.0
    ) -> tuple[str, dict]: ...


class OpenAICompatChat:
    """POST {base_url}/chat/completions (any OpenAI-compatible endpoint). urllib only.

    2 retries with backoff on 429/5xx, timeout 60 s. usage carries the served
    model from the API response (`model` field) so callers can verify it.
    """

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        model: str,
        allow_private: bool = False,
        extra_body: dict | None = None,
        timeout: int = 60,
        retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.allow_private = bool(allow_private)
        self.extra_body = dict(extra_body) if extra_body else None
        self.timeout = timeout
        self.retries = retries
        self.name = f"openai-compat:{model}"

    def complete(
        self, messages: list[dict], *, max_tokens: int = 300, temperature: float = 0.0
    ) -> tuple[str, dict]:
        key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if self.api_key_env and not key:
            # name the env var, never a value
            raise RuntimeError(f"chat model {self.model!r}: env {self.api_key_env} is not set")
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self.extra_body:  # provider-specific extras from reader config (e.g. Z.ai thinking off)
            payload.update(self.extra_body)
        body = json.dumps(payload).encode()
        data = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=body,
                headers={"Content-Type": "application/json",
                         **({"Authorization": f"Bearer {key}"} if key else {})},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                retryable = e.code == 429 or 500 <= e.code < 600
                if retryable and attempt < self.retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                if e.code not in (401, 403) and 400 <= e.code < 500:
                    # per-request refusal (content filter, bad input): skip this item, keep the run alive
                    raise LLMRefused(e.code, _err_detail(e)) from e
                raise
        if data is None:  # unreachable: retryable errors raise on the last attempt
            raise RuntimeError(f"chat model {self.model!r}: no response after retries")
        text = data["choices"][0]["message"]["content"]
        u = data.get("usage") or {}
        usage = {
            "prompt_tokens": u.get("prompt_tokens"),
            "completion_tokens": u.get("completion_tokens"),
            "total_tokens": u.get("total_tokens"),
            "served_model": data.get("model"),  # what the API actually served
        }
        return text, usage


class FakeChat:
    """Deterministic chat model for tests (no network, WORKFLOW §3 cassettes/fakes).

    rules: a callable prompt_text -> bool, or an ordered {substring: bool} mapping
    (first substring found in the prompt wins; no match -> False).
    """

    def __init__(self, rules=None, name: str = "fake:chat", allow_private: bool = True,
                 cards: bool = False):
        self.rules = rules or {}
        # cards=True (config provider "fake"): answer story-card prompts with a valid
        # card built from the first TITLE/BODY lines, so the offline demo renders cards.
        self.cards = cards
        self.name = name
        self.allow_private = allow_private
        self.prompts: list[str] = []  # every prompt seen, for assertions

    def complete(
        self, messages: list[dict], *, max_tokens: int = 300, temperature: float = 0.0
    ) -> tuple[str, dict]:
        prompt = "\n".join(m["content"] for m in messages)
        self.prompts.append(prompt)
        same = self._decide(prompt)
        n_in = len(prompt.split())
        usage = {
            "prompt_tokens": n_in,
            "completion_tokens": 3,
            "total_tokens": n_in + 3,
            "served_model": self.name,
        }
        if self.cards and '"why_it_matters"' in prompt:
            return json.dumps(self._card(prompt)), usage
        return json.dumps({"same": same}), usage

    @staticmethod
    def _card(prompt: str) -> dict:
        def first(tag: str) -> str:
            for line in prompt.splitlines():
                if line.startswith(tag):
                    return line[len(tag):].strip()
            return ""
        title = first("TITLE:") or "Untitled story"
        body = first("BODY:")
        summary = (body[:240] + "…") if len(body) > 240 else (body or title)
        return {"title": title[:120], "summary": summary,
                "why_it_matters": "Offline demo card (fake model): connect a real model for write-ups.",
                "confidence": 0.5}

    def _decide(self, prompt: str) -> bool:
        if callable(self.rules):
            return bool(self.rules(prompt))
        for needle, same in self.rules.items():
            if needle in prompt:
                return bool(same)
        return False


def from_config(cfg: dict) -> OpenAICompatChat | FakeChat:
    """Build the chat model from reader config [llm.editor]. No silent default:
    'fake' only when explicitly configured (same rule as embed.from_config)."""
    cfg = cfg or {}
    provider = cfg.get("provider", "openai")
    if provider == "openai":
        for field in ("base_url", "model"):
            if not cfg.get(field):
                raise ValueError(f"[llm.editor] is missing {field!r}")
        extra = cfg.get("extra_body")
        if extra is not None and not isinstance(extra, dict):
            raise ValueError("[llm.editor] extra_body must be a table (dict)")
        return OpenAICompatChat(
            base_url=cfg["base_url"],
            api_key_env=cfg.get("api_key_env", "OPENAI_API_KEY"),
            model=cfg["model"],
            allow_private=cfg.get("allow_private", False),
            extra_body=extra,
            timeout=int(cfg.get("timeout", 60)),  # raise for slow local models on CPU
        )
    if provider == "fake":
        return FakeChat(allow_private=cfg.get("allow_private", True), cards=True)
    raise ValueError(f"unknown llm provider {provider!r} (expected 'openai' or 'fake')")
