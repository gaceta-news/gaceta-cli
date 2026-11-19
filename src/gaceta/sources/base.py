"""Source adapter contract. Every source (RSS, Nostr, a sqlite collector…) follows it.

An adapter yields RawItem records; core ingest owns labelling, upserts and run bookkeeping.
Adapters MUST be read-only against their upstream (e.g. sqlite opened with URI mode=ro).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterator, Protocol


@dataclass(frozen=True)
class RawSource:
    id: str
    kind: str
    name: str | None = None
    url: str | None = None
    content_public: bool = True


@dataclass(frozen=True)
class RawItem:
    id: str
    source: RawSource
    author: str | None
    title: str | None
    url: str | None
    published: int | None
    body: str | None
    # reader-side layer (optional; an RSS adapter has none)
    verdict: str | None = None        # normalised: told | noted | skip | pending
    note: str | None = None
    library_path: str | None = None
    # provenance for nostr items: the author's hex pubkey (set by nostr adapters;
    # lets later code tell followed from WoT-discovered authors without a name lookup)
    author_pubkey: str | None = None


class SourceAdapter(Protocol):
    name: str

    def items(self, since: int | None = None) -> Iterator[RawItem]: ...
