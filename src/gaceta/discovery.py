"""Source discovery for the edition (roadmap 6b, DECISIONS 11/13).

Two kinds of candidates, both shown in the paper's 'Fuentes nuevas' section:

- trials: sources the collector is trying out (collector rss ids
  'feed:trial-…', YouTube authors '[trial] …'), with the edition's stories
  that carry their items.
- suggestions: candidates the reader does not follow yet — nostr accounts the
  reader's follows keep mentioning (adapter hook ``discovery_candidates``)
  and domains cited by >=3 distinct items of this edition's stories.

Only items of stories allowed at the edition's max privacy are considered
(same filter as edition.build). Entries with a recorded decision (schema v7
``source_decisions``) do not come back, except 'later' older than 7 days.
gaceta never edits a collector: decisions leave through `gaceta sources pending`.
"""
from __future__ import annotations

import re
import sqlite3
import time
from urllib.parse import urlsplit

from . import i18n, privacy

LATER_DAYS = 7
MAX_STORIES_PER_TRIAL = 3
MIN_CITED_ITEMS = 3
DEFAULT_LIMIT = 8

TRIAL_FEED_PREFIX = "feed:trial-"
TRIAL_YT_PREFIX = "[trial]"

# Hosts that are platforms, not sources: a citation there never suggests one.
BIG_PLATFORMS = {
    "youtube.com", "youtu.be", "m.youtube.com", "x.com", "twitter.com",
    "github.com", "google.com", "wikipedia.org", "t.co", "bit.ly",
    "primal.net", "njump.me", "apple.com", "spotify.com", "amazon.com",
    "reddit.com", "medium.com", "substack.com",
    # same category, found in the first real-data run: social platforms and a
    # redirect shortener are never the source themselves
    "instagram.com", "facebook.com", "smarturl.it", "paypal.me", "paypal.com", "amzn.to",
    "geni.us", "cash.app", "goo.gl", "patreon.com", "linktr.ee",
    "tiktok.com", "discord.gg", "discord.com", "t.me", "buymeacoffee.com", "ko-fi.com",
}
_INVISIBLE = "\u200b\u200c\u200d\ufeff"  # zero-width chars inside copied URLs
_LINK_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")


def _clean_name(raw: object) -> str:
    """'trial-la-miudad' -> 'La Miudad'; '[trial] Zorbo' -> 'Zorbo'."""
    name = str(raw or "").strip()
    if name.lower().startswith("[trial]"):
        name = name[len("[trial]"):].strip(" -")
    elif name.lower().startswith("trial-"):
        name = name[len("trial-"):]
    words = name.replace("-", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words if w)


def _host(url: object) -> str:
    try:
        text = str(url or "").translate({ord(c): None for c in _INVISIBLE})
        return (urlsplit(text).hostname or "").removeprefix("www.").lower()
    except ValueError:
        return ""


# nostr media/blob hosts: they host files for many authors, they are not a source
_MEDIA_HOST_SUFFIXES = (".blossom.band", "nostr.build", "void.cat", "nostrcheck.me", "satellite.earth")


def _is_media_host(host: str) -> bool:
    return host.startswith("npub1") or any(
        host == s.lstrip(".") or host.endswith(s if s.startswith(".") else "." + s) or host == s
        for s in _MEDIA_HOST_SUFFIXES)


def _is_big_platform(host: str) -> bool:
    if host == "substack.com" or host.endswith(".substack.com"):
        return False  # substack is decided by the same-author rule, not here
    if host in BIG_PLATFORMS:
        return True
    return any(host.endswith("." + p) for p in BIG_PLATFORMS)


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(token or "").lower())


