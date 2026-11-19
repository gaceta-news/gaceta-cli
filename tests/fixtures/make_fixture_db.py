"""Build a synthetic collector sqlite database and note library for tests.

Every item, channel, author, and URL here is invented. The fixture is
deterministic: no randomness, no clocks, no network. It exists so tests can
exercise gaceta's read-only sqlite source adapter, privacy canaries, and library
format without touching real data.

Usage:
    import make_fixture_db as fixture
    fixture.build(pathlib.Path("/tmp/fixture"))
"""

import datetime
import hashlib
import pathlib
import sqlite3

SCHEMA = """CREATE TABLE items(rowid INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, source TEXT, author TEXT, title TEXT, url TEXT, published INTEGER, body TEXT, status TEXT DEFAULT 'pending', verdict TEXT, note TEXT, seen_at INTEGER, done_at INTEGER);
CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE channels(channel_id TEXT PRIMARY KEY, title TEXT, rank INTEGER, last_video INTEGER DEFAULT 0, last_polled INTEGER DEFAULT 0, errors INTEGER DEFAULT 0);"""

PUBLISHED_AT = "2026-11-15T19:09:03"
PUBLISHED_EPOCH = int(
    datetime.datetime(2026, 11, 15, 19, 9, 3, tzinfo=datetime.timezone.utc).timestamp()
)

PINNED_TERMS = ("zorblax", "qq-secret-farm")
HOUSEHOLD_TERMS = ("glimmerhouse",)

def _hex(label, length):
    """Return a stable hex digest for a synthetic identifier."""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:length]


LIBRARY_IDS = (
    "yt:FAKEyt00002",
    "yt:FAKEyt00007",
    "nostr:" + _hex("nostr-5", 64),
    "hn:" + _hex("hn-1", 16),
    "trial-gazette:" + _hex("gazette-1", 16),
)


def _item(
    item_id,
    source,
    author,
    title,
    url,
    *,
    body="",
    status="pending",
    verdict=None,
    note="",
    offset=0,
):
    published = PUBLISHED_EPOCH - offset
    return {
        "id": item_id,
        "source": source,
        "author": author,
        "title": title,
        "url": url,
        "published": published,
        "body": body,
        "status": status,
        "verdict": verdict,
        "note": note,
        "seen_at": None if status == "pending" else PUBLISHED_EPOCH + 600,
        "done_at": PUBLISHED_EPOCH + 1800 if status == "done" else None,
    }


