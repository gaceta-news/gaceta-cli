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
import uuid
from typing import Protocol


def _err_detail(e) -> str:
    try:
        return (e.read() or b"")[:200].decode("utf-8", "replace")
    except Exception:
        return ""


_process_uuid: str | None = None


def _uuid_once() -> str:
    """The literal ${uuid} header token: exactly one uuid4 per process."""
    global _process_uuid
    if _process_uuid is None:
        _process_uuid = str(uuid.uuid4())
    return _process_uuid


UUID_TOKEN = "${uuid}"


def _clean_headers(headers) -> dict | None:
    """Validate a headers table from config into request headers.

    Values are scalars (the literal ${uuid} expands to one uuid4 per process).
    Authorization is rejected: keys only ever travel via api_key_env. Header
    values are never logged, printed or stored anywhere but the request.
    """
    if headers is None:
        return None
    if not isinstance(headers, dict):
        raise ValueError("llm headers must be a table (dict)")
    out: dict[str, str] = {}
    for k, v in headers.items():
        name = str(k).strip()
        if not name:
            raise ValueError("llm headers: a header name is empty")
        if name.lower() == "authorization":
            raise ValueError(
                "llm headers must not set Authorization; API keys only travel via api_key_env")
        if isinstance(v, (dict, list)):
            raise ValueError(f"llm header {name!r} must be one value, not a table/array")
        out[name] = str(v).replace(UUID_TOKEN, _uuid_once())
    return out or None


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
    `headers` (reader config [llm.editor] headers) are extra request headers;
    ${uuid} in a value is one uuid4 per process; Authorization is rejected
    (keys only ever travel via api_key_env, values are never logged).
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
        headers: dict | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.allow_private = bool(allow_private)
        self.extra_body = dict(extra_body) if extra_body else None
        self.timeout = timeout
        self.retries = retries
        self.headers = _clean_headers(headers)
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
                         **({"Authorization": f"Bearer {key}"} if key else {}),
                         **(self.headers or {})},
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


class ChainChat:
    """Fallback chain of ChatModel members, primary first (DECISIONS #9).

    A member is down for the rest of the process after HTTP 429 (its own
    retries spent), 5xx after retries, 401/403, or a connection error
    (URLError/timeout): the next member serves instead, and later calls
    start after every member already down (sticky — never hammer a
    quota-limited provider). LLMRefused (other 4xx) is per-item, not
    failover: it re-raises. With every member down the last error
    re-raises; callers already treat that as "the run continues".

    name and allow_private are the PRIMARY's, so llm_decisions cache keys
    are shared across members and a fallback can never widen the model
    boundary. usage gains "via": "<member index>:<model>"; served_model is
    whatever the serving member's API actually returned.
    """

    def __init__(self, members):
        if not members:
            raise ValueError("chat chain needs at least one member")
        self.members = list(members)
        self.name = self.members[0].name
        self.allow_private = self.members[0].allow_private
        self._down: set[int] = set()
        self._last_error: Exception | None = None

    @staticmethod
    def _marks_down(e: BaseException) -> bool:
        """True when this error means the member is out for this process:
        quota (429, after the member's own retries), auth (401/403), a 5xx
        after retries, or an unreachable route. A per-item refusal never is."""
        if isinstance(e, LLMRefused):
            return e.code == 429  # OpenAICompatChat raises this once 429 retries are spent
        if isinstance(e, urllib.error.HTTPError):
            return e.code == 429 or e.code in (401, 403) or 500 <= e.code < 600
        return isinstance(e, (urllib.error.URLError, TimeoutError))

    def complete(
        self, messages: list[dict], *, max_tokens: int = 300, temperature: float = 0.0
    ) -> tuple[str, dict]:
        last = self._last_error or RuntimeError(
            f"chat chain {self.name!r}: all members are down")
        for i, m in enumerate(self.members):
            if i in self._down:
                continue
            try:
                text, usage = m.complete(
                    messages, max_tokens=max_tokens, temperature=temperature)
            except Exception as e:  # noqa: BLE001 — classified below
                if not self._marks_down(e):
                    raise
                self._down.add(i)
                self._last_error = last = e
                continue
            u = dict(usage or {})
            u["via"] = f"{i}:{getattr(m, 'model', m.name)}"
            return text, u
        raise last

    def preflight(self) -> None:
        """One tiny real request (max_tokens 1) to the first member before
        the run's real calls: a down-class failure (a fresh 429, auth,
        unreachable) marks it down, so the whole run starts at the next
        member. Never raises — a preflight is a hint; the first real call
        reports real errors."""
        if not self.members or 0 in self._down:
            return
        try:
            self.members[0].complete(
                [{"role": "user", "content": "1"}], max_tokens=1, temperature=0.0)
        except Exception as e:  # noqa: BLE001 — see above
            if self._marks_down(e):
                self._down.add(0)
                self._last_error = e


