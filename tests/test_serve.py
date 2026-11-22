"""Serve: localhost-only binding, path-traversal-proof file serving, reaction API
validation + toggle, state/readers API, HTML injection. Server runs on an
ephemeral port in a thread; no network leaves 127.0.0.1."""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from gaceta.edition import build_edition
from gaceta.editor import cluster as run_cluster
from gaceta.editor.cluster import embed_items
from gaceta.providers.embed import FakeEmbedder
from gaceta.render import html as render_html
from gaceta.serve import inject_react, make_server, validate_host

from test_cluster import T0, DAY, add_item
from test_edition_build import _edition_date


def _site(env):
    for it in [
        ("a1", "A one", "alpha beta gamma", "noted", "public", T0),
        ("a2", "A two", "alpha beta gamma", "noted", "public", T0 + DAY),
        ("b1", "B one", "omega psi", "told", "public", T0 + DAY),
    ]:
        add_item(env, *it)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())
    run_cluster.cluster(env.conn, env.cfg["id"], env.cfg, rebuild=True)
    ed = build_edition(env.conn, env.cfg["id"], env.cfg, date_str=_edition_date(),
                       max_privacy="private", data_dir=env.data_dir)
    doc = render_html.render(ed, "private")
    site = env.data_dir / "site" / env.cfg["id"]
    site.mkdir(parents=True, exist_ok=True)
    (site / f"{ed['date']}.html").write_text(doc, encoding="utf-8")
    (site / "index.html").write_text(doc, encoding="utf-8")
    media = env.data_dir / "site" / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "0123456789abcdef.png").write_bytes(b"\x89PNG-fake")
    return ed


@pytest.fixture
def server(env):
    ed = _site(env)
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
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    return r.status, hdrs, data


def post_react(port, payload, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    return req(port, "POST", "/api/react", body=json.dumps(payload), headers=h)


# ------------------------------------------------------------------ binding

def test_non_localhost_bind_refused(tmp_path):
    for host in ("0.0.0.0", "192.168.1.5", "example.com", ""):
        with pytest.raises(ValueError):
            validate_host(host)
        with pytest.raises(ValueError):
            make_server(host, 0, tmp_path)
    make_server("127.0.0.1", 0, tmp_path).server_close()
    import socket
    if socket.has_ipv6:
        try:
            make_server("::1", 0, tmp_path).server_close()
        except OSError:
            pass  # no IPv6 stack in this environment


# ------------------------------------------------------------------ traversal

def test_traversal_attempts_404(server):
    port, env, ed = server
    for path in (
        "/r/../gaceta.db",
        "/r/%2e%2e/gaceta.db",
        "/r/testreader/../../../../gaceta.db",
        "/media/../../gaceta.db",
        "/media/%2e%2e/%2e%2e/gaceta.db",
        "/static/../serve.py",
        "/r/testreader/..%2f..%2fgaceta.db",
    ):
        status, _, _ = req(port, "GET", path)
        assert status == 404, path


def test_media_filename_regex_enforced(server):
    port, env, ed = server
    ok = req(port, "GET", "/media/0123456789abcdef.png")
    assert ok[0] == 200 and ok[2][:4] == b"\x89PNG"
    for name in ("0123456789abcdeG.png", "0123456789abcdef.txt", "0123456789abcdef.PNG",
                 "0123456789abcdef.jpg.exe", "0123456789abcd.png"):
        status, _, _ = req(port, "GET", f"/media/{name}")
        assert status == 404, name


# ------------------------------------------------------------------ serving

def test_index_redirects_with_single_reader(server):
    port, env, ed = server
    # fixture has one reader: / redirects straight to their edition
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/")
    r = conn.getresponse()
    r.read()
    assert r.status == 302
    assert r.getheader("Location") == f"/r/{env.cfg['id']}/"
    conn.close()


def test_edition_html_injection_and_headers(server):
    port, env, ed = server
    status, hdrs, body = req(port, "GET", f"/r/{env.cfg['id']}/")
    assert status == 200
    html = body.decode()
    tag = '<script src="/static/react.js" defer></script>'
    assert html.count(tag) == 1
    assert tag + "</body>" in html
    assert "default-src 'self'" in hdrs["content-security-policy"]
    assert hdrs["x-content-type-options"] == "nosniff"
    assert hdrs["referrer-policy"] == "no-referrer"
    assert "no-cache" in hdrs["cache-control"]
    # date URL works too, and injection happens once even for a repeated serve
    status, _, body = req(port, "GET", f"/r/{env.cfg['id']}/{ed['date']}.html")
    assert status == 200 and body.decode().count(tag) == 1


def test_inject_react_rewrites_media_src(server):
    port, env, ed = server
    site = env.data_dir / "site" / env.cfg["id"]
    (site / "2001-01-01.html").write_text(
        '<html><body><img src="../media/0123456789abcdef.png">'
        '<a href="../media/nope">x</a></body></html>', encoding="utf-8")
    status, _, body = req(port, "GET", f"/r/{env.cfg['id']}/2001-01-01.html")
    html = body.decode()
    assert 'src="/media/0123456789abcdef.png"' in html
    assert 'src="../media/' not in html
    assert 'href="../media/nope"' in html  # only src= attributes are rewritten
    assert html.count('src="/static/react.js"') == 1


def test_inject_react_is_idempotent():
    once = inject_react("<html><body>x</body></html>")
    assert inject_react(once) == once


def test_static_react_js_served(server):
    port, env, ed = server
    status, hdrs, body = req(port, "GET", "/static/react.js")
    assert status == 200
    assert "javascript" in hdrs["content-type"]
    assert b"gaceta.by" in body


def test_media_cache_immutable(server):
    port, env, ed = server
    _, hdrs, _ = req(port, "GET", "/media/0123456789abcdef.png")
    assert "immutable" in hdrs["cache-control"]
    assert hdrs["content-type"] == "image/png"


# ------------------------------------------------------------------ /api/react

def test_react_ok_and_toggle(server):
    port, env, ed = server
    story = ed["portada"][0]
    payload = {"reader": env.cfg["id"], "date": ed["date"], "story_id": story,
               "kind": "up", "by": "Test Reader"}
    status, _, body = post_react(port, payload)
    assert status == 200
    assert json.loads(body)["state"]["kinds"] == ["up"]
    row = env.conn.execute(
        "SELECT by_name FROM reactions WHERE reader_id = ? AND story_id = ?",
        (env.cfg["id"], story)).fetchone()
    assert row["by_name"] == "Test Reader"
    # same (story, kind, by) again today -> removed
    status, _, body = post_react(port, payload)
    assert status == 200
    assert json.loads(body)["state"]["kinds"] == []
    assert env.conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0] == 0


