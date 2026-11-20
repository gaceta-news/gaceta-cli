"""Edition builder: no repeats, every referenced story exists, privacy filter +
canaries (JSON and HTML), snapshot immutability, section matching and the
30-story section cap, per-story rev, next_update schedule."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from gaceta.edition import build_edition, snapshot_path
from gaceta.editor import cluster as run_cluster
from gaceta.editor.cluster import embed_items
from gaceta.providers.embed import FakeEmbedder
from gaceta.render import html as render_html

from test_cluster import T0, DAY, add_item

CANARY = "zorblax-secreto"


def _edition_date() -> str:
    return datetime.fromtimestamp(T0 + 6 * DAY, tz=timezone.utc).date().isoformat()


def _build(env, items):
    for it in items:
        add_item(env, *it)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    run_cluster.cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)


def _rich_fixture(env):
    """A: 3 public items + summary (keyword match); B1..B6: told, recent → portada;
    D: told but old → misses portada, lands in lo-que-te-conte; C: private (canary).
    One http source URL, one non-http URL."""
    _build(env, [
        ("a1", "A one", "alpha beta gamma", "noted", "public", T0),
        ("a2", "A two", "alpha beta gamma", "noted", "public", T0 + DAY),
        ("a3", "A three", "alpha beta gamma", "noted", "public", T0 + 2 * DAY),
        *[("b1"[:1] + str(n), f"B{n} told", f"delta epsilon {n}", "told", "public", T0 + 5 * DAY)
          for n in range(6)],
        ("c1", f"C {CANARY}", "zeta eta", "noted", "private", T0 + DAY),
        ("d1", "D told", "theta iota", "told", "public", T0),
    ])
    env.conn.execute(
        "UPDATE stories SET summary = 'resumen con gamma' WHERE title = 'A one'"
    )
    env.conn.execute("UPDATE items SET url = 'https://example.com/a1' WHERE id = 'a1'")
    env.conn.execute("UPDATE items SET url = 'javascript:alert(1)' WHERE id = 'd1'")
    env.conn.commit()
    env.cfg["sections"] = [
        {"id": "gamma-sec", "title": "Gamma", "kinds": [], "source_prefixes": [],
         "keywords": ["gamma"]},
    ]


def _all_ids(edition):
    return (list(edition["portada"])
            + [sid for s in edition["sections"] for sid in s["stories"]])


def test_no_repeats_and_every_referenced_story_exists(env):
    _rich_fixture(env)
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                       max_privacy="private", data_dir=env.data_dir)
    ids = _all_ids(ed)
    assert len(ids) == len(set(ids)), "a story appears more than once"
    assert set(ids) == set(ed["stories"].keys())
    assert 0 < len(ed["portada"]) <= 6
    assert set(ed["portada"]) <= set(ids)


def test_told_story_gets_its_section_and_sources_are_http_only(env):
    _rich_fixture(env)
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                       max_privacy="private", data_dir=env.data_dir)
    sections = {s["id"]: s for s in ed["sections"]}
    by_title = {v["title"]: sid for sid, v in ed["stories"].items()}
    assert by_title["D told"] not in ed["portada"]  # old told story misses the top 6
    told = sections.get("lo-que-te-conte", {}).get("stories", [])
    assert by_title["D told"] in told
    gamma = sections.get("gamma-sec", {}).get("stories", [])
    assert by_title["A one"] in gamma  # keyword 'gamma' in summary
    urls = [s["url"] for s in ed["stories"][by_title["A one"]]["sources"]]
    assert urls and all(u.startswith("http") for u in urls)
    assert ed["stories"][by_title["D told"]]["sources"] == []  # javascript: URL excluded
    otros = sections.get("otros", {}).get("stories", [])
    assert by_title[f"C {CANARY}"] in otros  # unmatched private story → otros


def test_private_stories_absent_and_canaries_absent_at_public(env):
    _rich_fixture(env)
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                       max_privacy="public", data_dir=env.data_dir)
    blob = json.dumps(ed, ensure_ascii=False)
    assert CANARY not in blob
    for story in ed["stories"].values():
        assert story["privacy"] in ("public",)
    doc = render_html.render(ed, "public")
    assert CANARY not in doc


def test_written_only_holds_back_raw_cards(env):
    _rich_fixture(env)
    env.cfg["edition"] = {"written_only": True}
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                       max_privacy="private", data_dir=env.data_dir)
    assert [s["summary"] for s in ed["stories"].values()] == ["resumen con gamma"]


def test_coverage_counts_distinct_sites_not_duplicate_articles(env):
    _build(env, [(f"a{i}", f"A {i}", "alpha beta gamma", "noted", "public", T0 + DAY)
                 for i in range(3)] + [
        ("b0", "B 0", "zeta eta theta", "noted", "public", T0 + DAY),
        ("b1", "B 1", "zeta eta theta", "noted", "public", T0 + DAY),
    ])
    for i, host in enumerate(("paper.example", "www.paper.example", "m.paper.example",
                              "a.example", "b.example")):
        iid = ("a0", "a1", "a2", "b0", "b1")[i]
        env.conn.execute("UPDATE items SET url = ? WHERE id = ?", (f"https://{host}/{iid}", iid))
    day = _edition_date()
    legacy = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=day, data_dir=env.data_dir)
    assert legacy["portada"][0] == story_of_item(env.conn, "a0")
    env.cfg["edition"] = {"coverage_v1": True}
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=day, data_dir=env.data_dir, force=True)
    a, b = (story_of_item(env.conn, k) for k in ("a0", "b0"))
    assert ed["portada"] == [b, a]
    assert any("En 2 sitios distintos" in s for s in ed["stories"][b]["score_reasons"])
    assert not any("fuentes" in s or "sitios" in s for s in ed["stories"][a]["score_reasons"])
    assert len(ed["stories"][a]["sources"]) == 3  # the archive keeps all real links


def test_new_coverage_uses_local_day_and_links_only_new_dated_items(env, monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("TZ", "America/Lima")
        time.tzset()
        try:
            _check_new_coverage(env)
        finally:
            m.undo()
            time.tzset()


def _check_new_coverage(env):
    today = datetime.fromtimestamp(T0 + 5 * DAY).replace(hour=0, minute=0, second=0)
    older = int((today - timedelta(hours=2)).timestamp())
    update = int((today + timedelta(hours=20)).timestamp())
    now = int((today + timedelta(hours=21)).timestamp())
    items = [
        ("a0", "Original", "alpha beta gamma", "noted", "public", older),
        ("a1", "Novedad <real>", "alpha beta gamma", "noted", "public", update),
        ("a2", "Mirror", "alpha beta gamma", "noted", "public", update + 60),
        ("a3", "Future", "alpha beta gamma", "noted", "public", now + 60),
        ("b0", "Brand new", "zeta eta theta", "noted", "public", update),
    ]
    for item in items:
        add_item(env, *item)
    env.conn.executemany(
        "INSERT INTO stories (id, reader_id, title, first_seen, last_seen, privacy) "
        "VALUES (?, ?, ?, ?, ?, 'public')",
        [("continuing", env.cfg["id"], "Original", older, now + 60),
         ("new", env.cfg["id"], "Brand new", update, update)],
    )
    env.conn.executemany("INSERT INTO story_items (story_id, item_id) VALUES (?, ?)",
                         [("continuing", item[0]) for item in items[:4]] + [("new", "b0")])
    for iid, url in [("a0", "https://www.paper.example/original/"),
                     ("a1", "https://paper.example/update"),
                     ("a2", "https://m.paper.example/original?utm_source=mirror#duplicate"),
                     ("a3", "https://paper.example/future"), ("b0", "https://other.example/brand-new")]:
        env.conn.execute("UPDATE items SET url = ? WHERE id = ?", (url, iid))
    env.cfg["edition"] = {"coverage_v1": True}
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=today.date().isoformat(),
                       data_dir=env.data_dir, now=now)
    a, b = (ed["stories"][story_of_item(env.conn, k)] for k in ("a0", "b0"))
    assert a["new_coverage"] == "https://paper.example/update"
    assert "new_coverage" not in b
    a["sources"].reverse()
    html = render_html.render(ed, "private")
    assert re.search(r'class="new-coverage"[^\n]+https://paper.example/update[^\n]+Novedad &lt;real&gt;', html)
    a["new_coverage"] = "https://example.com/not-a-story-source"
    assert '<p class="new-coverage">' not in render_html.render(ed, "private")


def test_snapshot_not_overwritten_without_force(env):
    _rich_fixture(env)
    kwargs = dict(date_str=_edition_date(), max_privacy="private", data_dir=env.data_dir)
    build_edition(env.conn, env.cfg["id"], env.cfg, **kwargs)
    path = snapshot_path(env.data_dir, env.cfg["id"], _edition_date(), "private")
    first = path.read_text()
    with pytest.raises(FileExistsError):
        build_edition(env.conn, env.cfg["id"], env.cfg, **kwargs)
    assert path.read_text() == first  # refused → untouched
    build_edition(env.conn, env.cfg["id"], env.cfg, force=True, **kwargs)
    assert path.exists()
    assert env.conn.execute("SELECT COUNT(*) FROM editions").fetchone()[0] == 1


def test_section_cap_is_30_and_rest_falls_to_otros(env):
    items = [(f"s{i}", f"S {i}", f"cuerpo unico {i}", "noted", "public", T0 + i * 3600)
             for i in range(40)]
    _build(env, items)
    env.cfg["sections"] = [
        {"id": "all", "title": "All", "kinds": [], "source_prefixes": ["test:"],
         "keywords": []},
    ]
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                       max_privacy="private", data_dir=env.data_dir)
    assert len(ed["portada"]) == 6
    sections = {s["id"]: s["stories"] for s in ed["sections"]}
    assert len(sections["all"]) == 30
    assert len(sections.get("otros", [])) == 4
    ids = _all_ids(ed)
    assert len(ids) == len(set(ids)) == 40


def test_portada_rotates_and_rests_leaders(env):
    """Six clear leaders repeat on the portada; after 3 consecutive editions
    they must rest (out of the portada, still placed in sections), and repeats
    carry an explicit reason. Deterministic fixture: stories written directly."""
    conn = env.conn
    conn.execute("INSERT OR IGNORE INTO readers (id, name, langs, created_at) "
                 "VALUES (?, 'Test Reader', 'en', 0)", (env.cfg["id"],))
    for n in range(6):  # leaders: 40 items + fresh → score ≈ 4.6, gap > max penalty
        sid = f"lead{n}"
        conn.execute(
            "INSERT INTO stories (id, reader_id, title, status, privacy, first_seen, last_seen) "
            "VALUES (?, ?, ?, 'settled', 'public', ?, ?)",
            (sid, env.cfg["id"], f"Lead {n}", T0, T0 + 5 * DAY))
        for k in range(40):
            iid = f"l{n}i{k}"
            conn.execute(
                "INSERT INTO items (id, title, url, published, ingested_at, origin) "
                "VALUES (?, ?, ?, ?, ?, 'sqlite')",
                (iid, f"L {n} {k}", f"https://paper{n}.example/{k}", T0 + k, T0 + k))
            conn.execute("INSERT INTO story_items (story_id, item_id) VALUES (?, ?)", (sid, iid))
        conn.execute("INSERT INTO told (reader_id, story_id, at) VALUES (?, ?, ?)",
                     (env.cfg["id"], sid, T0))
    for n in range(2):  # challengers: single item, no told → score ≈ 1.6
        sid = f"chal{n}"
        conn.execute(
            "INSERT INTO stories (id, reader_id, title, status, privacy, first_seen, last_seen) "
            "VALUES (?, ?, ?, 'settled', 'public', ?, ?)",
            (sid, env.cfg["id"], f"Chal {n}", T0 + DAY, T0 + 4 * DAY))
        conn.execute(
            "INSERT INTO items (id, title, url, published, ingested_at, origin) "
            "VALUES (?, ?, ?, ?, ?, 'sqlite')",
            (f"c{n}", f"C {n}", f"https://other{n}.example/", T0 + 4 * DAY, T0 + 4 * DAY))
        conn.execute("INSERT INTO story_items (story_id, item_id) VALUES (?, ?)", (sid, f"c{n}"))
    conn.commit()

    leaders = {f"lead{n}" for n in range(6)}
    days = [datetime.fromtimestamp(T0 + (6 + n) * DAY, tz=timezone.utc).date().isoformat()
            for n in range(4)]
    for n, day in enumerate(days):  # 3 days of stable leadership, then rotation
        ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=day,
                           max_privacy="private", data_dir=env.data_dir,
                           now=T0 + (6 + n) * DAY + 3600)
        if n < 3:
            assert set(ed["portada"]) == leaders  # repeat penalty < score gap
            if n > 0:  # repeats are explained, not silent
                assert all(any("portada" in r for r in ed["stories"][sid]["score_reasons"])
                           for sid in leaders)
        else:
            assert set(ed["portada"]) == {"chal0", "chal1"}  # leaders all rest
            for sid in leaders:
                assert any("descansa" in r for r in ed["stories"][sid]["score_reasons"])
                assert sid in _all_ids(ed)  # resting ≠ dropped: still in sections


def story_of_item(conn, item_id):
    row = conn.execute(
        "SELECT si.story_id FROM story_items si JOIN items i ON i.id = si.item_id "
        "WHERE i.id = ?", (item_id,)).fetchone()
    assert row is not None, item_id
    return row["story_id"]


def test_every_story_has_rev_and_it_tracks_members_and_summary(env):
    _rich_fixture(env)
    kw = dict(date_str=_edition_date(), max_privacy="private",
              data_dir=env.data_dir, force=True)
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, **kw)
    revs = {sid: e["rev"] for sid, e in ed["stories"].items()}
    assert revs and all(re.fullmatch(r"[a-f0-9]{12}", r) for r in revs.values())
    story = story_of_item(env.conn, "a1")
    assert story in revs
    # adding a member item changes the rev…
    add_item(env, "a9", "A nine", "alpha beta gamma", "noted", "public", T0 + 3 * DAY)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    run_cluster.cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)
    assert story_of_item(env.conn, "a9") == story_of_item(env.conn, "a1")
    ed2 = build_edition(env.conn, env.cfg["id"], env.cfg, **kw)
    story2 = story_of_item(env.conn, "a1")
    assert ed2["stories"][story2]["rev"] != revs[story]
    # …and so does a summary rewrite, on the same members
    old = ed2["stories"][story2]["rev"]
    env.conn.execute(
        "UPDATE stories SET summary = 'resumen con gamma (editado)' WHERE id = ?", (story2,))
    env.conn.commit()
    ed3 = build_edition(env.conn, env.cfg["id"], env.cfg, **kw)
    assert ed3["stories"][story_of_item(env.conn, "a1")]["rev"] != old


def test_next_update_from_schedule(env):
    _rich_fixture(env)
    kw = dict(date_str=_edition_date(), max_privacy="private",
              data_dir=env.data_dir, force=True)
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, **kw)
    assert "next_update" not in ed  # no [schedule] in the fixture config
    env.cfg["schedule"] = {"every_hours": 2, "start_hour": 6, "end_hour": 22, "minute": 30}
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, **kw)
    built = datetime.fromisoformat(ed["built_at"])
    nxt = datetime.fromisoformat(ed["next_update"])
    assert nxt > built
    assert nxt.minute == 30 and nxt.hour % 2 == 0 and 6 <= nxt.hour <= 22
    # built at the last slot of the day → tomorrow's first slot
    env.cfg["schedule"] = {"every_hours": 6, "start_hour": 10, "end_hour": 22, "minute": 0}
    after_last = datetime.now().astimezone().replace(hour=22, minute=15,
                                                     second=0, microsecond=0)
    ed = build_edition(env.conn, env.cfg["id"], env.cfg,
                       date_str=_edition_date(), max_privacy="private",
                       data_dir=env.data_dir, force=True, now=int(after_last.timestamp()))
    nxt = datetime.fromisoformat(ed["next_update"])
    assert (nxt.date() - after_last.date()).days == 1
    assert (nxt.hour, nxt.minute) == (10, 0)
