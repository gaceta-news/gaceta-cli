"""The synthetic desk packet (src/gaceta/assets/desk): deterministic replay,
blind/key isolation, packet structure, source-language links, accessibility
of the rendered sample, offline ingest→embed→cluster plumbing on the
FakeEmbedder only, and attribution.

All packet content is invented (fictional Valdora, reserved *.example hosts).
Nothing here touches the network, and no test asserts anything about any
live model — the recorded baseline is the deterministic FakeEmbedder's.
"""
from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime, timezone
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path

from gaceta.editor.cluster import cluster, embed_items
from gaceta.evals import clustering as ec
from gaceta.ingest import ingest
from gaceta.providers.embed import FakeEmbedder
from gaceta.sources.rss import RssAdapter, parse_feed

ROOT = Path(__file__).resolve().parents[1]
DESK = ROOT / "src" / "gaceta" / "assets" / "desk"
KEY = ROOT / "tests" / "fixtures" / "desk"

# A fixed 'now' comfortably inside max_age_days of the packet's window, so
# the offline feed test never depends on the real clock.
NOW = int(datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc).timestamp())


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "make_desk_packet", DESK / "make_desk_packet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MDP = _load_generator()


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


def desk_text() -> str:
    return (DESK / "index.html").read_text(encoding="utf-8")


# ------------------------------------------------------------ replay

def test_replay_is_byte_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    wrote_a = MDP.build(a / "desk", a / "key")
    MDP.build(b / "desk", b / "key")
    assert len(wrote_a) == 13
    assert tree(a) == tree(b), "two fresh builds differ"

    fresh = tree(a / "desk")
    committed = tree(DESK)
    assert set(fresh) <= set(committed), "committed blind tree is missing files"
    assert {k: committed[k] for k in fresh} == fresh, "committed blind tree is not a rebuild"

    fresh_key = tree(a / "key")
    committed_key = tree(KEY)
    assert set(fresh_key) <= set(committed_key)
    assert {k: committed_key[k] for k in fresh_key} == fresh_key, "committed key differs"


def test_shipped_unit_is_complete():
    for name in ("packet.json", "feed.rss", "index.html", "NOTICE.md", "CC-BY-4.0.txt"):
        assert (DESK / name).is_file(), name
    for doc in MDP.DOCUMENTS:
        assert (DESK / "sources" / f"{doc['id']}.html").is_file(), doc["id"]
    assert (KEY / "oracle.json").is_file() and (KEY / "golden.json").is_file()


# ------------------------------------------------------------ isolation

def _shippable_files(root: Path):
    """Text files of the shipped unit — build noise (__pycache__) excluded."""
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or path.suffix == ".pyc":
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def test_shipped_tree_stays_blind():
    # The generator is repo-side tooling and deliberately embeds the key; the
    # NOTICE forbids shipping it blind-side, so it is the one exception here.
    for path in _shippable_files(DESK):
        if path.name == "make_desk_packet.py":
            continue
        low = path.read_text(encoding="utf-8").lower()
        for token in MDP.ORACLE_ONLY_TOKENS:
            assert token not in low, f"{path.name} leaks {token!r}"
        assert "tests/fixtures" not in low, f"{path.name} points at the key"
    packet = json.loads((DESK / "packet.json").read_text(encoding="utf-8"))
    allowed = {"id", "date", "published", "language", "kind", "outlet", "url",
               "title", "subtitle", "body", "gazette_ref", "original"}
    for doc in packet["documents"]:
        assert set(doc) <= allowed, "documents must carry only public fields"


# ------------------------------------------------------------ structure