def _substack_token(url: str) -> str | None:
    """Author token of a substack URL ('zorbo.substack.com' / substack.com/@zorbo)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host.endswith(".substack.com") and host != "substack.com":
        return host[: -len(".substack.com")]
    if host == "substack.com":
        m = re.match(r"^/@([^/]+)", parts.path or "")
        if m:
            return m.group(1)
    return None


def decided_keys(conn: sqlite3.Connection, reader_id: str, now: float | None = None) -> set[str]:
    """Keys that must not resurface: any decision, except 'later' older than LATER_DAYS."""
    now = time.time() if now is None else now
    try:
        rows = conn.execute(
            "SELECT source_key, decision, at FROM source_decisions WHERE reader_id = ?",
            (reader_id,),
        ).fetchall()
    except sqlite3.OperationalError:  # pre-v7 db
        return set()
    skip: set[str] = set()
    for r in rows:
        if r["decision"] == "later" and int(r["at"] or 0) <= now - LATER_DAYS * 86400:
            continue
        skip.add(r["source_key"])
    return skip


def _allowed_stories(conn: sqlite3.Connection, reader_id: str, since: int,
                     max_privacy: str) -> list[sqlite3.Row]:
    return [
        r for r in conn.execute(
            "SELECT id, last_seen, privacy FROM stories "
            "WHERE reader_id = ? AND last_seen IS NOT NULL AND last_seen >= ?",
            (reader_id, since),
        )
        if privacy.allowed(r["privacy"], max_privacy)
    ]


def trial_sources(conn: sqlite3.Connection, reader_id: str, since: int, *,
                  max_privacy: str = "private",
                  story_scores: dict[str, float] | None = None,
                  now: float | None = None,
                  names: dict[str, str] | None = None) -> list[dict]:
    """Trial sources with items in the window, as discovery entries.

    ``story_scores`` (edition.build's scores) ranks each trial's stories; when
    omitted stories rank by last_seen. Trials the reader already decided on do
    not come back (except 'later' older than LATER_DAYS).
    """
    rows = conn.execute(
        "SELECT id, kind, name FROM sources "
        "WHERE id LIKE ? OR name LIKE ?",
        (TRIAL_FEED_PREFIX + "%", TRIAL_YT_PREFIX + "%"),
    ).fetchall()
    if not rows:
        return []
    skip = decided_keys(conn, reader_id, now)
    stories = _allowed_stories(conn, reader_id, since, max_privacy)
    if story_scores is not None:
        keep = set(story_scores)
        stories = [s for s in stories if s["id"] in keep]
    rank = (lambda s: story_scores.get(s["id"], 0.0)) if story_scores is not None else (
        lambda s: int(s["last_seen"] or 0))
    out: list[dict] = []
    for r in rows:
        key = r["id"]
        if key in skip:
            continue
        n_items = conn.execute(
            "SELECT COUNT(*) FROM items WHERE source_id = ? "
            "AND (published IS NULL OR published >= ?)",
            (key, since),
        ).fetchone()[0]
        if n_items <= 0:
            continue
        member_ids = [s["id"] for s in stories]
        story_ids: list[str] = []
        if member_ids:
            ph = ",".join("?" * len(member_ids))
            hits = {
                h["story_id"] for h in conn.execute(
                    "SELECT DISTINCT si.story_id FROM story_items si "
                    "JOIN items i ON i.id = si.item_id "
                    f"WHERE i.source_id = ? AND si.story_id IN ({ph})",
                    (key, *member_ids),
                )
            }
            story_ids = sorted(hits, key=lambda sid: -rank(
                next(s for s in stories if s["id"] == sid)))[:MAX_STORIES_PER_TRIAL]
        out.append({
            "key": key,
            "name": (names or {}).get(str(r["name"] or ""))
                    or (names or {}).get(key) or _clean_name(r["name"] or key),
            "kind": r["kind"],
            "n_items": int(n_items),
            "story_ids": story_ids,
        })
    out.sort(key=lambda e: (-e["n_items"], e["key"]))
    return out


def _existing_hosts_and_names(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """Hosts of items of every existing source + normalised names/authors."""
    hosts: set[str] = set()
    names: set[str] = set()
    for r in conn.execute("SELECT id, name, url FROM sources"):
        names.add(_norm(r["name"]))
        h = _host(r["url"])
        if h:
            hosts.add(h)
    for r in conn.execute("SELECT DISTINCT source_id, url FROM items WHERE url IS NOT NULL"):
        h = _host(r["url"])
        if h:
            hosts.add(h)
    for r in conn.execute("SELECT DISTINCT author FROM items WHERE author IS NOT NULL"):
        names.add(_norm(r["author"]))
    return hosts, names


def suggestions(conn: sqlite3.Connection, reader_id: str, since: int, *,
                adapters=(), max_privacy: str = "private",
                now: float | None = None, limit: int = DEFAULT_LIMIT,
                lang: str = "es") -> list[dict]:
    """Candidate sources the reader does not follow: adapter candidates
    (nostr mentions) + domains cited by >=MIN_CITED_ITEMS items of this
    edition's stories, excluding existing source hosts, big platforms and
    same-author substacks."""
    skip = decided_keys(conn, reader_id, now)
    out: list[dict] = []

    for adapter in adapters:
        hook = getattr(adapter, "discovery_candidates", None)
        if hook is None:
            continue
        for c in hook(limit=limit) or []:
            key = str(c.get("key") or "")
            if not key or key in skip or any(e["key"] == key for e in out):
                continue
            if not str(c.get("name") or "").strip():
                continue  # an unnamed account (bare pubkey) tells the reader nothing
            out.append({
                "key": key, "name": str(c.get("name") or key),
                "kind": str(c.get("kind") or "other"),
                "reason": str(c.get("reason") or ""),
                "url": str(c.get("url") or ""),
            })

    existing_hosts, existing_names = _existing_hosts_and_names(conn)
    stories = _allowed_stories(conn, reader_id, since, max_privacy)
    cited: dict[str, dict] = {}
    for s in stories:
        for it in conn.execute(
            "SELECT i.id, i.url, i.body, i.author, src.kind AS src_kind FROM story_items si "
            "JOIN items i ON i.id = si.item_id LEFT JOIN sources src ON src.id = i.source_id "
            "WHERE si.story_id = ?",
            (s["id"],),
        ):
            body = str(it["body"] or "").translate({ord(c): None for c in _INVISIBLE})
            # video descriptions are mostly sponsors/affiliate/tip links: only text
            # sources' bodies count as citations
            urls = [it["url"]] + ([] if it["src_kind"] == "youtube" else _LINK_RE.findall(body))
            seen_hosts: set[str] = set()
            for url in urls:
                host = _host(url)
                if not host or host in seen_hosts:
                    continue
                seen_hosts.add(host)
                d = cited.setdefault(host, {"items": set(), "stories": set(), "authors": set(),
                                            "url": str(url)})
                d["items"].add(it["id"])
                d["authors"].add(_norm(it["author"]))
                d["stories"].add(s["id"])
    domain_out: list[dict] = []
    for host, d in cited.items():
        key = f"host:{host}"
        if key in skip or len(d["items"]) < MIN_CITED_ITEMS:
            continue
        if host in existing_hosts or _is_big_platform(host) or _is_media_host(host):
            continue
        if host == "substack.com" or host.endswith(".substack.com"):
            # substack only when it is NOT an author already followed
            token = _substack_token(d["url"])
            if token is None or _norm(token) in existing_names:
                continue
        domain_out.append({
            "key": key, "name": host, "kind": "site",
            "reason": i18n.t(lang, "discovery.cited", len(d["stories"])),
            "url": f"https://{host}",
            "_n": len(d["stories"]),
        })
    domain_out.sort(key=lambda e: (-e["_n"], e["key"]))
    domain_out = domain_out[:limit]  # keep the section calm; best-cited first
    for e in domain_out:
        del e["_n"]
    return out + domain_out


def discovery_block(conn: sqlite3.Connection, reader_id: str, cfg: dict, since: int, *,
                    max_privacy: str = "private",
                    story_scores: dict[str, float] | None = None,
                    adapters=None, now: float | None = None,
                    limit: int = DEFAULT_LIMIT) -> dict | None:
    """The edition's top-level ``discovery`` block, or None when empty."""
    if not adapters:
        # adapters configured in the reader's [sources.*] that expose the
        # discovery_candidates hook (plugins; never a network call at build time)
        from .sources import discovery_adapters
        adapters = discovery_adapters(cfg)
    trials = trial_sources(conn, reader_id, since, max_privacy=max_privacy,
                           story_scores=story_scores, now=now,
                           names=(cfg.get("discovery") or {}).get("names") or {})
    sugg = suggestions(conn, reader_id, since, adapters=adapters,
                       max_privacy=max_privacy, now=now, limit=limit,
                       lang=i18n.reader_lang(cfg))
    block: dict = {}
    if trials:
        block["trials"] = trials
    if sugg:
        block["suggestions"] = sugg
    return block or None
