"""Build the synthetic demo export for the read-only agent lookup commands.

Every story, outlet, author, event, and URL in the demo export is invented:
``*.example`` hosts, ``FAKE`` video ids, fictional places like Demo Ridge.
The fixture is deterministic apart from the edition clock: no randomness, no
network. ``edition.built_at`` and ``edition.date`` are read from the clock at
generation time so a freshly regenerated export is never refused as stale;
every other timestamp is a fixed invented value.

Usage:
    python docs/agent/demo/make_demo_export.py    # refresh export.json here

    import make_demo_export as demo
    demo.build(pathlib.Path("/tmp/demo"))        # -> /tmp/demo/export.json
"""

import datetime
import hashlib
import json
import pathlib
import urllib.parse

SCHEMA_VERSION = 1
EXPORT_KIND = "gaceta.agent-export"
READER = {"id": "demo", "name": "Demo Reader"}

CITATION_POLICY = (
    "Sources are the only citable evidence: cite a source only for what it "
    "explicitly supports. The summary field is model-written text, never "
    "evidence. Links and model summaries are never promoted to 'confirmed' "
    "claims; if the sources do not support something, say so."
)

# The invented +01:00 zone used by every fixed timestamp in the fixture.
DEMO_TZ = datetime.timezone(datetime.timedelta(hours=1))

# Unix seconds for the demo-trail history stamps: history[].at is an int.
# 2026-11-15T09:00:00+01:00 and 2026-11-17T09:30:00+01:00.
HISTORY_V1_AT = int(
    datetime.datetime(2026, 11, 15, 9, 0, tzinfo=DEMO_TZ).timestamp()
)
HISTORY_V2_AT = int(
    datetime.datetime(2026, 11, 17, 9, 30, tzinfo=DEMO_TZ).timestamp()
)

# Keys whose presence makes the agent reader refuse the whole export.
# folio_code lives under edition; a folio block must not exist.
FORBIDDEN_KEYS = frozenset(
    {
        "why_it_matters",
        "score_reasons",
        "media",
        "portada",
        "sections",
        "discovery",
        "conditions",
        "rev",
        "next_update",
        "folio",
    }
)

STATUSES = ("developing", "settled", "corrected")
SOURCE_KINDS = ("rss", "youtube", "nostr", "hn")
SOURCE_ROLES = ("origin", "update", "dissent")

TRAIL_SUMMARY = (
    "The Demo Loop footpath is passable again after its boardwalk repairs, "
    "and a walker's report confirms the new boards hold. Both sources agree "
    "the detour sign stays until the replacement fence is installed."
)


def _hex(label, length):
    """Return a stable hex digest for a synthetic identifier."""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:length]


FOLIO_CODE = _hex("demo-agent-export-folio", 6).upper()
TRAIL_NOTE_URL = "https://notes.example/notes/" + _hex("demo-trail-note", 12)


def _source(title, url, author, kind, published, role):
    """Return one synthetic source entry."""
    return {
        "title": title,
        "url": url,
        "author": author,
        "kind": kind,
        "published": published,
        "role": role,
    }


