"""Discovery (roadmap 6b): trials + suggestions extraction on a synthetic
fixture, domain/platform/substack rules, decision filtering, edition JSON
block, render of 'Fuentes nuevas', owner-only /api/source, open-kind
idempotency + taste weight. All data is invented; collector's db is only
ever opened read-only by the adapter."""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time

import pytest

from gaceta.cli import main as cli_main
from gaceta.discovery import discovery_block, suggestions, trial_sources
from gaceta.edition import build_edition
from gaceta.editor import cluster as run_cluster
from gaceta.editor.cluster import embed_items
from gaceta.editor.taste import adjustments
from gaceta.providers.embed import FakeEmbedder
from gaceta.render import html as render_html
from gaceta.serve import make_server
from gaceta.sources.sqlite import SqliteAdapter

from test_cluster import DAY, T0, add_item
from test_edition_build import _edition_date

SINCE = T0 - 7 * DAY
TRIAL_FEED = "feed:trial-mountain-review"
TRIAL_YT = "youtube:[trial] Quiet Orbit Lab"
CITED = "cited-daily.example"
HEX = "ab" * 32
HEX2 = "cd" * 32
READER = "testreader"
OWNER = "Test Reader"
VOICES = ("Ana Ruiz", "Ben Okafor", "Cy Lund")


def _story(env, sid, item_ids, title, last_seen=T0 + 2 * DAY):
    env.conn.execute(
        "INSERT INTO stories (id, reader_id, title, summary, status, confidence, "
        "privacy, first_seen, last_seen, updated_at) "
        "VALUES (?, ?, ?, 'summary', 'developing', 1.0, 'public', ?, ?, ?)",
        (sid, env.cfg["id"], title, T0, last_seen, last_seen),
    )
    env.conn.executemany(
        "INSERT INTO story_items (story_id, item_id, role) VALUES (?, ?, ?)",
        [(sid, iid, "origin" if k == 0 else "update") for k, iid in enumerate(item_ids)],
    )


def _seed(env):
    """Reader + two trial sources + cited/substack items + stories that carry them."""
    add_item(env, "d1", "D one", "plain body", "noted", "public", T0)  # reader + test:direct row
    env.conn.executemany(
        "INSERT OR IGNORE INTO sources (id, kind, name) VALUES (?, ?, ?)",
        [(TRIAL_FEED, "rss", "trial-mountain-review"),
         (TRIAL_YT, "youtube", "[trial] Quiet Orbit Lab")],
    )
    for iid, src, title, url, published in [
        ("t1", TRIAL_FEED, "Lamp post vote", "https://feeds.example/mr/1", T0),
        ("t2", TRIAL_FEED, "River path reopens", "https://feeds.example/mr/2", T0 + DAY),
        ("t3", TRIAL_YT, "Silent pump build", "https://www.youtube.com/watch?v=FAKEtrial01", T0 + DAY),
    ]:
        env.conn.execute(
            "INSERT INTO items (id, source_id, title, url, published, body, ingested_at, origin) "
            "VALUES (?, ?, ?, ?, ?, 'body', 0, 'test')",
            (iid, src, title, url, published),
        )
    for i in range(3):
        add_item(env, f"c{i}", f"Cited {i}",
                 f"see https://{CITED}/2026/11/river-path-report-{i} and https://github.com/x/y{i}",
                 "noted", "public", T0 + i * DAY)
        add_item(env, f"sa{i}", f"Same author {i}",
                 f"newsletter at https://zorbo.substack.com/p/zorbo-weekly-letter-{i}",
                 "noted", "public", T0 + i * DAY)
        add_item(env, f"os{i}", f"Other sub {i}",
                 f"read https://other-gazette.substack.com/p/valley-water-notes-{i}",
                 "noted", "public", T0 + i * DAY)
    env.conn.execute("UPDATE items SET author = 'Zorbo' WHERE id LIKE 'sa%'")
    for i, who in enumerate(VOICES):  # independent citers: one source, distinct authors
        env.conn.execute("UPDATE items SET author = ? WHERE id IN (?, ?)", (who, f"c{i}", f"os{i}"))
    _story(env, "stT", ["t1", "t2"], "Mountain review week")
    _story(env, "stU", ["t3"], "A quiet pump")
    for i in range(3):
        _story(env, f"stC{i}", [f"c{i}", f"sa{i}", f"os{i}"], f"Cited round {i}")
    _probed(env, CITED, "other-gazette.substack.com")
    env.conn.commit()


