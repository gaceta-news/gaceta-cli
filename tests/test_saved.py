"""Saved stories ('Guardar', roadmap 6c, schema v9): plugin-adapter ingest via
the CLI, /api/save + /api/unsave (owner only), taste weights/labels, and the
/r/<reader>/guardadas archive page. All data is invented; the upstream db
fixture is built in tmp."""
from __future__ import annotations

import json
import re
import sqlite3
import time

from gaceta.cli import main as cli_main
from gaceta.editor import taste
from gaceta.editor.taste import adjustments
from gaceta.ingest import ingest
from gaceta.sources.sqlite import SqliteAdapter

from test_discovery import OWNER, READER, _seed, post_json, req, server  # noqa: F401

# ------------------------------------------------------------ ingest (a plugin
# adapter is a source, never saves)

def test_ingest_plugin_idempotent_source_only(env):
    ad = SqliteAdapter({"path": str(env.src_root)})
    result = ingest(env.conn, ad, env.cfg)
    assert result["new"] == env.info["n_items"]
    assert env.conn.execute("SELECT origin FROM items LIMIT 1").fetchone()[0] \
        == "sqlite"                                            # origin = adapter name
    result2 = ingest(env.conn, ad, env.cfg)
    assert (result2["new"], result2["updated"]) == (0, 0)
    # DECISIONS #14: source adapters are sources, never saves
    assert env.conn.execute("SELECT COUNT(*) FROM saves").fetchone()[0] == 0


def test_cli_ingest_plugin(env, capsys, monkeypatch):
    monkeypatch.setenv("GACETA_DATA", str(env.data_dir))
    toml = env.data_dir / "readers" / READER / "reader.toml"
    saved = toml.read_text()
    # a reader without [sources.sqlite] is a config error, not a crash
    toml.write_text(re.sub(r"(?ms)^\[sources\.sqlite\]\n.*?(?=^\[)", "", saved))
    assert cli_main(["ingest", "sqlite", "--reader", READER]) == 2  # no [sources.sqlite]
    assert "no [sources.sqlite]" in capsys.readouterr().err
    toml.write_text(saved)
    assert cli_main(["ingest", "sqlite", "--reader", READER]) == 0
    assert f"{env.info['n_items']} new" in capsys.readouterr().out


# ------------------------------------------------------------ /api/save, /api/unsave

def test_save_per_person_and_validation(server):  # noqa: F811
    port, env, ed = server
    sid = next(iter(ed["stories"]))
    ok = post_json(port, "/api/save",
                   {"reader": READER, "story_id": sid, "by": OWNER, "note": "  volver  "})
    assert ok[0] == 200
    row = env.conn.execute(
        "SELECT origin, note, removed_at FROM saves WHERE reader_id = ?", (READER,)).fetchone()
    assert (row["origin"], row["note"], row["removed_at"]) == ("gaceta", "volver", None)
    # idempotent: same story saved twice → one active row, note updated
    post_json(port, "/api/save", {"reader": READER, "story_id": sid, "by": OWNER, "note": "v2"})
    rows = env.conn.execute(
        "SELECT note FROM saves WHERE reader_id = ? AND removed_at IS NULL", (READER,)).fetchall()
    assert [r["note"] for r in rows] == ["v2"]
    # household readers keep their own saves; unknown story → 400; long note → 400
    assert post_json(port, "/api/save", {"reader": READER, "story_id": sid, "by": "Bob"})[0] == 200
    assert env.conn.execute("SELECT COUNT(*) FROM saves WHERE removed_at IS NULL").fetchone()[0] == 2
    assert post_json(port, "/api/save",
                     {"reader": READER, "story_id": "nope", "by": OWNER})[0] == 400
    assert post_json(port, "/api/save",
                     {"reader": READER, "story_id": sid, "by": OWNER, "note": "x" * 1001})[0] == 400
    # unsave retires only that person's save
    assert post_json(port, "/api/unsave", {"reader": READER, "story_id": sid, "by": "Bob"})[0] == 200
    assert post_json(port, "/api/unsave", {"reader": READER, "story_id": sid, "by": OWNER})[0] == 200
    row = env.conn.execute(
        "SELECT removed_at FROM saves WHERE reader_id = ? AND story_id = ?", (READER, sid)).fetchone()
    assert row["removed_at"] is not None
    # saving again after unsave creates a fresh active row
    assert post_json(port, "/api/save", {"reader": READER, "story_id": sid, "by": OWNER})[0] == 200


