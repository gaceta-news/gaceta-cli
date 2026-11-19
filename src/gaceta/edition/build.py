"""Edition builder: DB → immutable edition JSON snapshot (docs/EDITION.md).

An edition is built from the reader's stories with last_seen within 14 days of
the edition date, filtered by max privacy. Score = ln(1+n_items) + 1.5*has_told
+ recency (1.0 for the last 2 days, decaying linearly to 0 at 14 days) +
0.5*has_written_summary, plus taste steering from the OWNER's reactions
(editor.taste: story weights, similarity transfer, author affinity; 'hide'
excludes). Each story carries score_reasons, short Spanish strings explaining
the score. portada = top 6 by score; sections (in order:
'lo-que-te-conte', reader-config [[sections]], 'otros') never repeat a story and
never reuse a portada story. Snapshots are immutable: rebuilding the same
(reader, date, max_privacy) refuses unless force=True.

Opt-in coverage_v1 caps the bonus from distinct linked sites rather than
counting every article, and surfaces a dated link on continuing front stories.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit

import re

from .. import conditions, config, i18n, privacy
from ..discovery import discovery_block
from ..editor import taste
from ..media import YOUTUBE_ID_RE
from ..sources.rss import source_identity as rss_source_identity
from ..transcripts import video_id

WINDOW_DAYS = 14
PORTADA_SIZE = 6
SECTION_CAP = 30  # deeper sections: hiding seen stories reveals more below
MAX_THUMBS = 4  # lead + up to 3 more
# section ids stay stable across languages (links, closed-state); titles follow ui lang
TOLD_SECTION = ("lo-que-te-conte", "section.told")
REST_SECTION = ("otros", "section.rest")

def spanish_date(day: date) -> str:
    """e.g. date(2026, 11, 17) → "martes 17 de noviembre de 2026" (kept for callers)."""
    return i18n.long_date("es", day)


def _iso(ts) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _safe_url(url) -> str | None:
    text = str(url or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return text


def _recency(last_seen: int, end: int) -> float:
    age_days = max(0.0, (end - last_seen) / 86400.0)
    if age_days <= 2.0:
        return 1.0
    return max(0.0, (WINDOW_DAYS - age_days) / (WINDOW_DAYS - 2.0))


def _link_keys(url) -> tuple[str, tuple[str, str, str]] | None:
    url = _safe_url(url)
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").removeprefix("www.").removeprefix("m.")
    if host == "youtu.be" or host in ("youtube.com", "music.youtube.com"):
        vid = video_id(url)
        if vid:
            return "youtube.com", ("youtube.com", vid, "")
        host = "youtube.com"
    if not host:
        return None
    query = "&".join(p for p in parts.query.split("&")
                     if not unquote_plus(p.partition("=")[0]).lower().startswith("utm_"))
    return host, (host, parts.path.rstrip("/"), query)


def _score_and_reasons(s: dict, end: int, *, sites: int | None = None,
                       lang: str = "es") -> tuple[float, list[str]]:
    """Base score + short reasons (in the reader's language) for each component."""
    def r(key: str, v: float, **kw) -> str:
        return i18n.t(lang, key, v=f"{v:+.1f}", **kw)
    coverage = math.log1p(s["n_items"] if sites is None else max(1, min(sites, 4)))
    told = 1.5 if s["n_told"] > 0 else 0.0
    rec = _recency(s["last_seen"], end)
    written = 0.5 if s["summary"] else 0.0
    reasons: list[str] = []
    if sites is None and s["n_items"] >= 2:
        reasons.append(r("reason.many_sources", coverage))
    if sites is not None and sites >= 2:
        reasons.append(r("reason.sites", coverage, n=sites))
    if told:
        reasons.append(r("reason.told", told))
    if rec >= 0.05:
        reasons.append(r("reason.recent", rec))
    if written:
        reasons.append(r("reason.written", written))
    return coverage + told + rec + written, reasons


def _story_rows(conn: sqlite3.Connection, reader_id: str, start: int, end: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.id, s.title, s.summary, s.why_it_matters, s.status, s.confidence,
               s.privacy, s.first_seen, s.last_seen,
               (SELECT COUNT(*) FROM story_items si WHERE si.story_id = s.id) AS n_items,
               (SELECT COUNT(*) FROM story_items si JOIN annotations a
                  ON a.item_id = si.item_id AND a.reader_id = s.reader_id
                  WHERE si.story_id = s.id AND a.verdict = 'told') AS n_told
        FROM stories s
        WHERE s.reader_id = ? AND s.last_seen IS NOT NULL
          AND s.last_seen >= ? AND s.last_seen <= ?
        """,
        (reader_id, start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def _rss_aliases(cfg: dict) -> dict[str, tuple[str, ...]]:
    """Resolve shared source ids to this reader's configured RSS slugs."""
    aliases: dict[str, list[str]] = {}
    rss = (cfg.get("sources") or {}).get("rss") or {}
    for feed in rss.get("feeds") or []:
        if not (feed.get("id") and feed.get("url")):
            continue
        try:
            sid, _ = rss_source_identity(feed)
        except ValueError:
            continue
        alias = f"rss:{str(feed['id']).lower()}"
        aliases.setdefault(sid, []).append(alias)
    return {sid: tuple(slugs) for sid, slugs in aliases.items()}


def _match_section(section: dict, story: dict, items_by_story: dict,
                   rss_aliases: dict[str, tuple[str, ...]] | None = None) -> bool:
    """A story matches when ANY member item hits a kind or source-id prefix, or a
    keyword appears in the story title/summary (all case-insensitive)."""
    kinds = {str(k).lower() for k in section.get("kinds") or []}
    prefixes = tuple(str(p).lower() for p in section.get("source_prefixes") or [])
    keywords = [str(k).lower() for k in section.get("keywords") or []]
    for it in items_by_story.get(story["id"], []):
        if kinds and str(it.get("source_kind") or "").lower() in kinds:
            return True
        sid = str(it.get("source_id") or "").lower()
        if prefixes:
            if sid.startswith(prefixes):
                return True
            # RSS-sourced video/provenance variants append :videos and/or :u.
            base = ":".join(sid.split(":", 2)[:2])
            if rss_aliases and any(a.startswith(prefixes) for a in rss_aliases.get(base, ())):
                return True
    if keywords:
        hay = f"{story['title'] or ''}\n{story['summary'] or ''}".lower()
        # word-start match: 'ia' must not hit 'histor-ia'; 'vulnerab' still matches 'vulnerability'
        if any(re.search(r"(?<!\w)" + re.escape(k), hay) for k in keywords):
            return True
    return False


def story_rev(item_ids, summary) -> str:
    """Short rev hash: changes when a member item is added/removed or the
    write-up (summary) changes. Renderers copy it to data-rev; the server
    stores it per person so a story only resurfaces when something changed."""
    h = hashlib.sha1()
    h.update("\x00".join(sorted(str(i) for i in item_ids)).encode("utf-8"))
    h.update(b"\x00")
    h.update(str(summary or "").encode("utf-8"))
    return h.hexdigest()[:12]


def next_update(built_at: str, schedule: dict | None):
    """Next scheduled build slot after built_at, as an ISO string.

    Reader config ``[schedule]``: every_hours (default 2), start_hour (6),
    end_hour (22), minute (30). Slots run start_hour, start_hour+every_hours, …
    while the hour is <= end_hour; after the last slot the next day's first
    slot is used. Returns None when there is no (valid) [schedule].
    """
    if not isinstance(schedule, dict):
        return None
    try:
        every = int(schedule.get("every_hours", 2))
        start_h = int(schedule.get("start_hour", 6))
        end_h = int(schedule.get("end_hour", 22))
        minute = int(schedule.get("minute", 30))
    except (TypeError, ValueError):
        return None
    if not (1 <= every <= 24 and 0 <= start_h <= 23 and 0 <= end_h <= 23
            and 0 <= minute <= 59 and start_h <= end_h):
        return None
    try:
        now = datetime.fromisoformat(str(built_at))
    except ValueError:
        return None
    hours = list(range(start_h, end_h + 1, every))
    day = now.date()
    for offset in (0, 1):
        d = day + timedelta(days=offset)
        for h in hours:
            slot = datetime(d.year, d.month, d.day, h, minute,
                            tzinfo=now.tzinfo or datetime.now().astimezone().tzinfo)
            if slot > now:
                return slot.isoformat(timespec="minutes")
    return None


def _story_media(conn: sqlite3.Connection, story_id: str) -> tuple[list[dict], dict[str, str]]:
    """Story media per docs/EDITION.md. Items in the same order as sources
    (oldest first); the lead is the YouTube of the highest-ranked (newest) item,
    else the first ok image. Also returns item_id → local thumb for sources."""
    rows = [
        dict(r) for r in conn.execute(
            """
            SELECT si.item_id, m.kind, m.status, m.local_path, m.src_url, m.video_id, i.title
            FROM story_items si
            JOIN items i ON i.id = si.item_id
            LEFT JOIN media m ON m.item_id = si.item_id
            WHERE si.story_id = ?
            ORDER BY i.published IS NULL, i.published, i.id
            """,
            (story_id,),
        )
    ]
    youtube = [r for r in rows if r["kind"] == "youtube" and r["status"] == "ok"
               and r["local_path"] and r["video_id"]
               and YOUTUBE_ID_RE.fullmatch(str(r["video_id"]))]
    images = [r for r in rows if r["kind"] == "image" and r["status"] == "ok" and r["local_path"]]
    videos = [r for r in rows if r["kind"] == "video" and r["status"] == "ok"
              and str(r["src_url"] or "").startswith("https://")]

    lead = youtube[-1] if youtube else (images[0] if images else None)
    media: list[dict] = []

    def entry(r: dict, mtype: str) -> dict:
        e = {"type": mtype, "alt": r.get("title") or "", "item_id": r["item_id"]}
        if mtype == "video":
            e["src"] = r["src_url"]
        else:
            e["src"] = f"media/{r['local_path']}"
            if mtype == "youtube":
                e["video_id"] = r["video_id"]
        return e

    if lead:
        media.append(entry(lead, "youtube" if lead in youtube else "image"))
    for r in rows:
        if len(media) >= MAX_THUMBS:
            break
        if r is not lead and (r in youtube or r in images):
            media.append(entry(r, "youtube" if r in youtube else "image"))
    media.extend(entry(r, "video") for r in videos if len(media) < MAX_THUMBS + len(videos))

    thumbs = {}
    for r in rows:
        if r["status"] == "ok" and r["local_path"]:
            thumbs[r["item_id"]] = f"media/{r['local_path']}"
    return media, thumbs


SETTLED_AFTER_DAYS = 4


def edition_status(stored: str | None, n_items: int, last_seen: int | None, end: int) -> str:
    """Status shown in the edition. 'corrected' (set by the editor) always wins.
    'developing' = the thread is still moving: 2+ sources and a new one in the last
    SETTLED_AFTER_DAYS days. Everything else is 'settled' (no badge)."""
    if stored == "corrected":
        return "corrected"
    if n_items >= 2 and last_seen and end - last_seen <= SETTLED_AFTER_DAYS * 86400:
        return "developing"
    return "settled"


def _story_entry(conn: sqlite3.Connection, story: dict, end: int | None = None) -> dict:
    reasons = story.get("_reasons") or []
    sources = []
    media, thumbs = _story_media(conn, story["id"])
    member_ids = []
    for r in conn.execute(
        """
        SELECT i.id AS item_id, i.title, i.url, i.author, s.kind, i.published, si.role,
               i.ingested_at
        FROM story_items si
        JOIN items i ON i.id = si.item_id
        LEFT JOIN sources s ON s.id = i.source_id
        WHERE si.story_id = ?
        ORDER BY i.published IS NULL, i.published, i.id
        """,
        (story["id"],),
    ):
        url = _safe_url(r["url"])
        if not url:
            continue
        source = {
            "title": r["title"], "url": url, "author": r["author"],
            "kind": r["kind"], "published": _iso(r["published"]), "role": r["role"],
            "added": r["ingested_at"],  # unix; lets the page flag sources new since 'Visto'
        }
        if r["item_id"] in thumbs:
            source["thumb"] = thumbs[r["item_id"]]
        sources.append(source)
    corrections = [
        {"said": r["said"], "now": r["now"], "at": _iso(r["at"])}
        for r in conn.execute(
            "SELECT said, now, at FROM corrections WHERE story_id = ? ORDER BY at", (story["id"],)
        )
    ]
    # Every written version, oldest first: what the reader read when they marked
    # it seen, and what each rewrite added ('Desde que la viste'). Times are unix
    # so the page can compare them with the server's seen.at.
    history = [
        {"version": r["version"], "at": r["written_at"], "summary": r["summary"],
         "what_changed": r["what_changed"]}
        for r in conn.execute(
            "SELECT version, written_at, summary, what_changed FROM story_versions "
            "WHERE story_id = ? ORDER BY version", (story["id"],)
        )
    ]
    entry = {
        "title": story["title"], "summary": story["summary"],
        "rev": story_rev(member_ids, story["summary"]),
        "why_it_matters": story["why_it_matters"],
        "status": edition_status(story["status"], story.get("n_items") or 0, story["last_seen"],
                                 end if end is not None else int(time.time())),
        "confidence": story["confidence"],
        "first_seen": _iso(story["first_seen"]), "last_seen": _iso(story["last_seen"]),
        "privacy": story["privacy"],
        "sources": sources, "corrections": corrections,
        "score_reasons": reasons,
    }
    if len(history) > 1:
        entry["history"] = history
    if media:
        entry["media"] = media
    return entry


def _new_coverage(story: dict, start: int, end: int) -> str | None:
    """Newest link published this date in an ongoing story.

    Extra coverage, not a claim that the facts changed or the writer used it.
    Skip unknown publication dates and links already present before this date.
    """
    older = set()
    recent = []
    for source in story["sources"]:
        keys = _link_keys(source["url"])
        if not keys or not source["published"]:
            continue
        published = datetime.fromisoformat(source["published"]).timestamp()
        if published < start:
            older.add(keys[1])
        elif published <= end:
            recent.append((published, source["url"], keys[1]))
    if not older:
        return None  # don't add a redundant line to a story new today
    return next((url for _, url, key in sorted(recent, reverse=True) if key not in older), None)


def snapshot_path(data_dir: Path, reader_id: str, date_str: str, max_privacy: str) -> Path:
    return data_dir / "editions" / reader_id / f"{date_str}-{max_privacy}.json"


def build_edition(conn: sqlite3.Connection, reader_id: str, cfg: dict, *,
                  date_str: str | None = None, max_privacy: str = "private",
                  force: bool = False, data_dir: Path | None = None,
                  now: int | None = None) -> dict:
    """Build the edition and write its immutable snapshot + editions row.
    Returns the edition dict (also the path is under result? no — see cmd)."""
    if not privacy.allowed(max_privacy, "private"):
        raise ValueError(f"unknown max_privacy {max_privacy!r}")
    built_ts = int(now if now is not None else time.time())
    day = date.fromisoformat(date_str) if date_str else datetime.fromtimestamp(built_ts).date()
    data = Path(data_dir) if data_dir else config.data_dir()

    lang = i18n.reader_lang(cfg)
    edition_cfg = cfg.get("edition") or {}
    written_only = edition_cfg.get("written_only", False)
    coverage_v1 = edition_cfg.get("coverage_v1", False)
    day_start = int(datetime.combine(day, day_time.min).timestamp())
    # The window is the LOCAL calendar day (same calendar as the headline and
    # the scheduled release). A UTC-midnight end sits before `now` after 19:00
    # in UTC-5 and silently drops stories ingested that evening (#time-hole).
    end = int(datetime.combine(day + timedelta(days=1), day_time.min).timestamp()) - 1
    start = end - (WINDOW_DAYS - 1) * 86400
    rows = [s for s in _story_rows(conn, reader_id, start, end)
            if privacy.allowed(s["privacy"], max_privacy) and s["n_items"] > 0
            and (not written_only or s["summary"])]
    items_by_story: dict[str, list[dict]] = {}
    for s in rows:
        items_by_story[s["id"]] = [
            dict(r) for r in conn.execute(
                "SELECT si.item_id, i.source_id, i.origin, i.url, s2.kind AS source_kind FROM story_items si "
                "JOIN items i ON i.id = si.item_id "
                "LEFT JOIN sources s2 ON s2.id = i.source_id WHERE si.story_id = ?",
                (s["id"],),
            )
        ]
    for s in rows:
        sites = None
        if coverage_v1:
            sites = len({keys[0] for item in items_by_story[s["id"]]
                         if (keys := _link_keys(item["url"]))})
        base, reasons = _score_and_reasons(s, end, sites=sites, lang=lang)
        s["_score"], s["_base"], s["_reasons"] = base, base, reasons
    # taste steering: the owner's reactions only (editor.taste); hidden stories drop out
    adj = taste.adjustments(conn, reader_id, cfg, items_by_story)
    if adj.hidden:
        rows = [s for s in rows if s["id"] not in adj.hidden]
    for s in rows:
        if s["id"] in adj.delta:
            s["_score"] += adj.delta[s["id"]]
            s["_reasons"] = s["_reasons"] + adj.reasons.get(s["id"], [])
    rows.sort(key=lambda s: s["_score"], reverse=True)

    portada = [s["id"] for s in rows[:PORTADA_SIZE]]
    # instrument (roadmap 11c): how much taste moved the portada vs base score
    # alone. Both rankings are over the same post-hide rows; the base ranking
    # re-sorts by the score each story had before the owner's reactions.
    base_top = {s["id"] for s in
                sorted(rows, key=lambda s: s["_base"], reverse=True)[:PORTADA_SIZE]}
    portada_by_taste = sum(1 for sid in portada if sid not in base_top)
    used = set(portada)
    sections: list[dict] = []

    def take(pred) -> list[str]:
        picked = [s["id"] for s in rows if s["id"] not in used and pred(s)][:SECTION_CAP]
        used.update(picked)
        return picked

    sections.append({"id": TOLD_SECTION[0], "title": i18n.t(lang, TOLD_SECTION[1]),
                     "stories": take(lambda s: s["n_told"] > 0)})
    rss_aliases = _rss_aliases(cfg)
    for sec in cfg.get("sections") or []:
        sec = dict(sec)
        picked = take(lambda s, sec=sec: _match_section(sec, s, items_by_story, rss_aliases))
        sections.append({"id": str(sec.get("id") or "section"), "title": sec.get("title"),
                         "stories": picked})
    sections.append({"id": REST_SECTION[0], "title": i18n.t(lang, REST_SECTION[1]),
                     "stories": take(lambda s: True)})

    stories = {}
    for s in rows:
        if s["id"] in used or s["id"] in portada:
            entry = _story_entry(conn, s, end)
            if coverage_v1 and s["id"] in portada:
                update_url = _new_coverage(entry, day_start, min(end, built_ts))
                if update_url:
                    entry["new_coverage"] = update_url
            stories[s["id"]] = entry

    edition = {
        "version": 1,
        "reader": {"id": reader_id, "name": cfg.get("name") or reader_id,
                   "langs": [l.strip() for l in str(cfg.get("langs") or "en").split(",") if l.strip()],
                   "ui_lang": lang,
                   **({"masthead": str(cfg["masthead"])} if cfg.get("masthead") else {})},
        "date": day.isoformat(),
        "built_at": datetime.fromtimestamp(
            built_ts
        ).astimezone().isoformat(timespec="seconds"),
        "max_privacy": max_privacy,
        "portada": portada,
        "sections": [s for s in sections if s["stories"] or s["id"] != REST_SECTION[0]],
        "stories": stories,
    }
    nu = next_update(edition["built_at"], cfg.get("schedule"))
    if nu:
        edition["next_update"] = nu
    # folio + instrument (roadmap 11c): honest furniture over the included
    # stories only — code is a fingerprint of the input, counts of the day.
    member_ids = sorted({str(it["item_id"]) for sid in stories
                         for it in items_by_story.get(sid, ())})
    voices: set[str] = set()
    for i in range(0, len(member_ids), 500):
        chunk = member_ids[i:i + 500]
        for r in conn.execute(
            f"SELECT DISTINCT author FROM items WHERE author IS NOT NULL "
            f"AND id IN ({','.join('?' * len(chunk))})", chunk,
        ):
            a = str(r["author"]).strip()
            if a:
                voices.add(a)
    h = hashlib.sha1("\x00".join(member_ids).encode("utf-8"))
    edition["folio"] = {
        "code": h.hexdigest()[:6].upper(),
        "date_label": i18n.long_date(lang, day),
        "lang": lang,
        "window_end": datetime.fromtimestamp(built_ts).astimezone().strftime("%H:%M"),
        "stories_total": len(stories),
        "items_total": len(member_ids),
        "voices": len(voices),
        "portada": len(portada),
    }
    edition["instrument"] = {"portada_by_taste": portada_by_taste,
                             "portada": len(portada)}
    # discovery (roadmap 6b): trials + suggestions from this edition's stories
    disc = discovery_block(
        conn, reader_id, cfg, start, max_privacy=max_privacy,
        story_scores={s["id"]: s["_score"] for s in rows if s["id"] in stories},
    )
    if disc:
        edition["discovery"] = disc
    # condiciones: moving numbers captured at build time, never live (conditions.py)
    boxes = conditions.collect(cfg, max_privacy=max_privacy,
                               now=datetime.fromtimestamp(built_ts).astimezone())
    if boxes:
        edition["conditions"] = boxes

    path = snapshot_path(data, reader_id, day.isoformat(), max_privacy)
    if path.exists() and not force:
        raise FileExistsError(f"edition snapshot already exists (use --force): {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(edition, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    conn.execute(
        "INSERT INTO editions (reader_id, date, max_privacy, path, built_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(reader_id, date, max_privacy) DO UPDATE SET "
        "path = excluded.path, built_at = excluded.built_at",
        (reader_id, day.isoformat(), max_privacy, str(path),
         built_ts),
    )
    conn.commit()
    return edition