def _probed(env, *sites, ok=1):
    """source_probes rows as `gaceta sources probe` leaves them (6b v2 gate)."""
    for site in sites:
        env.conn.execute(
            "INSERT OR REPLACE INTO source_probes (site, checked_at, ok, feed_url, title, "
            "description, headlines) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (site, int(time.time()), ok, f"https://{site}/feed" if ok else None,
             f"{site.split('.')[0].title()} | Tagline here", f"About {site}.",
             json.dumps([f"Latest at {site}", "Second headline", "Third"])))


def _kv(env, rows):
    fw = sqlite3.connect(env.src_root / "items.db")
    with fw:
        for k, v in rows:
            fw.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, v))
    fw.close()


class FakeAdapter:
    name = "fake"

    def discovery_candidates(self, limit=8):
        return [{
            "key": f"nostr:{HEX}", "name": "Nadia Quill", "kind": "nostr",
            "reason": f"Mencionada 4 veces por gente que sigues",
            "url": f"https://njump.me/{HEX}",
        }]


# ------------------------------------------------------------ adapter hook

def test_sqlite_discovery_candidates(env):
    _kv(env, [
        ("nostr_candidates", json.dumps({HEX2: 2, HEX: 4})),
        ("nostr_names", json.dumps({HEX: "Nadia Quill"})),
    ])
    out = SqliteAdapter(env.src_root).discovery_candidates(limit=8)
    assert [c["key"] for c in out] == [f"nostr:{HEX}", f"nostr:{HEX2}"]  # by mentions
    assert out[0]["name"] == "Nadia Quill"
    assert out[1]["name"] == ""  # unnamed → empty; discovery skips it
    assert out[0]["reason"] == "Mencionada 4 veces por gente que sigues"
    assert out[0]["url"] == f"https://njump.me/{HEX}"
    assert out[0]["kind"] == "nostr"


def test_sqlite_candidates_survive_broken_kv(env):
    _kv(env, [("nostr_candidates", "not json")])
    assert SqliteAdapter(env.src_root).discovery_candidates() == []
    empty = env.src_root / "empty-items.db"
    conn = sqlite3.connect(empty)
    conn.executescript("CREATE TABLE items(id TEXT); CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);")
    conn.commit()
    conn.close()
    assert SqliteAdapter(env.src_root.parent / "missing").discovery_candidates() == []


# ------------------------------------------------------------ trials

def test_trial_sources(env):
    _seed(env)
    trials = {t["key"]: t for t in trial_sources(env.conn, READER, SINCE)}
    assert set(trials) == {TRIAL_FEED, TRIAL_YT}
    assert trials[TRIAL_FEED]["name"] == "Mountain Review"
    assert trials[TRIAL_FEED]["kind"] == "rss"
    assert trials[TRIAL_FEED]["n_items"] == 2
    assert trials[TRIAL_FEED]["story_ids"] == ["stT"]
    assert trials[TRIAL_YT]["name"] == "Quiet Orbit Lab"
    assert trials[TRIAL_YT]["kind"] == "youtube"
    assert trials[TRIAL_YT]["story_ids"] == ["stU"]


def test_trial_sources_respect_privacy_and_scores(env):
    _seed(env)
    env.conn.execute("UPDATE stories SET privacy = 'private' WHERE id = 'stT'")
    env.conn.commit()
    trials = {t["key"]: t for t in trial_sources(env.conn, READER, SINCE, max_privacy="public")}
    assert TRIAL_FEED not in trials  # its only story is private: nothing of it in this paper
    assert TRIAL_YT in trials


# ------------------------------------------------------------ suggestions

def test_suggestions_domains_platforms_substack(env):
    _seed(env)
    out = {s["key"]: s for s in suggestions(env.conn, READER, SINCE, adapters=[FakeAdapter()])}
    assert f"nostr:{HEX}" in out
    cited = out.get(f"host:{CITED}")
    assert cited and cited["reason"].startswith("Citada en 3 historias · por ")
    assert cited["kind"] == "site" and cited["url"] == f"https://{CITED}"
    assert f"host:other-gazette.substack.com" in out  # substack, NEW author
    # excluded: platforms, host of an existing source, same-author substack
    for absent in ("host:github.com", f"host:feeds.example", "host:youtube.com",
                   "host:zorbo.substack.com"):
        assert absent not in out, absent