def test_state_carries_only_this_persons_saves(server):  # noqa: F811
    port, env, ed = server
    sids = list(ed["stories"])
    post_json(port, "/api/save", {"reader": READER, "story_id": sids[0], "by": OWNER})
    post_json(port, "/api/save", {"reader": READER, "story_id": sids[-1], "by": "Bob"})
    from urllib.parse import quote
    st = json.loads(req(port, "GET", f"/api/state?reader={READER}&by={quote(OWNER)}")[1])
    assert st["saved"] == [sids[0]]
    st = json.loads(req(port, "GET", f"/api/state?reader={READER}&by=Bob")[1])
    assert st["saved"] == [sids[-1]]


# ------------------------------------------------------------ taste

def test_owner_save_weight_and_label_household_ignored(env):
    _seed(env)
    rid = env.cfg["id"]
    now = int(time.time())
    env.conn.execute("INSERT INTO saves (reader_id, by_name, story_id, origin, at) "
                     "VALUES (?, ?, 'stT', 'gaceta', ?)", (rid, OWNER, now))
    env.conn.execute("INSERT INTO saves (reader_id, by_name, story_id, origin, at) "
                     "VALUES (?, 'Bob', 'stT', 'gaceta', ?)", (rid, now))
    env.conn.commit()
    adj = adjustments(env.conn, rid, env.cfg, now=now)
    assert adj.delta.get("stT") == taste.KIND_WEIGHTS["save"]   # Bob's save doesn't steer
    assert "Le diste 'Guardar' (1 vez)" in adj.reasons["stT"]


# ------------------------------------------------------------ guardadas page

def test_guardadas_is_per_person_and_escaped(server):  # noqa: F811
    port, env, ed = server
    sids = list(ed["stories"])
    post_json(port, "/api/save", {"reader": READER, "story_id": sids[0], "by": OWNER,
                                  "note": "nota <b>con</b> marcas"})
    post_json(port, "/api/save", {"reader": READER, "story_id": sids[-1], "by": "Bob"})
    env.conn.execute("UPDATE stories SET title = ? WHERE id = ?",
                     ("<script>alert(1)</script>gato", sids[0]))
    env.conn.execute("UPDATE stories SET title = 'qq-secret-farm de Bob' WHERE id = ?", (sids[-1],))
    env.conn.commit()
    from urllib.parse import quote
    status, body = req(port, "GET", f"/r/{READER}/guardadas?by={quote(OWNER)}")
    doc = body.decode()
    assert status == 200
    assert "<script>alert(1)</script>gato" not in doc
    assert "&lt;script&gt;alert(1)&lt;/script&gt;gato" in doc
    assert "nota &lt;b&gt;con&lt;/b&gt; marcas" in doc
    assert "qq-secret-farm" not in doc                           # Bob's save is Bob's
    assert 'id="gf-q"' in doc and 'id="gf-o"' not in doc          # text filter only
    doc_bob = req(port, "GET", f"/r/{READER}/guardadas?by=Bob")[1].decode()
    assert "qq-secret-farm de Bob" in doc_bob and "alert(1)" not in doc_bob


def test_guardadas_without_person_asks_to_open_from_edition(server):  # noqa: F811
    port, env, ed = server
    post_json(port, "/api/save", {"reader": READER, "story_id": next(iter(ed["stories"])), "by": OWNER})
    doc = req(port, "GET", f"/r/{READER}/guardadas")[1].decode()
    assert "Ábrela desde tu edición" in doc and 'class="story g-entry"' not in doc


def test_guardadas_empty_state(server):  # noqa: F811
    port, env, ed = server
    status, body = req(port, "GET", f"/r/{READER}/guardadas?by=Bob")
    assert status == 200
    assert "Aún no has guardado nada" in body.decode()
