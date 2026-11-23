"""Source discovery for the edition (roadmap 6b, DECISIONS 11/13).

Two kinds of candidates, both shown in the paper's 'Fuentes nuevas' section:

- trials: sources the collector is trying out (collector rss ids
  'feed:trial-…', YouTube authors '[trial] …'), with the edition's stories
  that carry their items.
- suggestions: candidates the reader does not follow yet — nostr accounts the
  reader's follows keep mentioning (adapter hook ``discovery_candidates``)
  and sites that publish: >=2 distinct article-shaped links (a worded slug or
  a dated path), cited by >=2 independent sources (source+author) in this
  edition's stories, not net-disliked by the reader. Media/blob files, hash
  or nostr-id paths, bare homepages and app pages never count as citations;
  subdomains like media./assets./cdn. fold into the site.

Only items of stories allowed at the edition's max privacy are considered
(same filter as edition.build). Entries with a recorded decision (schema v7
``source_decisions``) do not come back, except 'later' older than 7 days.
gaceta never edits a collector: decisions leave through `gaceta sources pending`.
"""
from __future__ import annotations

import re
import sqlite3
import time
from urllib.parse import unquote, urlsplit

from . import i18n, privacy

LATER_DAYS = 7
MAX_STORIES_PER_TRIAL = 3
MIN_ARTICLE_LINKS = 2      # distinct article-shaped URLs on the site
MIN_CITING_SOURCES = 2     # independent (source, author) pairs citing it
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


# file links: a citation of a picture/clip/archive is never a citation of a source
_MEDIA_EXT = {
    "jpg", "jpeg", "png", "gif", "webp", "avif", "heic", "svg", "bmp", "ico",
    "mp4", "mov", "webm", "mkv", "m4v", "avi", "mp3", "m4a", "ogg", "opus", "wav", "flac",
    "pdf", "zip", "gz", "apk", "dmg", "exe", "json", "xml", "txt", "csv", "js", "css",
}
_HASHY_RE = re.compile(r"[0-9a-f]{32,}", re.I)          # blob/content-addressed paths
_NOSTR_ID_RE = re.compile(r"^(npub1|nprofile1|nevent1|note1|naddr1)", re.I)
_DATE_PATH_RE = re.compile(r"/(19|20)\d\d/(0?[1-9]|1[0-2])?")
# leading labels that are a site's plumbing, not a separate source
_PLUMBING_LABELS = {
    "www", "m", "mobile", "amp", "media", "assets", "cdn", "static", "img", "images", "image",
    "i", "files", "file", "video", "videos", "v", "gifs", "uploads", "content", "api",
}


def _link_kind(url: object) -> str:
    """'article' (worded slug or dated path) | 'page' | 'root' | 'media' | 'junk'."""
    try:
        text = str(url or "").translate({ord(c): None for c in _INVISIBLE})
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "junk"
    if "." not in host or host.rsplit(".", 1)[-1].isdigit():
        return "junk"  # 'https://coinjoin', bare IPs
    path = parts.path or ""
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "root"
    last = segs[-1]
    stem, dot, ext = last.rpartition(".")
    if dot and ext.lower() in _MEDIA_EXT:
        return "media"
    if any(_HASHY_RE.search(s.replace("-", "")) or _NOSTR_ID_RE.match(s) for s in segs):
        return "media"  # content hashes, UUIDs, nostr ids: app/blob addresses
    if _DATE_PATH_RE.search(path + "/"):
        return "article"
    slug = unquote(stem if dot else last)
    words = [t for t in re.split(r"[-_+\s]+", slug) if len(t) >= 2 and t.isalpha()]
    if len(words) >= 3:
        return "article"  # 'the-river-came-back'; never a wallet address or an id
    return "page"