def test_suggestions_skip_decided_except_stale_later(env):
    _seed(env)
    now = time.time()
    env.conn.executemany(
        "INSERT INTO source_decisions (reader_id, by_name, source_key, decision, at) "
        "VALUES (?, 'Test Reader', ?, ?, ?)",
        [(READER, f"host:{CITED}", "follow", int(now)),
         (READER, f"nostr:{HEX}", "later", int(now - 8 * 86400)),  # older than 7 days
         (READER, "host:other-gazette.substack.com", "later", int(now))],
    )
    env.conn.commit()
    keys = {s["key"] for s in suggestions(env.conn, READER, SINCE,
                                          adapters=[FakeAdapter()], now=now)}
    assert f"host:{CITED}" not in keys          # follow decided
    assert "host:other-gazette.substack.com" not in keys  # fresh later
    assert f"nostr:{HEX}" in keys               # stale later resurfaces


# ------------------------------------------------------------ edition + render

def _build_with_discovery(env):
    _seed(env)
    _kv(env, [("nostr_candidates", json.dumps({HEX: 4})),
                 ("nostr_names", json.dumps({HEX: "Nadia Quill"}))])
    return build_edition(env.conn, READER, env.cfg, date_str=_edition_date(),
                         max_privacy="private", force=True, data_dir=env.data_dir)


def test_edition_carries_discovery_block(env):
    ed = _build_with_discovery(env)
    disc = ed["discovery"]
    assert {t["key"] for t in disc["trials"]} == {TRIAL_FEED, TRIAL_YT}
    story_pool = set(ed["stories"]) | set(ed["portada"])
    for t in disc["trials"]:
        assert t["story_ids"] and set(t["story_ids"]) <= story_pool
    keys = {s["key"] for s in disc["suggestions"]}
    assert f"nostr:{HEX}" in keys and f"host:{CITED}" in keys


def test_edition_without_discovery_omits_block(env):
    add_item(env, "x1", "X one", "alpha", "noted", "public", T0)
    _story(env, "stX", ["x1"], "Only story")
    ed = build_edition(env.conn, READER, env.cfg, date_str=_edition_date(),
                       max_privacy="private", force=True, data_dir=env.data_dir)
    assert "discovery" not in ed


def test_render_discovery_section(env):
    ed = _build_with_discovery(env)
    doc = render_html.render(ed, "private")
    assert 'data-section="fuentes-nuevas"' in doc
    assert "Fuentes nuevas" in doc
    assert f'data-source-key="{TRIAL_FEED}"' in doc
    assert f'data-source-key="nostr:{HEX}"' in doc
    assert 'href="#stT"' in doc                 # trial → story anchor
    assert "A prueba · 2 entradas" in doc
    assert "Citada en 3 historias" in doc
    assert doc.rindex('data-section="') == doc.index('data-section="fuentes-nuevas"')  # last


# ------------------------------------------------------------ server

@pytest.fixture
def server(env):
    ed = _build_with_discovery(env)
    doc = render_html.render(ed, "private")
    site = env.data_dir / "site" / READER
    site.mkdir(parents=True, exist_ok=True)
    (site / f"{ed['date']}.html").write_text(doc, encoding="utf-8")
    (site / "index.html").write_text(doc, encoding="utf-8")
    httpd = make_server("127.0.0.1", 0, env.data_dir)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], env, ed
    finally:
        httpd.shutdown()
        httpd.server_close()


def req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def post_json(port, path, payload):
    return req(port, "POST", path, body=json.dumps(payload),
               headers={"Content-Type": "application/json"})


def test_source_decision_owner_only(server):
    port, env, ed = server
    payload = {"reader": READER, "source_key": TRIAL_FEED, "decision": "follow", "by": OWNER}
    status, _ = post_json(port, "/api/source", payload)
    assert status == 200
    row = env.conn.execute(
        "SELECT decision, by_name FROM source_decisions WHERE reader_id = ? AND source_key = ?",
        (READER, TRIAL_FEED)).fetchone()
    assert row["decision"] == "follow" and row["by_name"] == OWNER

    payload["by"] = "Bob"  # household reader: stored elsewhere, never decides
    status, body = post_json(port, "/api/source", payload)
    assert status == 403

    payload["decision"] = "maybe"
    payload["by"] = OWNER
    status, _ = post_json(port, "/api/source", payload)
    assert status == 400