def test_packet_structure():
    packet = json.loads((DESK / "packet.json").read_text(encoding="utf-8"))
    docs = packet["documents"]
    assert [d["id"] for d in docs] == [d["id"] for d in MDP.DOCUMENTS]
    assert packet["window"] == {"start": "2026-11-16", "end": "2026-11-18",
                                "days": 3, "tz_offset": "-06:00"}
    assert packet["counts"]["by_language"] == {"es": 5, "en": 3}
    assert packet["counts"]["documents"] == 8
    for day in ("2026-11-16", "2026-11-17", "2026-11-18"):
        langs = {d["language"] for d in docs if d["date"] == day}
        assert langs == {"es", "en"}, f"day {day} must carry both languages"
    pubs = [d["published"] for d in docs]
    assert pubs == sorted(pubs), "documents must be in published order"
    for doc in docs:
        assert doc["published"][:10] == doc["date"]
        assert doc["published"].endswith("-06:00")
    kinds = [d["id"] for d in docs if d["kind"] == "gazette"]
    assert kinds == ["gaz-4127-arav-117", "gaz-4129-arev-098"]
    for doc in docs:
        if doc["kind"] == "gazette":
            assert doc["gazette_ref"]
        else:
            assert "gazette_ref" not in doc


# ------------------------------------------------------------ source language

def test_source_language_links():
    for doc in MDP.DOCUMENTS:
        snap = (DESK / "sources" / f"{doc['id']}.html").read_text(encoding="utf-8")
        if doc["kind"] == "gazette":
            assert "hreflang" not in snap, "originals are the end of the chain"
            assert "original" not in doc
            continue
        orig = doc["original"]
        canonical = MDP.BY_ID[orig["id"]]
        assert canonical["kind"] == "gazette" and canonical["language"] == "es"
        assert orig["url"] == canonical["url"], "must cite the record's own url"
        assert f'href="{orig["id"]}.html" hreflang="es"' in snap, doc["id"]
    # the desk renders the same chain for all six derivative documents
    desk = desk_text()
    assert desk.count('hreflang="es"') == 6


# ------------------------------------------------------------ accessibility

class _DeskParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.decl = ""
        self.tags: list[tuple[str, dict]] = []
        self.ids: set[str] = set()
        self.hrefs: list[str] = []
        self.headings: list[str] = []
        self.times_missing_datetime = 0
        self.article_langs: list[str | None] = []
        self.nav_with_label = 0
        self.text: list[str] = []

    def handle_decl(self, decl):
        self.decl = decl

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if "id" in a:
            self.ids.add(a["id"])
        if "href" in a:
            self.hrefs.append(a["href"])
        if tag in ("h1", "h2", "h3", "h4"):
            self.headings.append(tag)
        if tag == "time" and "datetime" not in a:
            self.times_missing_datetime += 1
        if tag == "article":
            self.article_langs.append(a.get("lang"))
        if tag == "nav" and "aria-label" in a:
            self.nav_with_label += 1

    def handle_data(self, data):
        self.text.append(data)


def test_desk_page_is_accessible():
    desk = desk_text()
    p = _DeskParser()
    p.feed(desk)
    text = "".join(p.text)

    assert p.decl.lower() == "doctype html"
    html_tag = next(a for t, a in p.tags if t == "html")
    assert html_tag["lang"] == "en"
    metas = [a for t, a in p.tags if t == "meta"]
    assert any(a.get("charset") == "utf-8" for a in metas)
    assert any((a.get("name") or "") == "viewport" for a in metas)
    assert any((a.get("name") or "") == "description" for a in metas)

    assert p.headings.count("h1") == 1 and p.headings[0] == "h1"
    level = 0
    for h in p.headings:  # never skip a level going down
        n = int(h[1])
        assert n <= level + 1, f"heading skip: {p.headings}"
        level = n

    assert "#main" in p.hrefs and "main" in p.ids       # skip link works
    assert p.nav_with_label >= 1
    assert p.times_missing_datetime == 0
    assert p.article_langs == [d["language"] for d in MDP.DOCUMENTS]

    for href in p.hrefs:
        if href.startswith("#"):
            assert href[1:] in p.ids, f"dangling anchor {href}"
        else:
            assert not href.startswith(("http:", "https:")), href
            assert (DESK / href).exists(), href

    assert "a:focus-visible" in desk                     # keyboard focus styles
    assert "<script" not in desk.lower()                 # works without JS
    # badges carry text, never colour alone
    for word in ("GAZETTE", "NEWS", "ANALYSIS", "ES", "EN"):
        assert word in text
    # every document renders, with its own-language dateline
    for doc in MDP.DOCUMENTS:
        assert html_escape(doc["title"]) in desk, doc["id"]
        assert MDP._dateline(doc) in desk, doc["id"]
    assert "de noviembre de 2026" in text and "November 2026" in text