def _stories():
    """Return the three synthetic stories in stable order."""
    return {
        "demo-aurora": {
            "title": "Shimmer over Demo Ridge draws sky watchers",
            "status": "developing",
            "privacy": "public",
            "summary": (
                "Two sources describe a slow, aurora-like shimmer over Demo "
                "Ridge on the night of 16 November: a Gazette note from the "
                "next morning and a timelapse from Quiet Orbit Lab. They "
                "agree on the timing and on little else, so the cause "
                "remains an open question."
            ),
            "summary_written_by": "model",
            "confidence": 0.7,
            "first_seen": "2026-11-17T09:00:00+01:00",
            "last_seen": "2026-11-17T18:30:00+01:00",
            "sources": [
                _source(
                    "Sky watchers report a shimmer over Demo Ridge",
                    "https://gazette.example/sky/demo-ridge-shimmer",
                    "The Demo Gazette",
                    "rss",
                    "2026-11-17T09:00:00+01:00",
                    "origin",
                ),
                _source(
                    "Night sky timelapse: the shimmer over Demo Ridge",
                    "https://www.youtube.com/watch?v=FAKEdemo01",
                    "Quiet Orbit Lab",
                    "youtube",
                    "2026-11-17T18:00:00+01:00",
                    "update",
                ),
            ],
            "corrections": [
                {
                    "said": "The shimmer was visible on two consecutive nights.",
                    "now": (
                        "The Gazette note covers one night only; no source "
                        "mentions a second night."
                    ),
                    "at": "2026-11-18T10:00:00+01:00",
                }
            ],
        },
        "demo-trail": {
            "title": "Demo Loop trail reopens after boardwalk repairs",
            "status": "settled",
            "privacy": "public",
            "summary": TRAIL_SUMMARY,
            "summary_written_by": "model",
            "confidence": 0.8,
            "first_seen": "2026-11-15T09:00:00+01:00",
            "last_seen": "2026-11-17T09:30:00+01:00",
            "sources": [
                _source(
                    "Demo Loop boardwalk repairs finished",
                    "https://trails.example/demo-loop-reopens",
                    "Trail Notes Weekly",
                    "rss",
                    "2026-11-15T08:30:00+01:00",
                    "origin",
                ),
                _source(
                    "Walk report: Demo Loop on the first dry weekend",
                    TRAIL_NOTE_URL,
                    "bracken-lantern",
                    "nostr",
                    "2026-11-17T09:00:00+01:00",
                    "update",
                ),
            ],
            "history": [
                {
                    "version": 1,
                    "at": HISTORY_V1_AT,
                    "summary": (
                        "The Demo Loop footpath has reopened after repairs "
                        "to its boardwalk."
                    ),
                    "what_changed": "First write-up from the reopening note.",
                },
                {
                    "version": 2,
                    "at": HISTORY_V2_AT,
                    "summary": TRAIL_SUMMARY,
                    "what_changed": (
                        "Added the walker's note; the detour sign now has a "
                        "stated end condition."
                    ),
                },
            ],
        },
        "demo-quiet": {
            "title": "Demo Valley seed library opens its winter swap",
            "status": "settled",
            "privacy": "public",
            "summary": None,
            "summary_written_by": "model",
            "confidence": None,
            "first_seen": "2026-11-16T09:30:00+01:00",
            "last_seen": "2026-11-16T09:30:00+01:00",
            "sources": [
                _source(
                    "Demo Valley seed library opens its winter swap",
                    "https://gazette.example/demo-valley-seed-swap",
                    "The Demo Gazette",
                    "rss",
                    "2026-11-16T09:00:00+01:00",
                    "origin",
                ),
            ],
        },
    }


def _walk(node):
    """Yield every (key, value) pair in nested dicts and lists."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _check(export):
    """Fail fast on anything the agent reader refuses the whole export for."""
    assert export["schema_version"] == SCHEMA_VERSION
    assert export["kind"] == EXPORT_KIND
    assert export["reader"] == READER
    assert export["privacy"] == {"max_level": "public"}
    assert export["citation"]["policy"] == CITATION_POLICY
    built_at = datetime.datetime.fromisoformat(export["edition"]["built_at"])
    assert built_at.utcoffset() is not None

    stories = export["stories"]
    assert tuple(stories) == ("demo-aurora", "demo-trail", "demo-quiet")
    assert export["edition"]["stories_total"] == len(stories)
    for key, _value in _walk(export):
        assert key not in FORBIDDEN_KEYS, key
    for story in stories.values():
        assert story["privacy"] == "public"
        assert story["status"] in STATUSES
        for source in story["sources"]:
            url = urllib.parse.urlsplit(source["url"])
            assert url.scheme in ("http", "https"), source["url"]
            assert url.hostname, source["url"]
            assert source["kind"] in SOURCE_KINDS
            assert source["role"] in SOURCE_ROLES

    aurora = stories["demo-aurora"]
    assert aurora["status"] == "developing"
    assert len(aurora["sources"]) == 2 and len(aurora["corrections"]) == 1
    assert any("v=FAKE" in s["url"] for s in aurora["sources"])
    trail = stories["demo-trail"]
    assert [entry["version"] for entry in trail["history"]] == [1, 2]
    assert trail["history"][1]["what_changed"]
    quiet = stories["demo-quiet"]
    assert quiet["summary"] is None and quiet["confidence"] is None
    assert len(quiet["sources"]) == 1


def build(dest: pathlib.Path) -> pathlib.Path:
    """Create dest if missing, write dest/"export.json", and return its path."""
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now().astimezone()
    stories = _stories()
    export = {
        "schema_version": SCHEMA_VERSION,
        "kind": EXPORT_KIND,
        "reader": dict(READER),
        "edition": {
            "date": now.date().isoformat(),
            "built_at": now.isoformat(timespec="seconds"),
            "folio_code": FOLIO_CODE,
            "stories_total": len(stories),
        },
        "privacy": {"max_level": "public"},
        "citation": {
            "policy": CITATION_POLICY,
            "summary_origin": "model",
            "confirmed_exists": False,
        },
        "stories": stories,
    }
    _check(export)

    path = dest / "export.json"
    path.write_text(
        json.dumps(export, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    print(build(pathlib.Path(__file__).resolve().parent))