def test_source_decision_key_must_be_in_edition(server):
    port, env, ed = server
    status, _ = post_json(port, "/api/source",
                          {"reader": READER, "source_key": "feed:trial-nope",
                           "decision": "drop", "by": OWNER})
    assert status == 400


def test_state_carries_owner_and_decisions(server):
    port, env, ed = server
    post_json(port, "/api/source",
              {"reader": READER, "source_key": TRIAL_YT, "decision": "later", "by": OWNER})
    from urllib.parse import quote
    status, body = req(port, "GET", f"/api/state?reader={READER}&by={quote(OWNER)}")
    st = json.loads(body)
    assert st["owner"] == OWNER
    assert st["source_decisions"] == {TRIAL_YT: "later"}


def test_open_kind_idempotent(server):
    port, env, ed = server
    sid = ed["portada"][0]
    for _ in range(2):  # e.g. sendBeacon fired twice for one page load
        status, body = post_json(port, "/api/react",
                                 {"reader": READER, "date": ed["date"], "story_id": sid,
                                  "kind": "open", "by": OWNER})
        assert status == 200
    n = env.conn.execute(
        "SELECT COUNT(*) FROM reactions WHERE reader_id = ? AND story_id = ? "
        "AND kind = 'open' AND by_name = ?", (READER, sid, OWNER)).fetchone()[0]
    assert n == 1


# ------------------------------------------------------------ taste: 'open'

def test_taste_open_weight(env):
    for it in [
        ("a1", "A one", "alpha beta gamma", "noted", "public", T0),
        ("a2", "A two", "alpha beta gamma delta", "noted", "public", T0 + DAY),
        ("b1", "B one", "omega psi chi", "noted", "public", T0 + DAY),
        ("c1", "C one", "alpha beta gamma epsilon", "noted", "public", T0 + DAY),
    ]:
        add_item(env, *it)
    embed_items(env.conn, READER, FakeEmbedder())
    run_cluster.cluster(env.conn, READER, env.cfg, rebuild=True)
    by_title = {r["title"]: r["id"] for r in
                env.conn.execute("SELECT id, title FROM stories WHERE reader_id = ?", (READER,))}
    sid_a, sid_c = by_title["A one"], by_title["C one"]
    env.conn.execute(
        "INSERT INTO reactions (reader_id, story_id, kind, at, by_name) VALUES (?, ?, 'open', ?, ?)",
        (READER, sid_a, int(time.time()), OWNER))
    env.conn.commit()
    items_by_story = {sid_c: [{"source_id": "test:direct", "source_kind": "other"}]}
    adj = adjustments(env.conn, READER, env.cfg, items_by_story)
    assert sid_a not in adj.delta                      # open never boosts the opened story
    assert adj.delta.get(sid_c, 0) > 0                 # …but something similar
    assert "Abriste algo parecido" in adj.reasons.get(sid_c, [])


# ------------------------------------------------------------ CLI export

def test_sources_pending_cli(env, capsys, monkeypatch):
    _seed(env)
    now = int(time.time())
    env.conn.executemany(
        "INSERT INTO source_decisions (reader_id, by_name, source_key, decision, at) "
        "VALUES (?, 'Test Reader', ?, ?, ?)",
        [(READER, TRIAL_FEED, "follow", now), (READER, f"host:{CITED}", "drop", now - 10)],
    )
    env.conn.commit()
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    rc = cli_main(["sources", "pending", "--reader", READER])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [d["source_key"] for d in out] == [f"host:{CITED}", TRIAL_FEED]  # by at
    assert all(d["reader"] == READER and d["by"] == OWNER for d in out)

    rc = cli_main(["sources", "pending", "--reader", READER, "--mark-exported"])
    capsys.readouterr()
    assert rc == 0
    rc = cli_main(["sources", "pending", "--reader", READER])
    assert json.loads(capsys.readouterr().out) == []
    rc = cli_main(["sources", "pending", "--reader", "nobody"])
    assert rc == 2


def test_media_hosts_and_name_overrides():
    from gaceta.discovery import _is_media_host, _clean_name
    assert _is_media_host("npub1abc.blossom.band")
    assert _is_media_host("image.nostr.build")
    assert not _is_media_host("zorblax-news.example")
    assert _clean_name("trial-glimmer-house") == "Glimmer House"