# ------------------------------------------------------------ offline

def test_every_link_is_local_and_hosts_are_invented():
    pages = [DESK / "index.html", *sorted((DESK / "sources").glob("*.html"))]
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert not re.search(r'(?:href|src)="https?://', text), page.name
        base = DESK if page.name == "index.html" else DESK / "sources"
        for href in re.findall(r'href="([^"]+)"', text):
            if href.startswith("#"):
                continue  # in-page anchors are covered by the accessibility test
            assert (base / href).exists(), f"{page.name}: {href}"
    for name in ("packet.json", "feed.rss"):
        text = (DESK / name).read_text(encoding="utf-8")
        for url in re.findall(r"https://[^\s\"<>]+", text):
            assert re.match(r"https://[^/]+\.example(/|$)", url), url


def test_no_private_data_markers():
    for path in _shippable_files(DESK):
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", text), path.name
        assert "tel:" not in text, path.name


def test_generator_is_offline_code():
    src = (DESK / "make_desk_packet.py").read_text(encoding="utf-8")
    for mod in ("socket", "urllib", "http.client", "requests", "ftplib", "smtplib"):
        assert not re.search(rf"^\s*(import|from)\s+{re.escape(mod)}\b", src, re.M), mod
    assert "datetime.now" not in src and "time.time" not in src


# ------------------------------------------------------------ feed → pipeline

def _desk_adapter():
    return RssAdapter(
        {"feeds": [{"id": "desk", "name": "Desk packet", "file": str(DESK / "feed.rss")}]},
        now=lambda: NOW)


def test_feed_round_trips_through_the_rss_parser():
    entries = parse_feed((DESK / "feed.rss").read_bytes())
    assert len(entries) == 8
    assert [e["guid"] for e in entries] == [d["id"] for d in MDP.DOCUMENTS]
    for entry, doc in zip(entries, MDP.DOCUMENTS):
        assert entry["title"] == doc["title"]
        assert entry["url"] == doc["url"]
        assert entry["body"] == "\n\n".join(doc["body"])
        assert entry["published"] == int(
            datetime.fromisoformat(doc["published"]).timestamp())


def test_offline_pipeline_matches_recorded_baseline(env):
    raw = list(_desk_adapter().items())
    assert len(raw) == 8 and all(i.verdict == "noted" for i in raw)
    assert all(i.source.content_public for i in raw)

    first = ingest(env.conn, _desk_adapter(), env.cfg)
    assert first["new"] == 8 and first["updated"] == 0
    second = ingest(env.conn, _desk_adapter(), env.cfg)   # idempotent, no network
    assert second["new"] == 0 and second["updated"] == 0

    labels = {r[0] for r in env.conn.execute(
        "SELECT DISTINCT item_privacy FROM annotations WHERE reader_id = ?",
        (env.cfg["id"],))}
    assert labels == {"public"}  # feed provenance-public; no canary term hit

    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    result = cluster(env.conn, env.cfg["id"], env.cfg)
    assert result["threshold"] == 0.70 and result["window_days"] == 21
    assert result["model"] == "fake:256" and result["n_stories"] == 3

    doc_of_item = {
        r["id"]: next(d["id"] for d in MDP.DOCUMENTS if d["url"] == r["url"])
        for r in env.conn.execute("SELECT id, url FROM items")
    }
    assert len(doc_of_item) == 8

    pred: dict[str, list[str]] = {}
    for row in env.conn.execute(
            "SELECT si.story_id, si.item_id, si.role, i.published "
            "FROM story_items si JOIN stories s ON s.id = si.story_id "
            "JOIN items i ON i.id = si.item_id "
            "WHERE s.reader_id = ? ORDER BY si.story_id, i.published, si.item_id",
            (env.cfg["id"],)):
        pred.setdefault(row["story_id"], []).append(doc_of_item[row["item_id"]])
    got = sorted(sorted(members) for members in pred.values())
    baseline = sorted(sorted(s) for s in
                      MDP.ORACLE["fake_embedder_baseline"]["expected_stories"])
    assert got == baseline

    # the decision's Spanish story starts at the gazette record
    target = next(m for m in pred.values() if "gaz-4127-arav-117" in m)
    assert target[0] == "gaz-4127-arav-117"
    # the English coverage of the unrelated event stays a singleton
    alone = next(m for m in pred.values() if "vmp-0412" in m)
    assert alone == ["vmp-0412"]

    golden = ec.load_golden(KEY / "golden.json")
    metrics = ec.evaluate([list(members) for members in pred.values()], golden)
    expected = MDP.ORACLE["fake_embedder_baseline"]["expected_metrics"]
    assert metrics["n_items"] == 8
    for key in ("precision", "recall", "f1", "b3_precision", "b3_recall",
                "b3_f1", "n_stories"):
        assert metrics[key] == expected[key], key


