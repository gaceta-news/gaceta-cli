"""Embedding providers: adapters with config (DECISIONS #9). No hardcoded hosts in callers."""
from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from typing import Protocol


class Embedder(Protocol):
    """Anything that can turn texts into fixed-dim vectors."""

    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    """POST {base_url}/api/embed with {model, input:[...]}, batched. urllib only."""

    def __init__(self, base_url: str, model: str, batch: int = 32, timeout: int = 300):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.batch = batch
        self.timeout = timeout
        self.name = f"ollama:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            out.extend(self._post(texts[i : i + self.batch]))
        return out

    def _post(self, batch: list[str]) -> list[list[float]]:
        req = urllib.request.Request(
            f"{self.base_url}/api/embed",
            data=json.dumps({"model": self.model, "input": batch}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        return data["embeddings"]


FAKE_DIM = 256


class OpenAIEmbedder:
    """POST {base_url}/embeddings (any OpenAI-compatible endpoint). urllib only.

    Mirrors providers/llm.py's OpenAICompatChat: the API key is read from an
    env var named in config at call time and is never logged, printed or stored.
    """

    def __init__(self, base_url: str, api_key_env: str, model: str,
                 batch: int = 64, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.batch = batch
        self.timeout = timeout
        self.name = f"openai:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            out.extend(self._post(texts[i : i + self.batch]))
        return out

    def _post(self, batch: list[str]) -> list[list[float]]:
        import os

        key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if self.api_key_env and not key:
            # name the env var, never a value
            raise RuntimeError(f"embed model {self.model!r}: env {self.api_key_env} is not set")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps({"model": self.model, "input": batch}).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {key}"} if key else {})},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        # the API may return embeddings out of order; honor the index field
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]


class FakeEmbedder:
    """Deterministic signed bag-of-words hashing vector. For tests, no network."""

    def __init__(self, dim: int = FAKE_DIM):
        self.dim = dim
        self.name = f"fake:{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in text.lower().split():
            h = hashlib.md5(tok.encode()).digest()
            v[int.from_bytes(h[:4], "big") % self.dim] += 1.0 if h[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


def model_name(cfg: dict) -> str:
    """Name under which embeddings for this config are stored. No network."""
    e = cfg.get("embed") or {}
    provider = e.get("provider")
    if provider == "ollama":
        return f"ollama:{e.get('model', 'bge-m3')}"
    if provider == "openai":
        return f"openai:{e.get('model', 'text-embedding-3-small')}"
    if provider == "fake":
        return f"fake:{FAKE_DIM}"
    raise ValueError(f"unknown embed provider {provider!r} (expected 'ollama', 'openai' or 'fake')")


def from_config(cfg: dict) -> OllamaEmbedder | OpenAIEmbedder | FakeEmbedder:
    """Build the embedder from reader/app config [embed]. No silent default:
    'fake' only when explicitly configured."""
    e = cfg.get("embed") or {}
    provider = e.get("provider")
    if provider == "ollama":
        return OllamaEmbedder(
            base_url=e.get("base_url", "http://localhost:11434"),
            model=e.get("model", "bge-m3"),
        )
    if provider == "openai":
        for field in ("base_url", "model"):
            if not e.get(field):
                raise ValueError(f"[embed] provider 'openai' is missing {field!r}")
        return OpenAIEmbedder(
            base_url=e["base_url"],
            api_key_env=e.get("api_key_env", "OPENAI_API_KEY"),
            model=e["model"],
        )
    if provider == "fake":
        return FakeEmbedder()
    raise ValueError(f"unknown embed provider {provider!r} (expected 'ollama', 'openai' or 'fake')")