def _site(host: str) -> str:
    """'assets.science.nasa.gov' -> 'science.nasa.gov'; keeps at least two labels."""
    labels = host.split(".")
    while len(labels) > 2 and (labels[0] in _PLUMBING_LABELS
                               or (labels[0][:-1] in _PLUMBING_LABELS and labels[0][-1:].isdigit())):
        labels = labels[1:]
    return ".".join(labels)


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
        member_ids = [s["id"] for s in stories]
        if not member_ids:
            continue
        ph = ",".join("?" * len(member_ids))
        # only this reader's paper counts: a household collector's trial with no
        # items in this reader's stories is someone else's trial
        n_items = conn.execute(
            "SELECT COUNT(DISTINCT i.id) FROM story_items si JOIN items i ON i.id = si.item_id "
            f"WHERE i.source_id = ? AND si.story_id IN ({ph}) "
            "AND (i.published IS NULL OR i.published >= ?)",
            (key, *member_ids, since),
        ).fetchone()[0]
        if n_items <= 0:
            continue
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
    (nostr mentions) + publishing sites: >=MIN_ARTICLE_LINKS article links
    cited by >=MIN_CITING_SOURCES independent sources in this edition's
    stories, excluding existing source sites, big platforms, media hosts,
    same-author substacks and sites the reader's verdicts push away."""
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
    existing_sites = {_site(h) for h in existing_hosts} | existing_hosts
    stories = _allowed_stories(conn, reader_id, since, max_privacy)
    taste = _story_taste(conn, reader_id, [s["id"] for s in stories])
    cited: dict[str, dict] = {}
    for s in stories:
        for it in conn.execute(
            "SELECT i.id, i.url, i.body, i.author, i.source_id, src.kind AS src_kind "
            "FROM story_items si "
            "JOIN items i ON i.id = si.item_id LEFT JOIN sources src ON src.id = i.source_id "
            "WHERE si.story_id = ?",
            (s["id"],),
        ):
            body = str(it["body"] or "").translate({ord(c): None for c in _INVISIBLE})
            # video descriptions are mostly sponsors/affiliate/tip links: only text
            # sources' bodies count as citations
            urls = [it["url"]] + ([] if it["src_kind"] == "youtube" else _LINK_RE.findall(body))
            citer = (str(it["source_id"] or ""), _norm(it["author"]))
            for url in urls:
                host = _host(url)
                if not host:
                    continue
                kind = _link_kind(url)
                if kind in ("media", "junk"):
                    continue  # a picture or a blob is not a citation of a source
                site = _site(host)
                d = cited.setdefault(site, {"articles": set(), "stories": set(), "citers": set(),
                                            "url": str(url)})
                d["citers"].add(citer)
                d["stories"].add(s["id"])
                if kind == "article":
                    d["articles"].add(str(url).split("#", 1)[0].rstrip("/"))
    domain_out: list[dict] = []
    for site, d in cited.items():
        key = f"host:{site}"
        if key in skip:
            continue
        if len(d["articles"]) < MIN_ARTICLE_LINKS or len(d["citers"]) < MIN_CITING_SOURCES:
            continue  # needs published pieces AND more than one voice pointing at them
        if site in existing_sites or _is_big_platform(site) or _is_media_host(site):
            continue
        if site == "substack.com" or site.endswith(".substack.com"):
            # substack only when it is NOT an author already followed
            token = _substack_token(d["url"])
            if token is None or _norm(token) in existing_names:
                continue
        liking = sum(taste.get(sid, 0.0) for sid in d["stories"])
        if liking < 0:
            continue  # cited mostly in stories the reader pushed away
        domain_out.append({
            "key": key, "name": site, "kind": "site",
            "reason": i18n.t(lang, "discovery.cited", len(d["stories"])),
            "url": f"https://{site}",
            "_rank": (liking, len(d["citers"]), len(d["stories"])),
        })
    domain_out.sort(key=lambda e: (tuple(-x for x in e["_rank"]), e["key"]))
    domain_out = domain_out[:limit]  # keep the section calm; best-cited first
    for e in domain_out:
        del e["_rank"]
    return out + domain_out


def _story_taste(conn: sqlite3.Connection, reader_id: str, story_ids: list[str]) -> dict[str, float]:
    """Net reaction weight per story (taste.KIND_WEIGHTS; 'open' left out: it is
    attention, not a verdict). Missing reactions table → no signal."""
    from .editor.taste import KIND_WEIGHTS
    out: dict[str, float] = {}
    if not story_ids:
        return out
    ph = ",".join("?" * len(story_ids))
    try:
        rows = conn.execute(
            f"SELECT story_id, kind FROM reactions WHERE reader_id = ? AND story_id IN ({ph})",
            (reader_id, *story_ids),
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        if r["kind"] == "open":
            continue
        out[r["story_id"]] = out.get(r["story_id"], 0.0) + KIND_WEIGHTS.get(r["kind"], 0.0)
    return out


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