def from_config(cfg: dict) -> OpenAICompatChat | FakeChat | ChainChat:
    """Build the chat model from reader config [llm.editor]. No silent default:
    'fake' only when explicitly configured (same rule as embed.from_config).

    [[llm.editor.fallbacks]] members chain after the primary (ChainChat):
    each needs base_url (api_key_env defaults to the primary's, model to the
    primary's model); extra_body is NOT inherited unless the fallback sets
    one or asks with inherit_extra_body = true. allow_private is the
    primary's — a fallback cannot widen the model boundary.
    """
    cfg = cfg or {}
    provider = cfg.get("provider", "openai")
    if provider == "openai":
        for field in ("base_url", "model"):
            if not cfg.get(field):
                raise ValueError(f"[llm.editor] is missing {field!r}")
        extra = cfg.get("extra_body")
        if extra is not None and not isinstance(extra, dict):
            raise ValueError("[llm.editor] extra_body must be a table (dict)")
        fallbacks = cfg.get("fallbacks") or []
        if fallbacks and not isinstance(fallbacks, list):
            raise ValueError("[llm.editor] fallbacks must be an array of tables ([[...]])")
        timeout = int(cfg.get("timeout", 60))  # raise for slow local models on CPU
        primary = OpenAICompatChat(
            base_url=cfg["base_url"],
            api_key_env=cfg.get("api_key_env", "OPENAI_API_KEY"),
            model=cfg["model"],
            allow_private=cfg.get("allow_private", False),
            extra_body=extra,
            timeout=timeout,
            headers=cfg.get("headers"),
        )
        if not fallbacks:
            return primary
        members = [primary]
        for n, f in enumerate(fallbacks, 1):
            if not isinstance(f, dict) or not f.get("base_url"):
                raise ValueError(f"[[llm.editor.fallbacks]] #{n} is missing 'base_url'")
            if f.get("allow_private") and not primary.allow_private:
                raise ValueError(
                    "[[llm.editor.fallbacks]] allow_private cannot be true while "
                    "[llm.editor] allow_private is false (fallbacks never widen the boundary)")
            f_extra = f.get("extra_body")
            if f.get("inherit_extra_body"):
                f_extra = {**(extra or {}), **(f_extra or {})}
            if f_extra is not None and not isinstance(f_extra, dict):
                raise ValueError(f"[[llm.editor.fallbacks]] #{n} extra_body must be a table (dict)")
            members.append(OpenAICompatChat(
                base_url=f["base_url"],
                api_key_env=f.get("api_key_env", primary.api_key_env),
                model=f.get("model") or primary.model,
                allow_private=primary.allow_private,
                extra_body=f_extra or None,
                timeout=timeout,
                headers=f.get("headers"),
            ))
        return ChainChat(members)
    if provider == "fake":
        return FakeChat(allow_private=cfg.get("allow_private", True), cards=True)
    raise ValueError(f"unknown llm provider {provider!r} (expected 'openai' or 'fake')")
