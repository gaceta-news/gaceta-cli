"""Hand a story to the reader's own agent (roadmap 6c, schema v8): owner only,
story must be in the current edition, the send is recorded, the configured
command gets one JSON object on stdin, and no command just records."""
from __future__ import annotations

import json
import sys
import time

from gaceta.serve import deliver_to_agent

from test_discovery import OWNER, READER, post_json, server  # noqa: F401  (fixture)


def _story_id(ed):
    return next(iter(ed["stories"]))


def test_owner_send_is_recorded_without_command(server):  # noqa: F811
    port, env, ed = server
    sid = _story_id(ed)
    status, body = post_json(port, "/api/agent",
                             {"reader": READER, "story_id": sid, "by": OWNER, "note": "  guárdala  "})
    assert status == 200
    out = json.loads(body)
    assert out["ok"] and out["delivered"] is False  # no [agent] command configured
    row = env.conn.execute("SELECT by_name, story_id, note, delivered_at FROM agent_sends").fetchone()
    assert (row["by_name"], row["story_id"], row["note"], row["delivered_at"]) == (OWNER, sid, "guárdala", None)


def test_household_and_bad_input_rejected(server):  # noqa: F811
    port, env, ed = server
    sid = _story_id(ed)
    assert post_json(port, "/api/agent", {"reader": READER, "story_id": sid, "by": "Bob"})[0] == 403
    assert post_json(port, "/api/agent", {"reader": READER, "story_id": "nope", "by": OWNER})[0] == 400
    assert post_json(port, "/api/agent",
                     {"reader": READER, "story_id": sid, "by": OWNER, "note": "x" * 1001})[0] == 400
    assert env.conn.execute("SELECT COUNT(*) FROM agent_sends").fetchone()[0] == 0


def test_configured_command_gets_json_on_stdin(server, tmp_path):  # noqa: F811
    port, env, ed = server
    sid = _story_id(ed)
    sink = tmp_path / "sink.json"
    toml = env.data_dir / "readers" / READER / "reader.toml"
    script = f"import sys; open({str(sink)!r}, 'w').write(sys.stdin.read())"
    toml.write_text(toml.read_text() + f"\n[agent]\ncommand = [{json.dumps(sys.executable)}, \"-c\", {json.dumps(script)}]\n")
    status, body = post_json(port, "/api/agent",
                             {"reader": READER, "story_id": sid, "by": OWNER, "note": "¿qué opinas?"})
    assert status == 200 and json.loads(body)["delivered"] is True
    for _ in range(50):
        if sink.exists() and sink.read_text():
            break
        time.sleep(0.05)
    got = json.loads(sink.read_text())
    assert got["note"] == "¿qué opinas?" and got["story"]["id"] == sid
    assert got["story"]["title"] == ed["stories"][sid]["title"]
    row = env.conn.execute("SELECT delivered_at, error FROM agent_sends").fetchone()
    assert row["delivered_at"] and row["error"] is None


def test_deliver_rejects_shell_strings():
    ok, err = deliver_to_agent({"agent": {"command": "rm -rf / ; echo"}}, {})
    assert ok is False and err and "list of strings" in err
    assert deliver_to_agent({}, {}) == (False, None)


def test_send_is_a_strong_positive_signal(env):
    from gaceta.editor import taste
    from test_discovery import _seed
    _seed(env)
    sid = env.conn.execute("SELECT id FROM stories LIMIT 1").fetchone()["id"]
    env.conn.execute("INSERT INTO agent_sends (reader_id, by_name, story_id, at) VALUES (?,?,?,?)",
                     (env.cfg.get("id") or "testreader", env.cfg.get("name") or "testreader", sid, int(time.time())))
    adj = taste.adjustments(env.conn, env.cfg.get("id") or "testreader", env.cfg)
    assert adj.delta.get(sid) == 2.5
    assert any("mandar a tu agente" in r for r in adj.reasons[sid])
