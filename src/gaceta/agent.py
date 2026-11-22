"""Read-only public agent surface: versioned, public-only edition/story exports.

Agent sessions (an external LLM agent in Claude Code, Hermes or any harness)
never touch gaceta's private database. They read ONE artifact: a versioned,
public-only export file derived from a PUBLIC edition snapshot. Two sides:

OWNER (trusted, runs wherever the data lives):
- ``build_export`` / ``export_for_date`` derive the export from the day's
  public edition snapshot and re-check every privacy rule (a snapshot that is
  not public, or contains any story that is not strictly public, refuses
  outright; a story with ``why_it_matters`` is private by rule and likewise
  refuses). When given the database they also attach the paper's own version
  evidence, from the existing tables only: the public build trace
  (``editions``), each story's write-up history (``story_versions``) with the
  public sources that joined between versions, corrections with their
  evidence item, and coverage that no write-up has assessed yet. Nothing
  about any person rides along: no ``seen`` marks, no reactions, no taste —
  and a story labeled public whose member items are not all public refuses
  (fail closed). The export therefore never opens the ``seen`` table: the
  public bundle is the same bytes whatever anyone has read.
- ``derive_receipt`` (still owner side, but opt-in and consent-shaped) hands
  ONE person their own mark back: the explicit (reader, person, story) row
  from ``seen`` maps to the write-up version its rev actually pins —
  verified against the paper's write-up history (a version is claimed only
  when the rev reproduces the paper's own hash of that version's write-up)
  or honestly ``unknown`` — and ``receipt_baseline`` renders it as the
  baseline the existing ``since`` path already accepts. gaceta writes
  nothing (stdout is the receipt); no raw name, no timestamp, not even the
  rev ride along; another person's mark, a non-public story or a foreign
  reader never answers; and the receipt never joins the public export.

AGENT (untrusted, sandboxed):
- ``load_export`` + ``edition_view`` / ``story_view`` read that one file and
  nothing else: no database, no reader config, no writes, no marks. Every
  check fails closed: unknown kind/schema version, reader mismatch, any
  non-public content, a non-http(s) source url, a stale or future ``built_at``,
  an unknown story id — refuse with an error, never emit partial data.
- ``load_baseline`` + ``since_view`` answer the launch question ("what
  changed since I last read?") from the caller's OWN memory, a baseline file
  the agent session writes itself: which write-up version of each story it
  last saw, the edition date it last read when it states one, and where
  each of those facts came from (provenance: user statement, verified
  local receipt, or unknown). The answer classifies each story against
  the export's evidence — known version vs unknown, intervening editions
  as publication chronology only (never a claim that any edition was
  skipped or unread), material change (an evidence-supported proposal
  with the public source excerpt/reference behind it), the paper's public
  facts on record when the caller has no last-read record, or no
  supported update — and says 'unknown' out loud whenever the evidence
  cannot support a claim. No invented facts, ever: a reading date is
  never derived from a version, a trace time or an edition date, and
  nothing is claimed about delivery or access cycles.

Three clocks exist in this data and they are never confused: a source's
``published`` (when the OUTLET published it), the paper's trace times
(``added``/``written_at``/``first_seen``/``last_seen`` — when the PAPER
ingested, clustered or wrote something) and READING HISTORY, which this
surface simply does not have: it holds no per-person marks, so every 'since'
comparison uses the caller's stated version or trace times — a ``published``
timestamp is never compared, and the paper's ``seen`` marks never leave the
owner side uninvited — the one exception is the receipt above: a person's own
mark, verified and handed back to that person with consent, never into the
public export.

The reader is bound: the manifest records the reader it was exported for and
the lookup requires that exact ``--reader``. The citation contract rides in
every payload: sources are the only citable evidence; the summary and the
change notes are model-written text, never evidence; links and model
summaries are never promoted to 'confirmed' claims.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .edition import snapshot_path
from .edition.build import story_rev

EXPORT_KIND = "gaceta.agent-export"
EXPORT_SCHEMA_VERSION = 1
EDITION_SNAPSHOT_VERSION = 1  # the edition snapshot format build_edition writes
DEFAULT_MAX_AGE_HOURS = 48.0
FUTURE_TOLERANCE = timedelta(hours=1)  # built_at from the future → refuse
# same slug rule as serve.READER_RE (also kills path tricks in default paths)
READER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_STORY_ID = 120
MAX_EXCERPT_CHARS = 280  # verbatim span of a public source's text
MAX_COVERAGE = 8  # unassessed-coverage refs carried per story
MAX_BASELINE_ENTRIES = 500  # sanity bound on a caller-stated baseline

CITATION_POLICY = (
    "Sources are the only citable evidence: cite a source only for what it "
    "explicitly supports. The summary field is model-written text, never "
    "evidence. Links and model summaries are never promoted to 'confirmed' "
    "claims; if the sources do not support something, say so."
)
CITATION = {"policy": CITATION_POLICY, "summary_origin": "model",
            "confirmed_exists": False}

SEMANTICS = {
    "published": (
        "sources[].published and support[].published: when the OUTLET "
        "published the content. Never reading history; never used for "
        "'since your baseline' comparisons."
    ),
    "trace": (
        "first_seen/last_seen, sources[].added, versions[].written_at, "
        "corrections[].at: when the PAPER ingested, clustered or wrote "
        "something. Never reading history."
    ),
    "reading_history": (
        "only your baseline, and only what it explicitly states: an "
        "edition date when you stated one, and the write-up versions you "
        "last saw. This surface holds no per-person marks and cannot know "
        "when anyone actually read anything — and it never derives a "
        "reading date from a version, a trace time, or the edition that "
        "contained an earlier version."
    ),
    "provenance": (
        "baseline.provenance labels where each baseline fact came from, "
        "AS THE CALLER STATES IT: 'user_statement', 'verified_receipt' "
        "(the owner-side receipt path's own output, kept so a receipt "
        "fragment passes to the brief unchanged), or 'unknown' (unstated "
        "— the default for baseline files written before this field "
        "existed). The agent side cannot verify any origin: a label is "
        "never authentication and never evidence. A fact labeled "
        "'verified_receipt' is graded against the export's evidence "
        "exactly like any other claim — the label adds no trust and is "
        "never a 'verified' or 'confirmed' status."
    ),
    "brief_version": (
        "brief_version 2 renamed the launch brief's chronology block: "
        "v1's 'skipped_editions' (whose notes read as non-reading claims) "
        "is 'intervening_editions' — publication chronology only. The "
        "old key is retained, deprecated, and always null: it never "
        "carries a list again and claims nothing. Clients should read "
        "brief_version before parsing the brief."
    ),
    "comparisons": (
        "every 'since your baseline' comparison uses your stated version "
        "or the paper's trace times; a source's published time is never "
        "compared. A stated version is compared with versions only; a "
        "stated date bounds publication chronology only."
    ),
    "intervening_editions": (
        "public editions BUILT between your stated baseline date and the "
        "current edition — publication chronology only. Knowing these "
        "editions were built never proves they were skipped, unread or "
        "undelivered: readership of any edition is unknown to this "
        "surface, this brief says nothing about delivery or access "
        "cycles, and no reading date may be inferred from a version. With "
        "no date stated, which editions intervened is unknown."
    ),
}

APPROVAL = {
    "state": "proposal",
    "by": "reader",
    "how": (
        "accept by reading the cited sources, then record your own new "
        "baseline (this edition's date and each story's current version, "
        "each fact marked provenance user_statement) for the next launch. "
        "Record only what you actually read — never a date or version "
        "inferred from the paper's trace. The paper's own 'Visto' marks "
        "stay on the owner side; this surface never writes anything."
    ),
    "never": (
        "no proposal here is 'confirmed'; links and model summaries are never "
        "promoted to 'confirmed' claims."
    ),
}


class ExportError(Exception):
    """Any reason the surface refuses to export or to serve data (fail closed)."""


def validate_reader_id(reader: object) -> str:
    if not isinstance(reader, str) or not READER_RE.fullmatch(reader):
        raise ExportError(f"bad reader id {reader!r}")
    return reader


def export_dir(data_dir: Path, reader_id: str) -> Path:
    return Path(data_dir) / "agent-exports" / reader_id


def current_export_path(data_dir: Path, reader_id: str) -> Path:
    return export_dir(data_dir, reader_id) / "current.json"


# ------------------------------------------------------------------ owner side

def _ts_iso(ts) -> str | None:
    """unix seconds → ISO (UTC), like the edition builder's stamps."""
    if ts is None or isinstance(ts, bool):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(
            timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def _excerpt(body, cap: int = MAX_EXCERPT_CHARS) -> str | None:
    """Verbatim prefix of a source's public text, cut on a word boundary."""
    text = " ".join(str(body or "").split())
    if not text:
        return None
    if len(text) <= cap:
        return text
    cut = text[:cap]
    if " " in cut[cap // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def _http_url(url) -> str | None:
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
        ok = parts.scheme.lower() in ("http", "https") and bool(parts.hostname)
    except ValueError:
        ok = False
    return text if ok else None


def _safe_source(src: dict) -> dict:
    url = str(src.get("url") or "")
    if not _http_url(url):
        raise ExportError(f"non-http(s) source url {url!r}; refusing")
    out = {"title": src.get("title"), "url": url, "author": src.get("author"),
           "kind": src.get("kind"), "published": src.get("published"),
           "role": src.get("role")}
    # 'added' (unix, when the paper ingested the item) becomes an ISO trace
    # stamp; the caller's comparisons use it, never 'published'.
    if src.get("added") is not None:
        iso = _ts_iso(src.get("added"))
        if iso:
            out["added"] = iso
    return out


def _support_ref(item: dict) -> dict | None:
    """One member item as a citable reference (+ verbatim excerpt).

    Only for items with a public http(s) url; 'excerpt' is a verbatim prefix
    of the item's own source text (never an annotation note).
    """
    url = _http_url(item.get("url"))
    if not url:
        return None
    ref = {"title": item.get("title"), "url": url, "author": item.get("author"),
           "kind": item.get("source_kind"),
           "published": _ts_iso(item.get("published")),
           "added": _ts_iso(item.get("ingested_at"))}
    ex = _excerpt(item.get("body"))
    if ex:
        ref["excerpt"] = ex
    return ref


def _trace_editions(conn: sqlite3.Connection, reader_id: str) -> list[dict]:
    """The reader's PUBLIC build trace (editions table), oldest first.

    Non-public editions are invisible by design: the trace exists so a caller
    can see which public editions it has no record of reading.
    """
    try:
        rows = conn.execute(
            "SELECT date, built_at FROM editions "
            "WHERE reader_id = ? AND max_privacy = 'public' ORDER BY date",
            (reader_id,),
        ).fetchall()
    except sqlite3.Error as e:
        raise ExportError(f"cannot read the build trace: {e}") from None
    out = []
    for r in rows:
        built = _ts_iso(r["built_at"])
        if not built:
            raise ExportError(
                f"build trace row {r['date']!r} has a bad built_at; refusing")
        out.append({"date": str(r["date"]), "built_at": built})
    return out


def _story_evidence(conn: sqlite3.Connection, reader_id: str,
                    story_id: str) -> dict:
    """Version/coverage evidence for one exported story, from the real tables.

    story_items has no join time, so items.ingested_at stands in for when a
    source joined the story — the same stand-in the editor's change pass uses
    (editor.changes._new_items). A story labeled public whose member items are
    not all public is corrupt state and refuses the whole export.
    """
    try:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT i.id, i.title, i.url, i.author, i.published, i.body,
                   i.ingested_at, s.kind AS source_kind, a.item_privacy
            FROM story_items si
            JOIN items i ON i.id = si.item_id
            LEFT JOIN sources s ON s.id = i.source_id
            LEFT JOIN annotations a ON a.item_id = i.id AND a.reader_id = ?
            WHERE si.story_id = ?
            ORDER BY i.ingested_at, i.id
            """,
            (reader_id, story_id),
        )]
        vers = conn.execute(
            "SELECT version, written_at, what_changed FROM story_versions "
            "WHERE story_id = ? ORDER BY version", (story_id,),
        ).fetchall()
        corr = conn.execute(
            "SELECT said, now, at, evidence_item_id FROM corrections "
            "WHERE story_id = ? ORDER BY at", (story_id,),
        ).fetchall()
    except sqlite3.Error as e:
        raise ExportError(f"cannot read evidence for story {story_id!r}: {e}") from None
    for it in rows:
        label = str(it.get("item_privacy") or "").strip().lower()
        if label != "public":
            raise ExportError(
                f"story {story_id!r} is labeled public but member item "
                f"{it['id']!r} is {label or 'unlabeled'}; refusing (fail closed)")
    evidence: dict = {"versions": [], "unassessed_coverage": [], "corrections": []}
    prev_w = 0
    for v in vers:
        w = int(v["written_at"])
        joined = [it for it in rows if prev_w < it["ingested_at"] <= w]
        support = [r for it in joined if (r := _support_ref(it))]
        evidence["versions"].append({
            "version": int(v["version"]), "written_at": _ts_iso(w),
            "what_changed": (v["what_changed"] if v["what_changed"] is None
                             else str(v["what_changed"])),
            "support": support,
        })
        prev_w = w
    # coverage no write-up has assessed: joined after the newest version
    # (for a never-written story that is every member — nothing was assessed)
    w_last = prev_w
    cov = [it for it in rows if it["ingested_at"] > w_last]
    evidence["unassessed_coverage"] = [r for it in cov if (r := _support_ref(it))]
    evidence["coverage_total"] = len(cov)
    by_id = {it["id"]: it for it in rows}
    for c in corr:
        entry = {"said": c["said"], "now": c["now"], "at": _ts_iso(c["at"])}
        ev = c["evidence_item_id"]
        if ev is not None and ev in by_id:
            if ref := _support_ref(by_id[ev]):
                entry["supported_by"] = ref
        evidence["corrections"].append(entry)
    return evidence


def _attach_evidence(export: dict, conn: sqlite3.Connection,
                     reader_id: str) -> dict:
    """Add the DB-backed evidence blocks to a built export (in place).

    Query scope is this reader only; every attached item was re-checked
    against its privacy label above. Nothing per-person is queried.
    """
    export["editions"] = _trace_editions(conn, reader_id)
    for sid, story in export["stories"].items():
        ev = _story_evidence(conn, reader_id, sid)
        if ev["versions"]:
            story["current_version"] = ev["versions"][-1]["version"]
            story["versions"] = ev["versions"]
        else:
            # never written up: everything rests on its sources, none assessed
            story["current_version"] = None
        if ev["unassessed_coverage"]:
            story["unassessed_coverage"] = ev["unassessed_coverage"][:MAX_COVERAGE]
            if ev["coverage_total"] > MAX_COVERAGE:
                story["unassessed_coverage_total"] = ev["coverage_total"]
        story["corrections"] = ev["corrections"]
    return export


def _export_story(story_id: str, story: dict) -> dict:
    """One story for the export, or ExportError if it is not strictly public."""
    if not isinstance(story, dict):
        raise ExportError(f"story {story_id!r} is malformed")
    why = str(story.get("why_it_matters") or "").strip()
    if why:
        raise ExportError(
            f"story {story_id!r} has why_it_matters (always private); refusing")
    label = str(story.get("privacy") or "").strip().lower()
    if label != "public":
        raise ExportError(
            f"story {story_id!r} is not public ({label or 'no label'}); refusing")
    entry = {
        "title": story.get("title"),
        "status": story.get("status"),
        "privacy": "public",
        "summary": story.get("summary"),
        "summary_written_by": "model",
        "confidence": story.get("confidence"),
        "first_seen": story.get("first_seen"),
        "last_seen": story.get("last_seen"),
        "sources": [_safe_source(s) for s in story.get("sources") or []],
    }
    if story.get("corrections"):
        entry["corrections"] = story.get("corrections")
    if story.get("history"):
        entry["history"] = story.get("history")
    return entry


def build_export(edition: dict, reader_id: str, reader_name: str,
                 conn: sqlite3.Connection | None = None) -> dict:
    """Public edition snapshot (dict) → agent export (dict). Fail closed.

    Refuses anything not strictly public: a snapshot with a different version
    or max_privacy, a story whose label is not 'public', a story with
    why_it_matters, a non-http(s) source url. With ``conn`` (the trusted,
    owner-side path) the export also carries the version evidence: the public
    build trace, per-story write-up versions with the public sources that
    joined between them, corrections with their evidence, and unassessed
    coverage — all re-checked against privacy, all from the existing tables.
    """
    validate_reader_id(reader_id)
    if not isinstance(edition, dict):
        raise ExportError("edition snapshot is not a JSON object")
    if edition.get("version") != EDITION_SNAPSHOT_VERSION:
        raise ExportError(
            f"unsupported edition snapshot version {edition.get('version')!r}; "
            f"this code reads v{EDITION_SNAPSHOT_VERSION}")
    if str(edition.get("max_privacy") or "") != "public":
        raise ExportError(
            "refusing to export a non-public edition snapshot "
            f"(max_privacy={edition.get('max_privacy')!r}); build one with "
            f"`gaceta edition --max-privacy public`")
    stories = edition.get("stories")
    if not isinstance(stories, dict):
        raise ExportError("edition snapshot has no stories object")
    exported = {sid: _export_story(sid, s) for sid, s in stories.items()}
    folio = (edition.get("folio") or {}).get("code")
    export = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "kind": EXPORT_KIND,
        "reader": {"id": reader_id, "name": str(reader_name or reader_id)},
        "edition": {
            "date": edition.get("date"),
            "built_at": edition.get("built_at"),
            "folio_code": folio,
            "stories_total": len(exported),
        },
        "privacy": {"max_level": "public"},
        "citation": CITATION,
        "stories": exported,
    }
    if conn is not None:
        _attach_evidence(export, conn, reader_id)
    return export


def write_export(export: dict, data_dir: Path, reader_id: str) -> tuple[Path, Path]:
    """Write the dated export + refresh current.json, atomically. Idempotent."""
    day = str(export["edition"]["date"])
    d = export_dir(data_dir, reader_id)
    d.mkdir(parents=True, exist_ok=True)
    dated = d / f"{day}.json"
    current = d / "current.json"
    text = json.dumps(export, ensure_ascii=False, indent=2) + "\n"
    for target in (dated, current):
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    return dated, current


def export_for_date(data_dir: Path, reader_id: str, cfg: dict,
                    date_str: str | None = None,
                    conn: sqlite3.Connection | None = None) -> tuple[dict, Path, Path]:
    """CLI entry: public snapshot for ``date_str`` (default today) → export file.

    Reads the reader's config for the display name and the day's PUBLIC
    edition snapshot from disk. With ``conn`` (the normal owner path) it also
    attaches the version evidence from the database, re-checked against
    privacy; without it the export still serves the plain lookups, and the
    launch brief fails closed for lack of evidence rather than guessing.
    """
    validate_reader_id(reader_id)
    day = date_str or datetime.now().date().isoformat()
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except ValueError:
        raise ExportError(f"expected a YYYY-MM-DD date, got {day!r}") from None
    snap = snapshot_path(Path(data_dir), reader_id, day, "public")
    if not snap.is_file():
        hint = f" --date {day}" if date_str else ""
        raise ExportError(
            f"no public edition snapshot at {snap}; build one first: "
            f"gaceta edition --reader {reader_id} --max-privacy public{hint}")
    try:
        edition = json.loads(snap.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ExportError(f"unreadable edition snapshot {snap}: {e}") from None
    export = build_export(edition, reader_id, cfg.get("name") or reader_id,
                          conn=conn)
    dated, current = write_export(export, data_dir, reader_id)
    return export, dated, current


# ------------------------------------------------------ owner: seen receipts

MAX_BY_NAME = 40  # same bound as the local server's by (serve.MAX_BY)


def derive_receipt(conn: sqlite3.Connection, reader_id: str, by_name: object,
                   story_id: object) -> dict:
    """Owner side, opt-in: ONE person's own seen mark for ONE story → a
    receipt stating which write-up version they actually saw, verified
    against the paper's own history or honestly unknown.

    Consults exactly one row: this reader, this person (matched the paper's
    own way, case-insensitively — 'alice' on a new device is the same person
    as 'Alice'), this story. Never another person's mark, never another
    reader's rows, never a write. The receipt carries no raw reading history
    — no person name, no mark time, not even the rev: it states a version or
    nothing, and ``receipt_baseline`` renders it as the baseline the existing
    since path already accepts. Non-public stories refuse outright: the
    agent surface can never grade them, so their ids never travel.
    """
    validate_reader_id(reader_id)
    by = str(by_name or "").strip()
    if not by or len(by) > MAX_BY_NAME:
        raise ExportError(f"by must be the person's name, 1..{MAX_BY_NAME} chars")
    if not isinstance(story_id, str) or not story_id or len(story_id) > MAX_STORY_ID:
        raise ExportError("bad story id")
    try:
        story = conn.execute(
            "SELECT privacy FROM stories WHERE id = ? AND reader_id = ?",
            (story_id, reader_id)).fetchone()
        if story is None:
            raise ExportError(
                f"unknown story id {story_id!r} for reader {reader_id!r}")
        if str(story["privacy"] or "").strip().lower() != "public":
            raise ExportError(
                f"story {story_id!r} is not public; the receipt path serves "
                "public stories only (the agent surface can never grade the rest)")
        mark = conn.execute(
            "SELECT rev FROM seen WHERE reader_id = ? AND story_id = ? "
            "AND lower(by_name) = lower(?)",
            (reader_id, story_id, by)).fetchone()
        vers = conn.execute(
            "SELECT version, summary FROM story_versions WHERE story_id = ? "
            "ORDER BY version", (story_id,)).fetchall()
    except sqlite3.Error as e:
        raise ExportError(f"cannot read the paper's own tables: {e}") from None
    receipt = {"reader": reader_id, "story_id": story_id, "version": None}
    if mark is None:
        return receipt | {
            "status": "unknown",
            "reason": ("no seen mark on record for this person on this story; "
                       "the receipt states nothing")}
    version = _verified_seen_version(vers, str(mark["rev"]))
    if version is None:
        return receipt | {
            "status": "unknown",
            "reason": ("the mark's rev matches no write-up on record — the "
                       "story's state when it was marked cannot be reproduced; "
                       "the receipt states nothing")}
    return receipt | {
        "status": "known", "version": version,
        "reason": (f"the mark's rev pins the write-up of version {version} "
                   "in the paper's own history")}


def _verified_seen_version(vers, rev: str) -> int | None:
    """Which numbered write-up does this seen rev pin? A version is claimed
    only when the paper's own rev computation reproduces the mark's rev
    exactly: ``edition.build._story_entry`` computes the revs the pages —
    and so the ``seen`` table — carry, and as built its member list is
    empty, so a page rev pins the story's write-up text. That makes the
    hash check necessary but not sufficient: repeated text can match more
    than one version. Ambiguous and unreproducible marks stay unknown."""
    matches = {int(v["version"]) for v in vers if story_rev([], v["summary"]) == rev}
    return next(iter(matches)) if len(matches) == 1 else None


def receipt_baseline(receipt: dict) -> dict:
    """The receipt as the caller's own baseline: exactly the keys the
    existing since path already accepts, so it can be passed directly with
    ``--baseline``. A version is stated only when verified; unknown states
    nothing. No date: the seen table records marks, not which edition was
    read, so the brief answers the intervening-editions question with its
    honest unknown instead of inventing one. The stated version carries
    its provenance — a verified local receipt — so the brief can say where
    the fact came from without ever turning it into a reading date."""
    versions: dict[str, int] = {}
    prov: dict[str, str] = {}
    if receipt.get("status") == "known" and receipt.get("version") is not None:
        sid = str(receipt["story_id"])
        versions[sid] = int(receipt["version"])
        prov[sid] = "verified_receipt"
    out = {"reader": receipt["reader"], "versions": versions}
    if prov:
        out["provenance"] = {"versions": prov}
    return out


# ------------------------------------------------------------------ agent side

# The export is an allowlist contract both ways: the builder emits exactly
# these keys, and the reader refuses anything else (an edition-snapshot field
# that must never ride along — 'rev', taste signals, layout — cannot sneak in).
EXPORT_KEYS = frozenset({"schema_version", "kind", "reader", "edition", "privacy",
                         "citation", "stories", "editions"})
STORY_KEYS = frozenset({"title", "status", "privacy", "summary",
                        "summary_written_by", "confidence", "first_seen",
                        "last_seen", "sources", "corrections", "history",
                        "current_version", "versions", "unassessed_coverage",
                        "unassessed_coverage_total"})
SOURCE_KEYS = frozenset({"title", "url", "author", "kind", "published", "role",
                        "added"})
REF_KEYS = frozenset({"title", "url", "author", "kind", "published", "added",
                      "excerpt"})
CORRECTION_KEYS = frozenset({"said", "now", "at", "supported_by"})
HISTORY_KEYS = frozenset({"version", "at", "summary", "what_changed"})
VERSION_KEYS = frozenset({"version", "written_at", "what_changed", "support"})


def _parse_iso(value, what: str) -> datetime:
    """ISO timestamp → aware datetime; naive stamps are read as local."""
    text = str(value or "")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ExportError(f"{what} is not an ISO timestamp: {text!r}; refusing") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _check_ref(ref: object, what: str, *, require_added: bool = False) -> None:
    """An evidence reference (story source or version support). Fail closed."""
    if not isinstance(ref, dict):
        raise ExportError(f"{what} is malformed; refusing")
    if not set(ref) <= REF_KEYS:
        raise ExportError(f"{what} has unknown keys; refusing (fail closed)")
    if not _http_url(ref.get("url")):
        raise ExportError(f"{what} has a non-http(s) url; refusing")
    for key in ("title", "author", "kind", "published"):
        if ref.get(key) is not None and not isinstance(ref[key], str):
            raise ExportError(f"{what}.{key} must be a string or null; refusing")
    if "added" in ref:
        _parse_iso(ref["added"], f"{what}.added")
    elif require_added:
        raise ExportError(f"{what} lacks its 'added' trace stamp; refusing")
    ex = ref.get("excerpt")
    if ex is not None:
        if not isinstance(ex, str) or not ex.strip():
            raise ExportError(f"{what}.excerpt must be a non-empty string; refusing")
        if len(ex) > MAX_EXCERPT_CHARS + 1:
            raise ExportError(
                f"{what}.excerpt exceeds {MAX_EXCERPT_CHARS} chars; refusing")


def _check_editions(export: dict) -> None:
    """The public build trace: {date, built_at} rows, strictly increasing."""
    eds = export.get("editions")
    if eds is None:
        return
    if not isinstance(eds, list):
        raise ExportError("editions trace must be a list; refusing")
    prev: str | None = None
    for e in eds:
        if not isinstance(e, dict) or set(e) != {"date", "built_at"}:
            raise ExportError(
                "editions trace rows must be exactly {date, built_at}; refusing")
        d = str(e.get("date") or "")
        try:
            if date.fromisoformat(d).isoformat() != d:
                raise ValueError
        except ValueError:
            raise ExportError(f"editions trace has a bad date {d!r}; refusing") from None
        _parse_iso(e.get("built_at"), "editions trace built_at")
        if prev is not None and d <= prev:
            raise ExportError(
                "editions trace must be strictly increasing; refusing")
        prev = d


def _require(export: dict, reader: str) -> None:
    """Every structural check, all fail closed. Order: baseline first."""
    if export.get("kind") != EXPORT_KIND:
        raise ExportError("not a gaceta agent export (kind mismatch)")
    v = export.get("schema_version")
    if isinstance(v, bool) or v != EXPORT_SCHEMA_VERSION:
        raise ExportError(
            f"unknown export schema version {v!r}; this tool supports "
            f"{EXPORT_SCHEMA_VERSION} — refusing (fail closed)")
    if not set(export) <= EXPORT_KEYS:
        extra = sorted(set(export) - EXPORT_KEYS)
        raise ExportError(
            f"export carries keys this surface never emits ({', '.join(extra)}); "
            "refusing (fail closed)")
    rid = (export.get("reader") or {}).get("id")
    if rid != reader:
        raise ExportError(f"export is bound to reader {rid!r}, not {reader!r}")
    if (export.get("privacy") or {}).get("max_level") != "public":
        raise ExportError("export is not public-only; refusing")
    citation = export.get("citation") or {}
    if not isinstance(citation.get("policy"), str) or not citation.get("policy").strip():
        raise ExportError("export lacks its citation policy; refusing")
    ed = export.get("edition") or {}
    for key in ("date", "built_at"):
        if not str(ed.get(key) or "").strip():
            raise ExportError(f"edition baseline is missing {key}; refusing")
    stories = export.get("stories")
    if not isinstance(stories, dict):
        raise ExportError("export has no stories object; refusing")
    for sid, story in stories.items():
        _check_story(sid, story)
    _check_editions(export)


def _check_story(story_id: object, story: object) -> None:
    if not isinstance(story, dict):
        raise ExportError(f"story {story_id!r} is malformed; refusing")
    if str(story.get("privacy") or "").lower() != "public":
        raise ExportError(
            f"story {story_id!r} is not public; refusing the whole export "
            "(fail closed)")
    if str(story.get("why_it_matters") or "").strip():
        raise ExportError(
            f"story {story_id!r} carries why_it_matters (always private); "
            "refusing the whole export (fail closed)")
    if not set(story) <= STORY_KEYS:
        extra = sorted(set(story) - STORY_KEYS)
        raise ExportError(
            f"story {story_id!r} carries keys this surface never emits "
            f"({', '.join(extra)}); refusing the whole export (fail closed)")
    sources = story.get("sources")
    if not isinstance(sources, list):
        raise ExportError(f"story {story_id!r} has no sources list; refusing")
    for k, src in enumerate(sources):
        if not isinstance(src, dict):
            raise ExportError(f"story {story_id!r} has a malformed source; refusing")
        if not set(src) <= SOURCE_KEYS:
            raise ExportError(
                f"story {story_id!r} source[{k}] has unknown keys; refusing")
        if not _http_url(src.get("url")):
            raise ExportError(
                f"story {story_id!r} has a non-http(s) source url; refusing")
        if "added" in src:
            _parse_iso(src["added"], f"story {story_id!r} source added stamp")
    for k, c in enumerate(story.get("corrections") or []):
        what = f"story {story_id!r} correction[{k}]"
        if not isinstance(c, dict) or not {"said", "now", "at"} <= set(c):
            raise ExportError(f"{what} is malformed; refusing")
        if not set(c) <= CORRECTION_KEYS:
            raise ExportError(f"{what} has unknown keys; refusing")
        if not isinstance(c["said"], str) or not isinstance(c["now"], str):
            raise ExportError(f"{what} must carry string said/now; refusing")
        _parse_iso(c["at"], f"{what} at")
        if "supported_by" in c:
            _check_ref(c["supported_by"], f"{what} supported_by")
    for k, h in enumerate(story.get("history") or []):
        what = f"story {story_id!r} history[{k}]"
        if not isinstance(h, dict) or not set(h) <= HISTORY_KEYS:
            raise ExportError(f"{what} is malformed; refusing")
        if isinstance(h.get("version"), bool) or not isinstance(h.get("version"), int):
            raise ExportError(f"{what} has a bad version number; refusing")
        at = h.get("at")
        if not isinstance(at, (int, str)) or isinstance(at, bool):
            raise ExportError(f"{what} has a bad at stamp; refusing")
        if isinstance(at, str):
            _parse_iso(at, f"{what} at")
        if h.get("what_changed") is not None and not isinstance(h["what_changed"], str):
            raise ExportError(f"{what} has a bad what_changed; refusing")
    cur = story.get("current_version")
    if cur is not None and (isinstance(cur, bool) or not isinstance(cur, int) or cur < 1):
        raise ExportError(f"story {story_id!r} has a malformed current_version; refusing")
    versions = story.get("versions")
    if versions is not None:
        if not isinstance(versions, list) or not versions:
            raise ExportError(
                f"story {story_id!r} versions must be a non-empty list; refusing")
        expected = 1
        for k, u in enumerate(versions):
            what = f"story {story_id!r} versions[{k}]"
            if not isinstance(u, dict) or set(u) != VERSION_KEYS:
                raise ExportError(f"{what} is malformed; refusing")
            n = u.get("version")
            if isinstance(n, bool) or not isinstance(n, int) or n != expected:
                raise ExportError(
                    f"story {story_id!r} versions must be contiguous from 1; refusing")
            expected += 1
            _parse_iso(u.get("written_at"), f"{what} written_at")
            if u.get("what_changed") is not None and not isinstance(u["what_changed"], str):
                raise ExportError(f"{what} has a bad what_changed; refusing")
            if not isinstance(u.get("support"), list):
                raise ExportError(f"{what} has a malformed support list; refusing")
            for j, ref in enumerate(u["support"]):
                _check_ref(ref, f"{what} support[{j}]")
    cov = story.get("unassessed_coverage")
    if cov is not None:
        if not isinstance(cov, list):
            raise ExportError(
                f"story {story_id!r} unassessed_coverage must be a list; refusing")
        for k, ref in enumerate(cov):
            _check_ref(ref, f"story {story_id!r} unassessed_coverage[{k}]",
                       require_added=True)
    total = story.get("unassessed_coverage_total")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int)
                              or total < 0):
        raise ExportError(
            f"story {story_id!r} has a malformed unassessed_coverage_total; refusing")


def _check_freshness(export: dict, max_age_hours: float,
                     now: datetime | None = None) -> None:
    built = str(export["edition"]["built_at"])
    try:
        built_dt = datetime.fromisoformat(built)
    except ValueError:
        raise ExportError(f"unparseable built_at {built!r}; refusing") from None
    if built_dt.tzinfo is None:  # the builder always writes an offset
        built_dt = built_dt.astimezone()
    now_dt = now or datetime.now().astimezone()
    if built_dt - now_dt > FUTURE_TOLERANCE:
        raise ExportError(
            f"export built_at {built} is in the future; refusing (fail closed)")
    if now_dt - built_dt > timedelta(hours=max_age_hours):
        raise ExportError(
            f"export is stale: built {built}, older than {max_age_hours:g}h "
            "(fail closed; the owner refreshes it with `gaceta agent export`)")


def load_export(path: Path, *, reader: str,
                max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                now: datetime | None = None) -> dict:
    """Agent side: read and fully validate ONE export file. Never writes.

    Binding comes from the manifest; the agent side never reads reader.toml.
    """
    validate_reader_id(reader)
    if not isinstance(max_age_hours, (int, float)) or max_age_hours <= 0:
        raise ExportError("max-age-hours must be a positive number")
    p = Path(path)
    if not p.is_file():
        raise ExportError(f"no agent export at {p}")
    try:
        export = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ExportError(f"unreadable agent export {p}: {e}") from None
    if not isinstance(export, dict):
        raise ExportError("agent export is not a JSON object")
    _require(export, reader)
    _check_freshness(export, float(max_age_hours), now)
    return export


def edition_view(export: dict) -> dict:
    """Listing: one line per story (id, title, status, counts), newest first."""
    order = sorted(export["stories"].items(),
                   key=lambda kv: (kv[1].get("last_seen") or "", kv[0]),
                   reverse=True)
    return {
        "schema_version": export["schema_version"],
        "kind": export["kind"],
        "reader": export["reader"],
        "edition": export["edition"],
        "privacy": export["privacy"],
        "citation": export["citation"],
        "stories": [{
            "id": sid,
            "title": s.get("title"),
            "status": s.get("status"),
            "version": s.get("current_version"),
            "sources": len(s.get("sources") or []),
            "corrections": len(s.get("corrections") or []),
            "last_seen": s.get("last_seen"),
        } for sid, s in order],
    }


def story_view(export: dict, story_id: object) -> dict:
    """One story in full, wrapped with the manifest and citation contract."""
    if not isinstance(story_id, str) or not story_id or len(story_id) > MAX_STORY_ID:
        raise ExportError("bad story id")
    story = export["stories"].get(story_id)
    if story is None:
        raise ExportError(
            f"unknown story id {story_id!r}; list them with "
            "`gaceta agent edition`")
    return {
        "schema_version": export["schema_version"],
        "kind": export["kind"],
        "reader": export["reader"],
        "edition": export["edition"],
        "privacy": export["privacy"],
        "citation": export["citation"],
        "story": {"id": story_id, **story},
    }


# ------------------------------------------------------- launch: since/baseline

# The since-view output has its own contract version, separate from the
# export file's schema_version. v2 renamed the chronology block: what v1
# called 'skipped_editions' (and worded as non-reading claims) is
# 'intervening_editions' — publication chronology only. The old key is
# RETAINED, deprecated, and always null so legacy clients never see a
# silently renamed field and never receive a claim through it.
BRIEF_VERSION = 2

BASELINE_KEYS = frozenset({"reader", "date", "versions", "provenance"})
PROVENANCE_KEYS = frozenset({"date", "versions"})
PROVENANCE_VALUES = frozenset({"user_statement", "verified_receipt",
                                "unknown"})


def _check_provenance(obj: dict, day, clean: dict[str, int]) -> dict:
    """Validate the caller's stated provenance for its own memory facts.

    ``{"date": <token>, "versions": {story: <token>}}`` where token is
    user_statement / verified_receipt / unknown. Fail closed on a token
    this surface does not know, on provenance for a fact the baseline
    does not state (a date, or a story with no version), and on unknown
    keys. Absent → every fact defaults to 'unknown': old baseline files
    stay loadable and are simply graded as unstated-origin claims.
    """
    if "provenance" not in obj:
        return {"date": None, "versions": {}}
    prov = obj["provenance"]
    if not isinstance(prov, dict):
        raise ExportError("baseline provenance must be an object "
                          "{date?, versions?}; refusing")
    unknown = set(prov) - PROVENANCE_KEYS
    if unknown:
        raise ExportError(
            f"baseline provenance has unknown keys ({', '.join(sorted(unknown))}); "
            "this surface fails closed")
    out: dict = {"date": None, "versions": {}}
    if "date" in prov:
        if day is None:
            raise ExportError(
                "baseline provenance states a date origin, but the baseline "
                "states no date; refusing (fail closed)")
        token = prov["date"]
        if token not in PROVENANCE_VALUES:
            raise ExportError(
                f"baseline date provenance must be one of "
                f"{sorted(PROVENANCE_VALUES)}, got {token!r}; refusing")
        out["date"] = token
    pv = prov.get("versions")
    if pv is None:
        pv = {}
    if not isinstance(pv, dict):
        raise ExportError("baseline provenance.versions must be an object "
                          "{story_id: origin}; refusing")
    for sid, token in pv.items():
        if not isinstance(sid, str) or sid not in clean:
            raise ExportError(
                "baseline provenance.versions names a story the baseline "
                "states no version for; refusing (fail closed)")
        if token not in PROVENANCE_VALUES:
            raise ExportError(
                f"baseline provenance for {sid!r} must be one of "
                f"{sorted(PROVENANCE_VALUES)}, got {token!r}; refusing")
        out["versions"][sid] = token
    return out


def load_baseline(source: object, *, reader: str, export: dict) -> dict:
    """Read and validate the CALLER's own memory file. Fail closed.

    The baseline is what the agent session states about its own reading: the
    edition date it last read (when it states one), the write-up version it
    last saw per story, and — optionally — where each of those facts came
    from (provenance: user statement, verified local receipt, or unknown).
    It is never taken from the paper — this surface holds no per-person
    marks, so reading history exists here only as the caller's own claim,
    believed exactly as far as the export's evidence supports it. Provenance
    is metadata about the claim, never evidence, and never a date.
    ``source``: a path, ``"-"`` for stdin, or None (first launch: no memory).
    """
    validate_reader_id(reader)
    if source is None or source == "":
        return {"reader": reader, "date": None, "versions": {},
                "provenance": {"date": None, "versions": {}}}
    if source == "-":
        try:
            text = sys.stdin.read()
        except OSError as e:
            raise ExportError(f"cannot read the baseline from stdin: {e}") from None
    else:
        p = Path(source)
        if not p.is_file():
            raise ExportError(f"no baseline file at {p}")
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ExportError(f"unreadable baseline file {p}: {e}") from None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ExportError(f"baseline is not valid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise ExportError("baseline must be a JSON object")
    unknown = set(obj) - BASELINE_KEYS
    if unknown:
        raise ExportError(
            f"baseline has unknown keys ({', '.join(sorted(unknown))}); "
            "this surface fails closed")
    stated = obj.get("reader")
    if stated is None:
        raise ExportError(
            "baseline must state its reader (the paper's id it was read from)")
    if not isinstance(stated, str) or stated != reader:
        raise ExportError(
            f"baseline is bound to reader {stated!r}, not {reader!r}; refusing "
            "(one paper's memory never reads another's)")
    day = obj.get("date")
    if day is not None:
        if not isinstance(day, str):
            raise ExportError("baseline date must be a YYYY-MM-DD string")
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except ValueError:
            raise ExportError(f"baseline date is not YYYY-MM-DD: {day!r}") from None
        ed_day = str((export.get("edition") or {}).get("date") or "")
        if day > ed_day:
            raise ExportError(
                f"baseline date {day} is after this export's edition date "
                f"{ed_day}; the export is older than your memory — ask the "
                "owner to rebuild and re-export (fail closed)")
    versions = obj.get("versions")
    if versions is None:
        versions = {}
    if not isinstance(versions, dict):
        raise ExportError("baseline versions must be an object {story_id: version}")
    if len(versions) > MAX_BASELINE_ENTRIES:
        raise ExportError(
            f"baseline states {len(versions)} stories; this surface refuses "
            f"more than {MAX_BASELINE_ENTRIES}")
    clean: dict[str, int] = {}
    for sid, ver in versions.items():
        if not isinstance(sid, str) or not sid or len(sid) > MAX_STORY_ID:
            raise ExportError("baseline has a malformed story id; refusing")
        if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
            raise ExportError(
                f"baseline version for {sid!r} must be an integer >= 1; refusing")
        clean[sid] = ver
    prov = _check_provenance(obj, day, clean)
    # normalize: every STATED fact carries an explicit origin — unstated
    # means 'unknown', so the brief never has to guess or omit it
    if day is not None and prov["date"] is None:
        prov["date"] = "unknown"
    for sid in clean:
        prov["versions"].setdefault(sid, "unknown")
    return {"reader": reader, "date": day, "versions": clean,
            "provenance": prov}


def _version_list(story: dict) -> list[dict]:
    """The story's write-up versions, normalized: version, written_at (dt),
    what_changed, support. Prefers the versions block; falls back to the
    history block (older exports), whose support is unknown ([])."""
    raw = story.get("versions")
    if isinstance(raw, list) and raw:
        return [{"version": u["version"],
                 "written_at": _parse_iso(u["written_at"], "versions written_at"),
                 "what_changed": u.get("what_changed"),
                 "support": u.get("support") or []} for u in raw]
    hist = story.get("history")
    if isinstance(hist, list) and hist:
        out = []
        for h in hist:
            at = h.get("at")
            try:
                w = (datetime.fromtimestamp(int(at), tz=timezone.utc)
                     if not isinstance(at, str)
                     else _parse_iso(at, "history at"))
            except (ValueError, OverflowError, OSError):
                w = None
            out.append({"version": h["version"], "written_at": w,
                        "what_changed": h.get("what_changed"), "support": []})
        return out
    return []


def _story_source_refs(story: dict) -> list[dict]:
    """The story's current sources as plain references (no excerpts needed)."""
    return [{"title": s.get("title"), "url": s.get("url"),
             "author": s.get("author"), "kind": s.get("kind"),
             "published": s.get("published"), "role": s.get("role")}
            for s in story.get("sources") or []]


def _proposal_support(story: dict, support: list[dict]) -> tuple[list[dict], str]:
    """Evidence behind one proposal: the sources that joined, else the story's
    own sources as references, else nothing (the note rests on the rewrite)."""
    if support:
        return support, "joined_sources"
    refs = _story_source_refs(story)
    if refs:
        return refs, "story_sources"
    return [], "none"


def _after(point_dt: datetime | None, point_day: date | None,
           iso: str | None) -> bool:
    """Is this trace stamp after the caller's point? Compares trace times
    only (added/written_at/at/first_seen) — a source's published time is
    never compared. Exactly one of point_dt/point_day is set."""
    if iso is None:
        return False
    when = _parse_iso(iso, "trace stamp")
    if point_dt is not None:
        return when > point_dt
    return when.date() > point_day


def _change_block(story: dict, versions: list[dict], evidence: list[dict],
                  *, point_dt: datetime | None, point_day: date | None,
                  new_to_you: bool) -> dict:
    """Classify one story's change since the caller's point.

    material_change — a change note ('what_changed', itself evidence-gated
    when written) or a correction, each carried as a proposal with the public
    sources behind it. no_supported_update — the paper's change pass looked
    and found only rewording/duplicate coverage, or no trace evidence exists
    after the point. unknown — evidence is missing (a rewrite with no change
    note on record, or coverage no write-up has assessed): explicit unknown,
    with the factual coverage listed and no materiality claim.
    """
    proposals: list[dict] = []
    assessed_no_change: list[dict] = []
    unassessed: list[dict] = []
    for u in evidence:
        wc = u["what_changed"]
        entry = {"version": u["version"],
                 "written_at": (u["written_at"].isoformat(timespec="seconds")
                                if u["written_at"] else None)}
        if isinstance(wc, str) and wc.strip():
            support, kind = _proposal_support(story, u["support"])
            proposals.append({"kind": "proposed_update", **entry,
                              "what_changed": wc,
                              "what_changed_origin": "model",
                              "support": support, "support_kind": kind})
        elif wc is None and u["version"] == 1:
            # a first write-up has no change note by design; factual, not missing
            continue
        elif wc is None:
            unassessed.append({**entry, "support": u["support"],
                               "note": "rewrite without a change note on record "
                                       "(the change pass has not assessed it)"})
        else:
            assessed_no_change.append({**entry, "support": u["support"],
                                       "note": "assessed as rewording/duplicate "
                                               "coverage only"})
    corrections = []
    for c in story.get("corrections") or []:
        if _after(point_dt, point_day, c.get("at")):
            corrections.append(c)
    for c in corrections:
        support = [c["supported_by"]] if c.get("supported_by") else []
        kind = "joined_sources" if support else (
            "story_sources" if _story_source_refs(story) else "none")
        proposals.append({
            "kind": "proposed_correction", "version": None,
            "written_at": c.get("at"), "said": c["said"], "now": c["now"],
            "what_changed": c["now"], "what_changed_origin": "model",
            "support": support or _story_source_refs(story),
            "support_kind": kind})
    coverage = []
    for ref in story.get("unassessed_coverage") or []:
        if _after(point_dt, point_day, ref.get("added")):
            coverage.append(ref)

    incomplete = bool(unassessed or coverage)
    if proposals:
        block = {"status": "material_change",
                 "reason": ("evidence-supported change since your baseline; "
                            "each proposal carries the public sources behind it"
                            + ("; further evidence is not yet assessed"
                               if incomplete else "")),
                 "proposals": proposals}
        if incomplete:
            block["incomplete_evidence"] = True
    elif new_to_you:
        block = {"status": "new_story",
                 "reason": ("first entered the paper's trace after your "
                            "baseline date; none of it is in your baseline — "
                            "its sources are the evidence")}
    elif unassessed or coverage:
        block = {"status": "unknown",
                 "reason": ("missing evidence: " +
                            ("a newer write-up has no change note on record; "
                             if unassessed else "") +
                            ("coverage joined that no write-up has assessed yet"
                             if coverage else ""))}
    elif assessed_no_change:
        block = {"status": "no_supported_update",
                 "reason": ("the paper's change pass assessed the newer "
                            "write-up(s) as rewording/duplicate coverage only")}
    else:
        block = {"status": "no_supported_update",
                 "reason": ("no write-up, correction or ingested coverage trace "
                            "after your baseline point in this export's evidence")}
    if unassessed:
        block["unassessed_versions"] = unassessed
    if assessed_no_change:
        block["assessed_no_change"] = assessed_no_change
    if coverage:
        block["new_coverage_unassessed"] = coverage
    return block


def _on_record(story: dict, cur) -> dict | None:
    """The paper's public facts for a story the caller has no record of:
    its current write-up version and its corrections, each with the
    evidence behind it. Public facts about the PAPER, never claims about
    the caller — the brief lists them so an interested reader can be told
    what is on record, while what is new to THEM stays unknown."""
    facts: dict = {}
    if cur is not None:
        facts["current_version"] = cur
    corrections = []
    for c in story.get("corrections") or []:
        entry = {"said": c["said"], "now": c["now"], "at": c.get("at")}
        if c.get("supported_by"):
            entry["supported_by"] = c["supported_by"]
        corrections.append(entry)
    if corrections:
        facts["corrections"] = corrections
    if not facts:
        return None
    facts["note"] = ("the paper's public record for this story — plain "
                     "facts, not claims about what you have read")
    return facts


def _story_update(sid: str, story: dict, claimed: dict[str, int],
                  base_day: date | None, provenance: dict[str, str]) -> dict:
    """One export story: baseline classification + the change block."""
    versions = _version_list(story)
    newest = versions[-1]["version"] if versions else None
    cur = story.get("current_version")
    if cur is None:
        cur = newest
    mine = claimed.get(sid)
    prov = provenance.get(sid)
    first_seen = story.get("first_seen")
    if mine is None:
        point_dt = point_day = None
        if base_day is None:
            baseline_block = {"claimed_version": None, "status": "none_stated",
                              "provenance": None,
                              "reason": ("your baseline states no version and "
                                         "no edition date for this story")}
            change = {"status": "unknown",
                      "reason": ("no last-read record for this story, so this "
                                 "surface cannot compare; the paper's public "
                                 "facts on record are listed — whether any "
                                 "of it is new to you is unknown")}
            on_record = _on_record(story, cur)
            if on_record:
                change["on_record"] = on_record
        else:
            baseline_block = {"claimed_version": None, "status": "none_stated",
                              "provenance": None,
                              "reason": (f"not in your baseline; comparing trace "
                                         f"evidence against your baseline date "
                                         f"{base_day.isoformat()}")}
            point_day = base_day
            evidence = [u for u in versions if u["written_at"]
                        and u["written_at"].date() > point_day]
            # an export that carries no version evidence at all (no write-up
            # history, no coverage block, no trace stamps) cannot support any
            # 'nothing changed' claim either — say unknown, do not guess
            has_evidence = (bool(versions) or bool(story.get("unassessed_coverage"))
                           or any("added" in s for s in story.get("sources") or []))
            if not has_evidence:
                change = {"status": "unknown",
                          "reason": ("this export carries no version evidence "
                                     "for this story; cannot compare against "
                                     "your baseline date")}
            else:
                new_to_you = _after(None, point_day, first_seen)
                change = _change_block(story, versions, evidence,
                                       point_dt=None, point_day=point_day,
                                       new_to_you=new_to_you)
            # the date bounds publication chronology only: without a stated
            # version for this story, no comparison here can say what the
            # caller actually saw — personal novelty stays an explicit
            # unknown, separate from the chronology above
            change["new_to_you"] = "unknown"
            change["new_to_you_note"] = (
                "you state no version for this story: only the paper's trace "
                "was compared against your stated date, so whether this is "
                "personally new to you is unknown")
        return {"story_id": sid, "title": story.get("title"),
                "baseline": baseline_block, "current_version": cur,
                "change": change}
    # the caller states a version: known only if the export's evidence has it
    point_dt = point_day = None
    if not versions:
        baseline_block = {"claimed_version": mine, "status": "unknown",
                          "provenance": prov,
                          "reason": ("this story has no write-up versions in "
                                     "the paper's evidence; your claim cannot "
                                     "be checked")}
    elif mine > (newest or 0):
        baseline_block = {"claimed_version": mine, "status": "unknown",
                          "provenance": prov,
                          "reason": (f"your version {mine} is ahead of this "
                                     f"export's newest write-up version "
                                     f"{newest} — the export may be older than "
                                     f"your memory")}
    elif any(u["version"] == mine for u in versions):
        mine_u = next(u for u in versions if u["version"] == mine)
        if mine == newest:
            reason = "your baseline version is the current write-up"
        else:
            reason = (f"baseline version {mine} of {newest} matches the "
                      "paper's write-up history")
        baseline_block = {"claimed_version": mine, "status": "known",
                          "provenance": prov, "reason": reason}
        point_dt = mine_u["written_at"]
        evidence = [u for u in versions if u["version"] > mine]
        change = _change_block(story, versions, evidence,
                              point_dt=point_dt, point_day=None, new_to_you=False)
    else:
        baseline_block = {"claimed_version": mine, "status": "unknown",
                          "provenance": prov,
                          "reason": (f"no version {mine} in this story's "
                                     f"write-up history (known versions: "
                                     f"1..{newest})")}
    if baseline_block["status"] == "unknown":
        change = {"status": "unknown",
                  "reason": ("your stated version cannot be tied to this "
                             "story's evidence; no diff is claimed — the "
                             "story payload carries the current write-up and "
                             "sources")}
    return {"story_id": sid, "title": story.get("title"),
            "baseline": baseline_block, "current_version": cur,
            "change": change}


def since_view(export: dict, baseline: dict) -> dict:
    """The launch brief: what changed since the caller's stated last read.

    Per story, classifies the caller's baseline against the export's evidence
    (known version vs unknown vs none stated), then the change: material
    (evidence-supported proposals), no supported update, or explicit unknown.
    Intervening editions come from the public build trace vs the caller's
    stated date — publication chronology only, never a claim that any
    edition was skipped or unread. Fails soft per story (says 'unknown')
    but never invents: no invented versions, no invented changes, no
    reading dates derived from versions or trace times, no claims about
    private stories, no delivery or access claims of any kind.

    Output contract is versioned (``brief_version``): v2 renamed the v1
    ``skipped_editions`` block to ``intervening_editions`` and kept the
    old key as a deprecated, always-null alias — legacy clients never see
    a silently renamed field and never receive a claim through the old
    name.
    """
    base_day = (date.fromisoformat(baseline["date"])
                if baseline.get("date") else None)
    cur_day = date.fromisoformat(str(export["edition"]["date"]))
    if base_day is None:
        intervening, note = None, (
            "unknown: your baseline states no edition date, so this surface "
            "cannot say which public editions intervened between your reads. "
            "It will not infer one from your stated versions, from trace "
            "times, or from the edition that contained an earlier version — "
            "readership of any edition is unknown here")
    elif "editions" not in export:
        intervening, note = None, (
            "unknown: this export carries no public build trace")
    else:
        intervening = [{"date": e["date"], "built_at": e["built_at"]}
                       for e in export.get("editions") or []
                       if base_day.isoformat() < e["date"] < cur_day.isoformat()]
        note = ("public editions BUILT after your stated baseline date and "
                "before the current one — publication chronology only. It is "
                "not evidence that any edition was skipped or unread, and it "
                "says nothing about delivery or access: whether you read any "
                "of them is unknown to this surface. The current edition is "
                "in hand and never listed")

    claimed = baseline.get("versions") or {}
    prov = (baseline.get("provenance") or {}).get("versions") or {}
    updates: list[dict] = []
    for sid, story in export["stories"].items():
        updates.append(_story_update(sid, story, claimed, base_day, prov))
    # stories the caller remembers that this public edition has no record of:
    # never confirm or deny them (they may be outside the window or non-public)
    for sid in claimed:
        if sid not in export["stories"]:
            updates.append({
                "story_id": sid, "title": None,
                "baseline": {"claimed_version": claimed[sid], "status": "unknown",
                             "provenance": prov.get(sid),
                             "reason": ("no public evidence for this story id in "
                                        "this edition — it may be outside the "
                                        "window or non-public; this surface "
                                        "cannot confirm or deny it")},
                "current_version": None,
                "change": {"status": "unknown",
                           "reason": ("unknown story in this public edition; "
                                      "nothing further can be said")}})
    order = {sid: k for k, sid in enumerate(export["stories"])}
    updates.sort(key=lambda u: (u["story_id"] not in order,  # export stories first
                                order.get(u["story_id"], 0)))
    return {
        "schema_version": export["schema_version"],
        "brief_version": BRIEF_VERSION,
        "kind": export["kind"],
        "reader": export["reader"],
        "edition": export["edition"],
        "privacy": export["privacy"],
        "citation": export["citation"],
        "semantics": SEMANTICS,
        "baseline": {"reader": baseline["reader"], "date": baseline.get("date"),
                     "versions": claimed,
                     "provenance": (baseline.get("provenance")
                                    or {"date": None, "versions": {}})},
        "intervening_editions": intervening,
        "intervening_editions_note": note,
        # deprecated since brief v2 (see semantics.brief_version): retained
        # so legacy clients never meet a silently renamed field, and pinned
        # to null so it can never again deliver an unread claim
        "skipped_editions": None,
        "skipped_editions_note": (
            "deprecated since brief v2: 'skipped_editions' never carries a "
            "list again — readership of any edition is unknown to this "
            "surface, so this field stays null and claims nothing. Read "
            "'intervening_editions' instead: the public editions BUILT "
            "between your stated date and the current one — publication "
            "chronology only, never an unread claim"),
        "updates": updates,
        "approval": APPROVAL,
    }