def test_react_validation(server):
    port, env, ed = server
    story = ed["portada"][0]
    base = {"reader": env.cfg["id"], "story_id": story, "kind": "up", "by": "Test Reader"}
    bad = [
        ({**base, "kind": "meh"}, 400),                      # unknown kind
        ({**base, "story_id": "nope"}, 404),                 # unknown story
        ({**base, "reader": "someoneelse"}, 404),            # unknown reader
        ({**base, "by": "x" * 41}, 400),                     # by too long
        ({**base, "text": "t" * 501}, 400),                  # text too long
        ({**base, "reader": "../evil"}, 400),                # bad reader slug
    ]
    for payload, want in bad:
        status, _, _ = post_react(port, payload)
        assert status == want, payload
    # oversized body -> 413
    big = {**base, "text": "t" * 4100}
    status, _, _ = post_react(port, big)
    assert status == 413
    # wrong content type -> 415
    status, _, _ = req(port, "POST", "/api/react",
                       body=json.dumps(base), headers={"Content-Type": "text/plain"})
    assert status == 415
    # cross-origin Origin -> 403
    status, _, _ = post_react(port, base, headers={"Origin": "https://evil.example"})
    assert status == 403
    # same-origin Origin is fine
    status, _, _ = post_react(port, base,
                              headers={"Origin": f"http://127.0.0.1:{port}"})
    assert status == 200
    # nothing but the last (valid) one was stored
    assert env.conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0] == 1


# ------------------------------------------------------------------ /api/*

def test_api_state(server):
    port, env, ed = server
    story = ed["portada"][0]
    post_react(port, {"reader": env.cfg["id"], "story_id": story,
                      "kind": "down", "by": "Bob"})
    status, _, body = req(port, "GET",
                          f"/api/state?reader={env.cfg['id']}&by=Bob")
    assert status == 200
    st = json.loads(body)
    assert st["date"] == ed["date"]
    assert st["reactions"].get(story) == ["down"]
    assert st["reasons"]  # score_reasons came through from the edition JSON
    # a story not in the edition is not reported
    status, _, _ = req(port, "GET", f"/api/state?reader=none&by=Bob")
    assert status == 404


def test_api_state_date_from_latest_edition(server):
    port, env, ed = server
    status, _, body = req(port, "GET", f"/api/state?reader={env.cfg['id']}&by=X")
    assert status == 200
    assert json.loads(body)["date"] == ed["date"]


def test_api_state_carries_edition_revs_for_open_pages(server):
    """An open page compares revs with what it loaded to announce a new edition."""
    port, env, ed = server
    _, _, body = req(port, "GET", f"/api/state?reader={env.cfg['id']}&by=X")
    st = json.loads(body)
    assert st["revs"] == {sid: s["rev"] for sid, s in ed["stories"].items()}
    assert st["built_at"] == ed.get("built_at")


def test_api_readers_lists_household(server):
    port, env, ed = server
    toml = env.data_dir / "readers" / env.cfg["id"] / "reader.toml"
    toml.write_text(toml.read_text() + '\n[serve]\nhousehold = ["Bob"]\n', encoding="utf-8")
    status, _, body = req(port, "GET", f"/api/readers?reader={env.cfg['id']}")
    assert status == 200
    data = json.loads(body)
    assert data["owner"] == "Test Reader"
    assert data["household"] == ["Bob"]


# ------------------------------------------------------------------ locking

def test_store_uses_wal_and_busy_timeout(server):
    _, env, _ = server
    assert env.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert env.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10_000


def test_api_answers_while_edition_build_holds_write_lock(server):
    """Regression (22-nov): an edition build writing for minutes plus the
    server's per-request schema writes deadlocked the rollback-journal file;
    every /api/* returned 500 'OperationalError' and the reaction bar vanished.
    Now: WAL (readers never wait for the writer) + init once per process."""
    import sqlite3
    import time

    port, env, _ = server
    reader = env.cfg["id"]
    assert req(port, "GET", f"/api/state?reader={reader}&by=Bob")[0] == 200
    writer = sqlite3.connect(env.data_dir / "gaceta.db", timeout=1)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO meta (k, v) VALUES ('lock-test', '1')")
        t0 = time.monotonic()
        for path in (f"/api/state?reader={reader}&by=Bob",
                     f"/api/readers?reader={reader}"):
            status, _, body = req(port, "GET", path)
            assert status == 200, body
        assert time.monotonic() - t0 < 3  # no busy-wait: reads don't touch the write lock
    finally:
        writer.rollback()
        writer.close()
