"""Nostr web-of-trust source adapter (roadmap 11a, read-only).

Reads the reader's WoT-ranked nostr notes (kinds 1 and 30023) from a NIP-50
search relay — the same lens The Nostr Observer uses (docs/OBSERVER-LESSONS.md).
Ported from the Observer's readiness.mjs/chain.mjs/corpus.mjs (JavaScript).

Load-bearing facts, all measured on the reference relay (search.brainstorm.world):
- WITHOUT a resolvable observer the relay does not fail: `sort:rank` silently
  becomes the ANONYMOUS ranking (measured: 209 of 400 posts from one spam
  account). So the adapter refuses to ingest until a readiness chain passes
  (kind 10040 naming a 30382:rank service with a relay hint → the service's
  kind 30382 cards exist → a ranked read actually comes back through the lens).
- Unranked lookups (kind 0 profiles, kind 10040) must carry the `include:spam`
  token or the relay's auth gate CLOSES the query outright.
- The reader's own posts rank highly through their own lens almost by
  construction; they are excluded (a paper is what other people did).

Reader config (data/readers/<id>/reader.toml):

    [sources.nostr_wot]
    pubkey = "<reader pubkey hex>"     # whose web of trust (64 hex chars)
    relay = "wss://search.brainstorm.world"   # optional
    window_hours = 24                  # optional lookback (default 24)
    limit = 300                        # optional max notes per pull (default 300)
    floor = 20                         # optional trust floor filter:rank:gte (default 20)
    into_stories = false               # optional: verdict 'noted' so embed/cluster
                                       # pick the items up (editor gate); default off

Item ids are 'nostr:<event hex id>' — the same scheme other nostr adapters
use, so a note already ingested from follows dedupes instead of duplicating.
Source ids are 'nostr:<author name>' like the existing nostr sources, but
origin='nostr_wot' and items.author_pubkey record the provenance so later code
can tell followed (e.g. origin 'sqlite') from WoT-discovered (nostr_wot) authors.
content_public=1: notes are publicly published content. Note bodies are
strangers' text: stored as data, never executed or followed.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Iterator

from .base import RawItem, RawSource

DEFAULT_RELAY = "wss://search.brainstorm.world"
INCLUDE_SPAM = "include:spam"          # token the relay's auth gate needs on unranked queries
KIND_TRUST_PROVIDERS = 10040           # NIP-85: who works out your web of trust
KIND_CONTACT_CARD = 30382              # NIP-85 trust assertion (the rank cards)
RANK_SERVICE = "30382:rank"
KIND_NOTE, KIND_LONGFORM = 1, 30023
DEFAULT_WINDOW_HOURS = 24
DEFAULT_LIMIT = 300
DEFAULT_FLOOR = 20
IDLE_TIMEOUT = 25.0                    # seconds of relay silence before giving up (no retries)
PROBE_LIMIT = 12
PROFILE_CHUNK = 100                    # authors per kind-0 REQ


# ---------------------------------------------------------------- bech32 (npub)
# Minimal port of the Observer's nostr.mjs: enough to print a short npub.
# The paper never prints a bare hex string (reference/editorial.md).

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _polymod(values: list[int]) -> int:
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _GENERATOR[i]
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert_bits(data: list[int], frm: int, to: int, pad: bool) -> list[int] | None:
    acc = bits = 0
    out: list[int] = []
    maxv = (1 << to) - 1
    for value in data:
        if value < 0 or value >> frm:
            return None
        acc = (acc << frm) | value
        bits += frm
        while bits >= to:
            bits -= to
            out.append((acc >> bits) & maxv)
    if pad:
        if bits > 0:
            out.append((acc << (to - bits)) & maxv)
    elif bits >= frm or ((acc << (to - bits)) & maxv):
        return None
    return out


def to_npub(hexkey: str) -> str:
    data = bytes.fromhex(hexkey)
    bits = _convert_bits(list(data), 8, 5, True)
    if bits is None:
        raise ValueError(f"not a pubkey: {hexkey[:16]}")
    values = _hrp_expand("npub") + bits
    chk = _polymod(values + [0] * 6) ^ 1
    tail = [(chk >> (5 * (5 - i))) & 31 for i in range(6)]
    return "npub1" + "".join(_CHARSET[d] for d in bits + tail)


def short_npub(hexkey: str) -> str:
    """What a person is called when they published no name. Never hex."""
    npub = to_npub(hexkey)
    return npub[:10] + "…" + npub[-4:]


# ---------------------------------------------------------------- relay client

class RelayClient:
    """One-shot NIP-01 REQ reader over one websocket, with bounded timeouts.

    The timeout is an IDLE window, not a deadline: drain until the relay has
    said nothing for `idle_timeout` seconds. AUTH/NOTICE/OK frames are ignored
    (the relay sends an AUTH challenge unprompted; resolving on the first
    non-EVENT frame reads that challenge as an empty answer). A wall clock at
    2*idle+5 guards the whole call. No retries, ever: the relay has spells of
    not answering and must not be hammered.
    """

    def __init__(self, idle_timeout: float = IDLE_TIMEOUT, open_timeout: float = 10.0):
        self.idle_timeout = idle_timeout
        self.open_timeout = open_timeout

    def req(self, url: str, filters: list[dict]) -> tuple[list[dict], str | None]:
        """Everything matching `filters`. Returns (events, note); note is None on
        a clean EOSE, else why the read stopped ('idle', 'deadline', relay message)."""
        from websockets.sync.client import connect  # locked dep; imported here so tests need no socket

        sub = uuid.uuid4().hex[:12]
        events: list[dict] = []
        seen: set[str] = set()
        note: str | None = None
        hard = time.monotonic() + self.idle_timeout * 2 + 5.0
        with connect(url, open_timeout=self.open_timeout, close_timeout=5.0,
                     ping_interval=20.0) as ws:
            ws.send(json.dumps(["REQ", sub, *filters]))
            while True:
                left = hard - time.monotonic()
                if left <= 0:
                    note = "deadline"
                    break
                try:
                    raw = ws.recv(timeout=min(self.idle_timeout, left))
                except TimeoutError:
                    note = "idle"
                    break
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                if not (isinstance(frame, list) and len(frame) >= 2):
                    continue
                verb, sid = frame[0], frame[1]
                if verb == "EVENT" and sid == sub and len(frame) >= 3:
                    ev = frame[2]
                    eid = ev.get("id") if isinstance(ev, dict) else None
                    if eid and eid not in seen:
                        seen.add(eid)
                        events.append(ev)
                elif verb == "EOSE" and sid == sub:
                    break
                elif verb == "CLOSED" and sid == sub:
                    note = f"relay closed: {frame[2] if len(frame) > 2 else 'no reason'}"
                    break
                # AUTH / NOTICE / OK: not anybody's answer; the idle clock restarts
            try:
                ws.send(json.dumps(["CLOSE", sub]))
            except Exception:  # noqa: BLE001 — socket already gone
                pass
        return events, note


# ---------------------------------------------------------------- readiness

def _rank_provider(event: dict | None) -> str | None:
    """The 30382:rank service the reader's kind 10040 names, with a relay hint.
    All three fields required: a 30382:followers entry can order but not rank,
    and an entry with no relay hint resolves to nothing in the provider map."""
    if not event:
        return None
    for tag in event.get("tags") or []:
        if (isinstance(tag, list) and len(tag) >= 3 and tag[0] == RANK_SERVICE
                and tag[1] and tag[2]):
            return str(tag[1])
    return None


def _assess(facts: dict) -> dict:
    """Pure readiness verdict, ported from the Observer's chain.mjs: the first
    unmet link wins so one failure is never reported as four."""
    state = "ready"
    if not facts["score_list_seen"]:
        state = "no-score-list"
    elif not facts["rank_service"]:
        state = "no-rank-service"
    elif not facts["scores_here"]:
        state = "no-scores-yet"
    elif facts["probe_anonymous"] > 0 and facts["probe_observed"] == 0:
        # cards present but not projected: the ranked read silently degrades to
        # the anonymous (spam) ranking — the one failure this chain exists for
        state = "projection-pending"
    return {"ready": state == "ready", "state": state}


class NotReadyError(RuntimeError):
    """Raised when the reader's lens is not resolvable; nothing is ingested."""

    def __init__(self, verdict: dict):
        self.verdict = verdict
        super().__init__(f"nostr-wot not ready: {verdict['state']}")