def _items():
    """Return the fixture items in stable insertion order."""
    return [
        # YouTube: Quiet Orbit Lab and Pixel Pantry are ordinary channels.
        # Zorblax Daily supplies the author-field canary and no other item.
        _item(
            "yt:FAKEyt00001",
            "youtube",
            "Zorblax Daily",
            "Orbital garden tour, ep. 4",
            "https://www.youtube.com/watch?v=FAKEyt00001",
            body="A slow tour of a balcony garden built from scavenged parts. The watering timer is the only bought piece.",
            status="done",
            verdict="told",
            offset=3600,
        ),
        _item(
            "yt:FAKEyt00002",
            "youtube",
            "Quiet Orbit Lab",
            "Building a silent pump from scrap",
            "https://www.youtube.com/watch?v=FAKEyt00002",
            body="A workshop build with an improvised seal and a very patient leak test. The final flow rate is modest but stable.",
            status="done",
            verdict="noted",
            note="A calm walkthrough of a pump build. The improvised seal is the interesting part.",
        ),
        _item(
            "yt:FAKEyt00003",
            "youtube",
            "Quiet Orbit Lab",
            "Why my greenhouse failed twice",
            "https://www.youtube.com/watch?v=FAKEyt00003",
            body="Two seasons of mistakes condensed into twelve minutes. Ventilation mattered more than insulation.",
            status="baseline",
            verdict="skip",
            offset=7200,
        ),
        _item(
            "yt:FAKEyt00004",
            "youtube",
            "Pixel Pantry",
            "Five pantry meals under ten minutes",
            "https://www.youtube.com/watch?v=FAKEyt00004",
            body="Beans, rice, and one tired onion become five different dinners. The third one is the keeper.",
            status="done",
            verdict="told",
            offset=10800,
        ),
        _item(
            "yt:FAKEyt00005",
            "youtube",
            "Pixel Pantry",
            "The cast-iron myth, revisited",
            "https://www.youtube.com/watch?v=FAKEyt00005",
            body="A test kitchen tries the old seasoning advice and finds most of it harmless. One step is actively unhelpful.",
            offset=14400,
        ),
        _item(
            "yt:FAKEyt00006",
            "youtube",
            "Quiet Orbit Lab",
            "A quiet week in the workshop",
            "https://www.youtube.com/watch?v=FAKEyt00006",
            body="Small repairs, a failed jig, and a shelf that finally sits level. No grand project this week.",
            status="done",
            verdict="",
            offset=18000,
        ),
        _item(
            "yt:FAKEyt00007",
            "youtube",
            "Pixel Pantry",
            "Bread without a dutch oven",
            "https://www.youtube.com/watch?v=FAKEyt00007",
            body="A tray of water and a hot stone stand in for the usual covered pot. The crust is different but good.",
            status="baseline",
            verdict="noted",
            note="Useful for the weekend bake. The no-dutch-oven method needs a careful preheat.",
        ),
        _item(
            "yt:FAKEyt00008",
            "youtube",
            "Quiet Orbit Lab",
            "Night market lamps, one year later",
            "https://www.youtube.com/watch?v=FAKEyt00008",
            body="The solar lamps from last year mostly survived the winter. Two failed connectors explain the dark corner.",
            status="done",
            offset=21600,
        ),
        # Nostr: canaries live in one title, one body, and two bodies with the
        # household word. The note-only canary never appears in scanned fields.
        _item(
            "nostr:" + _hex("nostr-1", 64),
            "nostr",
            "nadia-quill",
            "Zorblax protocol notes, part two",
            "https://example.com/notes/" + _hex("nostr-url-1", 12),
            body="The second pass covers retries and a short comparison with the older draft. The diagrams are clearer this time.",
            status="done",
            verdict="skip",
            offset=25200,
        ),
        _item(
            "nostr:" + _hex("nostr-2", 64),
            "nostr",
            "ferro-kite",
            "On slow software",
            "https://example.com/notes/" + _hex("nostr-url-2", 12),
            body="The zorblax handshake is simpler than it looks. Two round trips and a checksum are enough for the demo.",
            status="done",
            verdict="",
            offset=28800,
        ),
        _item(
            "nostr:" + _hex("nostr-3", 64),
            "nostr",
            "moss-and-ink",
            "A walk through the fog market",
            "https://example.com/notes/" + _hex("nostr-url-3", 12),
            body="The stalls near Glimmerhouse Lane open late and close early. The bakery is the only reliable landmark.",
            status="done",
            verdict="skip",
            offset=32400,
        ),
        _item(
            "nostr:" + _hex("nostr-4", 64),
            "nostr",
            "ferro-kite",
            "Notes on a repair club",
            "https://example.com/notes/" + _hex("nostr-url-4", 12),
            body="Six people, one broken lamp, and a box of donated screws. The club meets on the first dry Sunday.",
            status="baseline",
            offset=36000,
        ),
        _item(
            "nostr:" + _hex("nostr-5", 64),
            "nostr",
            "nadia-quill",
            "Reading log: week 46",
            "https://example.com/notes/" + _hex("nostr-url-5", 12),
            body="Three essays and a short manual, all finished on the train. The manual was the surprise of the week.",
            status="done",
            verdict="noted",
            note="Worth keeping for the reading list. A zorblax mention lives only in this annotation.",
            offset=39600,
        ),
        _item(
            "nostr:" + _hex("nostr-6", 64),
            "nostr",
            "moss-and-ink",
            "The quiet cost of a newsletter",
            "https://example.com/notes/" + _hex("nostr-url-6", 12),
            body="Our newsletter runs from a kitchen table in Glimmerhouse, which keeps costs low and deadlines soft.",
            offset=43200,
        ),
        _item(
            "nostr:" + _hex("nostr-7", 64),
            "nostr",
            "tallow-press",
            "Three small tools I keep",
            "https://example.com/notes/" + _hex("nostr-url-7", 12),
            body="A folding rule, a worn awl, and a lamp that refuses to die. None of them were expensive.",
            status="baseline",
            verdict="",
            offset=46800,
        ),
        _item(
            "nostr:" + _hex("nostr-8", 64),
            "nostr",
            "tallow-press",
            "A reply to nobody in particular",
            "https://example.com/notes/" + _hex("nostr-url-8", 12),
            body="A short answer to a question that arrived twice. The second copy was the one that made sense.",
            offset=50400,
        ),
        # Hacker-news-like items.
        _item(
            "hn:" + _hex("hn-1", 16),
            "hn",
            "greybeard-88",
            "Ask: how do you archive a personal wiki?",
            "https://example.com/hn/" + _hex("hn-url-1", 10),
            body="A thread about durable formats, local search, and the yearly export nobody remembers to run.",
            status="done",
            verdict="noted",
            note="A good thread on long-lived archives. The comments add two practical options.",
        ),
        _item(
            "hn:" + _hex("hn-2", 16),
            "hn",
            "pikelet",
            "Show: a tiny feed reader in 400 lines",
            "https://example.com/hn/" + _hex("hn-url-2", 10),
            body="The author keeps one file of subscriptions and a very small cache. Polling is deliberately boring.",
            status="done",
            verdict="told",
            offset=5400,
        ),
        _item(
            "hn:" + _hex("hn-3", 16),
            "hn",
            "orbital-moss",
            "The case against dashboards",
            "https://example.com/hn/" + _hex("hn-url-3", 10),
            body="Charts multiplied until nobody read them. The proposed fix is a weekly sentence instead.",
            offset=10800,
        ),
        _item(
            "hn:" + _hex("hn-4", 16),
            "hn",
            "pikelet",
            "Why does my sqlite file keep growing?",
            "https://example.com/hn/" + _hex("hn-url-4", 10),
            body="Internal mirror credentials are rotated under qq-secret-farm policy. Do not quote this line in a post.",
            status="done",
            verdict="skip",
            offset=16200,
        ),
        _item(
            "hn:" + _hex("hn-5", 16),
            "hn",
            "greybeard-88",
            "Ask: durable notes for a decade",
            "https://example.com/hn/" + _hex("hn-url-5", 10),
            body="Plain files, a yearly index, and one offline copy in a drawer. The thread mostly agrees.",
            status="baseline",
            verdict="",
            offset=21600,
        ),
        _item(
            "hn:" + _hex("hn-6", 16),
            "hn",
            "quiet-loop",
            "A minimal weather station",
            "https://example.com/hn/" + _hex("hn-url-6", 10),
            body="Three sensors, a solar cell, and a small screen. The enclosure is an old food box.",
            status="done",
            offset=27000,
        ),
        # RSS-like local gazette.
        _item(
            "trial-gazette:" + _hex("gazette-1", 16),
            "trial-gazette",
            "Trial Gazette",
            "Council approves new lamp posts",
            "https://example.com/trial-gazette/" + _hex("gazette-url-1", 10),
            body="The east walk gets twelve lamps before winter. Work starts after the next market day.",
            status="done",
            verdict="noted",
            note="Local civic note with a clear timeline. Watch for the December follow-up.",
        ),
        _item(
            "trial-gazette:" + _hex("gazette-2", 16),
            "trial-gazette",
            "Trial Gazette",
            "River path reopens after repairs",
            "https://example.com/trial-gazette/" + _hex("gazette-url-2", 10),
            body="The washed-out section is passable again. The detour sign will stay until the fence is replaced.",
            status="done",
            verdict="skip",
            offset=7200,
        ),
        _item(
            "trial-gazette:" + _hex("gazette-3", 16),
            "trial-gazette",
            "Trial Gazette",
            "Library extends winter hours",
            "https://example.com/trial-gazette/" + _hex("gazette-url-3", 10),
            body="Weeknight closing moves to eight. The reading room keeps its quiet rule.",
            status="baseline",
            verdict="",
            offset=14400,
        ),
        _item(
            "trial-gazette:" + _hex("gazette-4", 16),
            "trial-gazette",
            "Trial Gazette",
            "Harvest fair moves indoors",
            "https://example.com/trial-gazette/" + _hex("gazette-url-4", 10),
            body="Rain pushed the stalls into the old hall. Entry stays free and the pie table is unchanged.",
            offset=21600,
        ),
        _item(
            "trial-gazette:" + _hex("gazette-5", 16),
            "trial-gazette",
            "Trial Gazette",
            "Budget hearing set for December",
            "https://example.com/trial-gazette/" + _hex("gazette-url-5", 10),
            body="Residents can comment on the draft before the council vote. Copies are at the front desk.",
            status="pending",
            verdict="",
            offset=28800,
        ),
        # Release feed with version-shaped ids.
        _item(
            "orbital-releases:v1_2_3",
            "orbital-releases",
            "Orbital Releases",
            "Orbital 1.2.3",
            "https://example.com/orbital/releases/v1_2_3",
            body="Fixes a cache eviction bug and adds a clearer error for missing config files. No schema changes.",
            status="done",
            verdict="told",
            offset=3600,
        ),
        _item(
            "orbital-releases:v1_3_0-rc1",
            "orbital-releases",
            "Orbital Releases",
            "Orbital 1.3.0 release candidate",
            "https://example.com/orbital/releases/v1_3_0-rc1",
            body="The candidate adds scheduled exports and a dry-run flag. Testing is open for one week.",
            offset=10800,
        ),
        _item(
            "orbital-releases:v1_2_2",
            "orbital-releases",
            "Orbital Releases",
            "Orbital 1.2.2",
            "https://example.com/orbital/releases/v1_2_2",
            body="A small patch for the importer and two documentation corrections. Upgrade is optional.",
            status="baseline",
            offset=18000,
        ),
    ]