def test_long_host_label_is_shortened_for_display():
    from gaceta.render.html import _short_label
    host = "npub1x8j86s83ti8yzokopd3ag5derekpilgrims0000000000000.nsite.lol"
    short = _short_label(host)
    assert short.endswith(".nsite.lol") and "…" in short
    assert max(len(l) for l in short.split(".")) <= 32
    assert _short_label("simonwillison.net") == "simonwillison.net"
    assert _short_label("Latent Space") == "Latent Space"


# ------------------------------------------------------------ signal quality (v1)

def test_link_kind_classifies_citations():
    from gaceta.discovery import _link_kind
    assert _link_kind("https://news.example/2026/11/03/") == "article"
    assert _link_kind("https://news.example/opinion/the-river-came-back") == "article"
    assert _link_kind("https://news.example/notes/q3-update.html") == "page"  # 2 words only
    assert _link_kind("https://blob.example/" + HEX) == "media"
    assert _link_kind("https://cdn.example/a/pic-of-the-day.jpg") == "media"
    assert _link_kind("https://chain.example/block/" + HEX) == "media"
    assert _link_kind("https://git.example/npub1abcdef/repo") == "media"
    assert _link_kind("https://app.example/") == "root"
    assert _link_kind("https://app.example/?ref=me") == "root"
    assert _link_kind("https://forum.example/items/1616039") == "page"
    assert _link_kind("https://coinjoin") == "junk"
    assert _link_kind("http://[::1") == "junk"
    assert _link_kind("https://chain.example/address/bc1qphukp9rjfc4rcej7erpm9ae20xmtspw8pcddrr") == "page"
    assert _link_kind("https://music.example/track/3504d80b-b4bf-4196-923b-7ed8b60caec9") == "media"
    assert _link_kind("https://diario.example/opinion/el-r%C3%ADo-volvi%C3%B3-hoy") == "article"


def test_site_folds_plumbing_subdomains():
    from gaceta.discovery import _site
    assert _site("assets.science.example.gov") == "science.example.gov"
    assert _site("media1.giphy.example") == "giphy.example"
    assert _site("cdn.example") == "cdn.example"          # never below two labels
    assert _site("blog.vendor.example") == "blog.vendor.example"


def _cite(env, iid, author, body, sid):
    add_item(env, iid, f"Note {iid}", body, "noted", "public", T0 + DAY)
    env.conn.execute("UPDATE items SET author = ? WHERE id = ?", (author, iid))
    _story(env, sid, [iid], f"Story {sid}")


def test_suggestions_ignore_media_apps_and_lone_voices(env):
    _seed(env)
    n = 0
    for who in VOICES:  # three independent voices, all linking non-sources
        for body in (f"https://blobs.example/{HEX} https://pics.example/a/sunset-over-hills.jpg",
                     f"https://chain.example/block/{HEX2} https://wallet-app.example/",
                     "https://tools.example/apps/foundation.mostro.app"):
            n += 1
            _cite(env, f"m{n}", who, body, f"stM{n}")
    for i in range(4):  # a real site, but only one person keeps posting it
        _cite(env, f"solo{i}", "Lone Poster", f"https://satire.example/news/a-funny-story-{i}", f"stS{i}")
    env.conn.commit()
    keys = {s["key"] for s in suggestions(env.conn, READER, SINCE, require_probe=False)}
    assert f"host:{CITED}" in keys
    for absent in ("host:blobs.example", "host:pics.example", "host:chain.example",
                   "host:wallet-app.example", "host:tools.example", "host:satire.example"):
        assert absent not in keys, absent


def test_suggestions_fold_subdomain_into_existing_site(env):
    _seed(env)
    env.conn.execute("INSERT INTO sources (id, kind, name, url) VALUES "
                     "('feed:space', 'rss', 'Space desk', 'https://space.example/feed')")
    for i, who in enumerate(VOICES):
        _cite(env, f"sp{i}", who, f"https://assets.space.example/2026/11/rocket-launch-{i}", f"stP{i}")
    env.conn.commit()
    keys = {s["key"] for s in suggestions(env.conn, READER, SINCE, require_probe=False)}
    assert "host:assets.space.example" not in keys and "host:space.example" not in keys