# ------------------------------------------------------------ key side

def test_key_partitions_and_rubric():
    oracle = json.loads((KEY / "oracle.json").read_text(encoding="utf-8"))
    doc_ids = {d["id"] for d in MDP.DOCUMENTS}
    assert set(oracle["per_document"]) == doc_ids
    classes = [v["classification"] for v in oracle["per_document"].values()]
    assert classes.count("material_decision") == 1
    assert classes.count("repeat") == 5
    assert classes.count("similar_unrelated_event") == 2
    assert {e["label"] for e in oracle["events"]} == {
        "material_decision", "similar_unrelated_event"}
    seen = [d for e in oracle["events"] for d in e["documents"]]
    assert sorted(seen) == sorted(doc_ids), "events must partition the packet"
    for event in oracle["events"]:
        for doc_id in event["documents"]:
            assert oracle["per_document"][doc_id]["event"] == event["key"]

    target = next(e for e in oracle["events"] if e["label"] == "material_decision")
    assert target["canonical_doc"] == "gaz-4127-arav-117"
    assert len(target["documents"]) == 6
    facts = target["expected_facts"]
    gazette = MDP.BY_ID["gaz-4127-arav-117"]
    assert facts["instrument"] in gazette["title"]
    assert facts["fine"] in "\n".join(gazette["body"])

    assert {r["id"] for r in oracle["rubric"]} == {
        "identify", "collapse-repeats", "reject-similar-unrelated",
        "source-language"}

    golden = ec.load_golden(KEY / "golden.json")  # validates partitions too
    other = next(e for e in oracle["events"] if e["label"] != "material_decision")
    assert sorted(golden["clusters"][0]) == sorted(target["documents"])
    assert sorted(golden["clusters"][1]) == sorted(other["documents"])
    baseline = oracle["fake_embedder_baseline"]["expected_stories"]
    assert sorted(i for s in baseline for i in s) == sorted(doc_ids)


# ------------------------------------------------------------ attribution

def test_attribution_and_fictional_entities_are_declared():
    notice = (DESK / "NOTICE.md").read_text(encoding="utf-8")
    license_text = (DESK / "CC-BY-4.0.txt").read_text(encoding="utf-8")
    assert "CC BY 4.0" in notice
    assert "gaceta-news/gaceta-cli" in notice          # the credit line
    assert "coincidental" in notice
    assert "make_desk_packet.py" in notice            # generator stays repo-side

    for doc in MDP.DOCUMENTS:
        assert doc["outlet"] in notice, doc["outlet"]
    for name in ("Compañía Minera Cerro Alto, S.A.", "Generadora Marbrisa, S.A.",
                 "Autoridad Reguladora de Aguas de Valdora (ARAV)",
                 "Autoridad Reguladora de Energía de Valdora (AREV)",
                 "Helena Marsal", "Ferrer & Linares", "Serranía del Toldo",
                 "Puerto Gris", "Vado Prieto", "valdoro"):
        assert name in notice, name
    for host in {re.sub(r"^https://", "", d["url"]).split("/")[0] for d in MDP.DOCUMENTS}:
        assert host in notice, host

    assert "Attribution 4.0 International" in license_text
    assert "Section 3 -- License Conditions." in license_text
    assert "a. Attribution." in license_text