def _channels():
    return [
        ("UCfakezorblax0000000001", "Zorblax Daily", 1, PUBLISHED_EPOCH, PUBLISHED_EPOCH + 120, 0),
        ("UCfakequietorbit0000002", "Quiet Orbit Lab", 2, PUBLISHED_EPOCH - 3600, PUBLISHED_EPOCH + 90, 0),
        ("UCfakepixelpantry000003", "Pixel Pantry", 3, PUBLISHED_EPOCH - 7200, PUBLISHED_EPOCH + 60, 1),
    ]


def _contains(item, terms):
    text = " ".join(item[field] for field in ("title", "body", "url", "author")).casefold()
    return any(term in text for term in terms)


def build(root: pathlib.Path) -> dict:
    """Create items.db and the note library under root, then return canary ids."""
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)

    items = _items()
    db_path = root / "items.db"
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            """
            INSERT INTO items
                (id, source, author, title, url, published, body, status,
                 verdict, note, seen_at, done_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item["id"],
                    item["source"],
                    item["author"],
                    item["title"],
                    item["url"],
                    item["published"],
                    item["body"],
                    item["status"],
                    item["verdict"],
                    item["note"],
                    item["seen_at"],
                    item["done_at"],
                )
                for item in items
            ],
        )
        conn.executemany(
            "INSERT INTO channels (channel_id, title, rank, last_video, last_polled, errors) VALUES (?, ?, ?, ?, ?, ?)",
            _channels(),
        )
        conn.executemany(
            "INSERT INTO kv (k, v) VALUES (?, ?)",
            [
                ("schema_version", "3"),
                ("last_sweep", str(PUBLISHED_EPOCH)),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    by_id = {item["id"]: item for item in items}
    library_dir = root / "library" / "2026-11"
    library_dir.mkdir(parents=True, exist_ok=True)
    for item_id in LIBRARY_IDS:
        item = by_id[item_id]
        assert item["verdict"] == "noted", item_id
        document = (
            f"# {item['title']}\n"
            "\n"
            f"source: {item['source']} | author: {item['author']} | published: {PUBLISHED_AT}\n"
            f"url: {item['url']}\n"
            "verdict: noted\n"
            "\n"
            f"{item['note']}\n"
        )
        filename = item_id.replace(":", "_") + ".md"
        (library_dir / filename).write_text(document, encoding="utf-8")

    pinned_ids = [item["id"] for item in items if _contains(item, PINNED_TERMS)]
    household_ids = [item["id"] for item in items if _contains(item, HOUSEHOLD_TERMS)]
    note_canary_ids = [
        item["id"] for item in items if "zorblax" in item["note"].casefold()
    ]
    told_ids = [item["id"] for item in items if item["verdict"] == "told"]

    assert len(items) == 30
    assert len(pinned_ids) == 4
    assert len(household_ids) == 2
    assert len(note_canary_ids) == 1
    assert len(told_ids) >= 3

    return {
        "n_items": len(items),
        "pinned_ids": pinned_ids,
        "household_ids": household_ids,
        "told_ids": told_ids,
        "library_ids": list(LIBRARY_IDS),
        "note_canary_ids": note_canary_ids,
    }