# ---------------------------------------------------------------- adapter

class NostrWotAdapter:
    name = "nostr_wot"

    def __init__(self, cfg: dict, client: RelayClient | None = None):
        pubkey = str(cfg.get("pubkey") or "").strip().lower()
        if len(pubkey) != 64 or any(c not in "0123456789abcdef" for c in pubkey):
            raise ValueError("sources.nostr_wot.pubkey must be 64 hex characters")
        self.pubkey = pubkey
        self.relay = str(cfg.get("relay") or DEFAULT_RELAY)
        self.window_hours = int(cfg.get("window_hours", DEFAULT_WINDOW_HOURS))
        self.limit = int(cfg.get("limit", DEFAULT_LIMIT))
        self.floor = int(cfg.get("floor", DEFAULT_FLOOR))
        self.into_stories = bool(cfg.get("into_stories", False))
        self._client = client or RelayClient()
        self.fetched = 0            # events kept after the own-post filter
        self.authors: set[str] = set()

    # -- readiness chain (every question is a REQ; no NIP-45 COUNT) ----------
    def readiness(self, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else now
        since = now - self.window_hours * 3600
        try:
            score_list, _ = self._client.req(self.relay, [
                {"kinds": [KIND_TRUST_PROVIDERS], "authors": [self.pubkey],
                 "limit": 1, "search": INCLUDE_SPAM}])
            service = _rank_provider(score_list[0] if score_list else None)
            cards = False
            if service:
                got, _ = self._client.req(self.relay, [
                    {"kinds": [KIND_CONTACT_CARD], "authors": [service],
                     "limit": 1, "search": INCLUDE_SPAM}])
                cards = bool(got)
            # no trust floor on the probe: it must be able to SEE the silent
            # degradation to anonymous ranking, and a floor would hide it
            observed, _ = self._client.req(self.relay, [
                {"kinds": [KIND_NOTE], "since": since, "limit": PROBE_LIMIT,
                 "search": f"observer:{self.pubkey} sort:rank"}])
            anonymous, _ = self._client.req(self.relay, [
                {"kinds": [KIND_NOTE], "since": since, "limit": PROBE_LIMIT,
                 "search": f"{INCLUDE_SPAM} sort:rank"}])
        except OSError as exc:  # includes websocket errors; never retry
            return {"ready": False, "state": "unreachable", "detail": str(exc)[:200]}
        return _assess({
            "score_list_seen": bool(score_list),
            "rank_service": service,
            "scores_here": cards,
            "probe_observed": len(observed),
            "probe_anonymous": len(anonymous),
        })

    # -- the ranked pull ------------------------------------------------------
    def items(self, since: int | None = None) -> Iterator[RawItem]:
        verdict = self.readiness()
        if not verdict["ready"]:
            raise NotReadyError(verdict)
        until = int(time.time())
        start = since if since is not None else until - self.window_hours * 3600
        events, _note = self._client.req(self.relay, [{
            "kinds": [KIND_NOTE, KIND_LONGFORM],
            "since": start,
            "until": until,
            "limit": self.limit,
            "search": (f"observer:{self.pubkey} sort:rank "
                       f"filter:rank:gte:{self.floor}"),
        }])
        # a paper is what other people did; the reader's own words are not the news
        events = [e for e in events if isinstance(e, dict) and e.get("pubkey") != self.pubkey]
        profiles = self._profiles({e["pubkey"] for e in events if e.get("pubkey")})
        self.fetched = len(events)
        self.authors = {e["pubkey"] for e in events if e.get("pubkey")}
        for event in sorted(events, key=lambda e: e.get("created_at") or 0):
            item = self._raw_item(event, profiles)
            if item is not None:
                yield item

    def _profiles(self, pubkeys: set[str]) -> dict[str, str]:
        """kind 0 names by author; a missing or broken profile never breaks the run."""
        out: dict[str, str] = {}
        keys = sorted(pubkeys)
        for i in range(0, len(keys), PROFILE_CHUNK):
            chunk = keys[i:i + PROFILE_CHUNK]
            try:
                events, _ = self._client.req(self.relay, [
                    {"kinds": [0], "authors": chunk, "search": INCLUDE_SPAM}])
            except OSError:
                continue
            for event in events:
                try:
                    name = str(json.loads(event.get("content") or "{}").get("name") or "").strip()
                except ValueError:
                    name = ""
                if name:
                    out[str(event.get("pubkey"))] = name
        return out

    def _raw_item(self, event: dict, profiles: dict[str, str]) -> RawItem | None:
        eid = str(event.get("id") or "").lower()
        pk = str(event.get("pubkey") or "").lower()
        if len(eid) != 64 or not pk:
            return None  # not a usable event id; never invent one
        name = profiles.get(pk) or short_npub(pk)
        content = str(event.get("content") or "")
        title = None
        if int(event.get("kind") or 0) == KIND_LONGFORM:
            title = next((str(t[1]) for t in event.get("tags") or []
                          if isinstance(t, list) and len(t) >= 2 and t[0] == "title" and t[1]), None)
        if not title:
            for line in content.splitlines():
                if line.strip():
                    title = line.strip()[:140]
                    break
        return RawItem(
            id=f"nostr:{eid}",
            source=RawSource(id=f"nostr:{name}", kind="nostr", name=name,
                             url=None, content_public=True),
            author=name,
            title=title,
            url=f"https://njump.me/{eid}",
            published=int(event["created_at"]) if event.get("created_at") else None,
            body=content or None,
            verdict="noted" if self.into_stories else None,
            author_pubkey=pk,
        )