def test_suggestions_drop_sites_the_reader_pushed_away(env):
    _seed(env)
    now = int(time.time())
    env.conn.executemany(
        "INSERT INTO reactions (reader_id, story_id, kind, at, by_name) VALUES (?, ?, 'less', ?, ?)",
        [(READER, f"stC{i}", now, OWNER) for i in range(3)])
    env.conn.commit()
    keys = {s["key"] for s in suggestions(env.conn, READER, SINCE)}
    assert f"host:{CITED}" not in keys


def test_trials_are_per_reader(env):
    _seed(env)
    env.conn.execute("INSERT INTO readers (id, name, langs, created_at) VALUES ('other', 'O', 'es', 0)")
    env.conn.commit()
    assert trial_sources(env.conn, "other", SINCE) == []  # shared collector, none in her paper


# ------------------------------------------------------------ 6b v2: probe gate + description

def test_unprobed_or_feedless_sites_are_not_shown(env):
    _seed(env)
    env.conn.execute("DELETE FROM source_probes WHERE site = ?", (CITED,))
    _probed(env, "other-gazette.substack.com", ok=0)
    env.conn.commit()
    keys = {s["key"] for s in suggestions(env.conn, READER, SINCE)}
    assert f"host:{CITED}" not in keys and "host:other-gazette.substack.com" not in keys
    keys = {s["key"] for s in suggestions(env.conn, READER, SINCE, require_probe=False)}
    assert f"host:{CITED}" in keys  # the signal is there; only the gate holds it


def test_probed_site_carries_name_about_headlines_and_who(env):
    _seed(env)
    out = {s["key"]: s for s in suggestions(env.conn, READER, SINCE)}
    e = out[f"host:{CITED}"]
    assert e["name"] == CITED.split(".")[0].title()       # tagline trimmed
    assert e["about"] == f"About {CITED}." and e["feed_url"] == f"https://{CITED}/feed"
    assert e["headlines"] == [f"Latest at {CITED}", "Second headline"]
    assert all(v in e["reason"] for v in VOICES[:2])
    pub = {s["key"]: s for s in suggestions(env.conn, READER, SINCE, max_privacy="public")}
    assert " · " not in pub[f"host:{CITED}"]["reason"]   # who-cites is private detail


def test_render_shows_about_and_lately(env):
    ed = _build_with_discovery(env)
    doc = render_html.render(ed, "private")
    assert f"About {CITED}." in doc and "Últimas:" in doc and f"Latest at {CITED}" in doc


def test_probe_site_finds_feed_and_description():
    from gaceta.sources import probe
    home = (b'<html lang="es-CO"><head><title>Rio Notes - news</title>'
            b'<meta property="og:description" content="Notes on rivers &amp; water.">'
            b'<link rel="alternate" type="application/rss+xml" href="/my.xml"></head></html>')
    now = time.time()
    pub = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(now - 86400))
    feed = (f'<rss><channel><item><title>Flood map update</title><link>https://rio.example/a</link>'
            f'<pubDate>{pub}</pubDate></item></channel></rss>').encode()
    pages = {"https://rio.example/": home, "https://rio.example/my.xml": feed}

    def fetch(url, max_bytes):
        if url not in pages:
            raise OSError("404")
        return pages[url]

    row = probe.probe_site("rio.example", fetch=fetch, now=now)
    assert row["ok"] == 1 and row["feed_url"] == "https://rio.example/my.xml"
    assert row["description"] == "Notes on rivers & water." and row["lang"] == "es"
    assert json.loads(row["headlines"]) == ["Flood map update"]
    del pages["https://rio.example/my.xml"]
    row = probe.probe_site("rio.example", fetch=fetch, now=now)
    assert row["ok"] == 0 and row["note"] == "no feed found" and row["title"] == "Rio Notes - news"
    old = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(now - 400 * 86400))
    pages["https://rio.example/feed"] = feed.replace(pub.encode(), old.encode())
    row = probe.probe_site("rio.example", fetch=fetch, now=now)
    assert row["ok"] == 0 and row["note"].startswith("feed stale")


def test_probe_due_respects_ttls(env):
    from gaceta.sources import probe
    _seed(env)
    now = time.time()
    assert probe.due(env.conn, [CITED, "new.example"], now=now) == ["new.example"]
    assert probe.due(env.conn, [CITED], now=now + probe.OK_TTL + 1) == [CITED]
