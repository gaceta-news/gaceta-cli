"""Clustering eval harness: pairwise + B-cubed metrics on a tiny hand-made golden."""
from __future__ import annotations

import json

import pytest

from gaceta.evals import clustering as ec
from gaceta.editor.cluster import cluster, embed_items
from gaceta.providers.embed import FakeEmbedder

from test_cluster import T0, DAY, add_item

GOLDEN = {"items": ["a", "b", "c", "d"], "clusters": [["a", "b"], ["c", "d"]]}


def test_perfect_clustering_f1_is_1():
    m = ec.evaluate([["a", "b"], ["c", "d"]], GOLDEN)
    assert m["precision"] == m["recall"] == m["f1"] == 1.0
    assert m["b3_f1"] == 1.0 and m["n_items"] == 4 and m["n_stories"] == 2


def test_all_singletons_recall_is_0():
    m = ec.evaluate([["a"], ["b"], ["c"], ["d"]], GOLDEN)
    assert m["recall"] == 0.0 and m["precision"] == 0.0 and m["f1"] == 0.0


def test_partial_numbers():
    # predicted [[a,b,c],[d]]: pred pairs {ab,ac,bc}, gold pairs {ab,cd}, tp=1
    m = ec.evaluate([["a", "b", "c"], ["d"]], GOLDEN)
    assert m["precision"] == pytest.approx(1 / 3, abs=1e-3)
    assert m["recall"] == pytest.approx(0.5, abs=1e-3)
    assert m["f1"] == pytest.approx(0.4, abs=1e-3)
    # b-cubed: a,b: p=2/3 r=1/2; c: p=1/3 r=1; d: 1,1 → p=(2/3+2/3+1/3+1)/4, r=(1/2+1/2+1+1)/4
    assert m["b3_precision"] == pytest.approx((2 / 3 + 2 / 3 + 1 / 3 + 1) / 4, abs=1e-3)
    assert m["b3_recall"] == pytest.approx(0.75, abs=1e-3)


def test_restricted_to_golden_items():
    # predicted clusters containing extra items are restricted to the golden universe;
    # golden items in no cluster are predicted singletons
    m = ec.evaluate([["a", "b", "x"], ["c", "e"], ["d"]], GOLDEN)
    # pred pairs within golden: {ab} → p=1/1, r=1/2
    assert m["precision"] == 1.0 and m["recall"] == 0.5


def test_unlisted_golden_items_are_singletons():
    g = {"items": ["a", "b", "c"], "clusters": [["a", "b"]]}
    m = ec.evaluate([["a", "b", "c"]], g)  # c is a gold singleton; gluing it on hurts
    assert m["precision"] == pytest.approx(1 / 3, abs=1e-3)
    assert m["recall"] == 1.0


def test_load_golden_rejects_bad_files(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"items": ["a", "b"], "clusters": [["a"], ["a", "b"]]}))
    with pytest.raises(AssertionError):
        ec.load_golden(p)
    p.write_text(json.dumps({"items": ["a"], "clusters": [["z"]]}))
    with pytest.raises(AssertionError):
        ec.load_golden(p)


def _seed(env):
    add_item(env, "a", "topic one", "alpha beta gamma", published=T0)
    add_item(env, "b", "topic one again", "alpha beta gamma", published=T0 + DAY)
    add_item(env, "c", "other topic", "delta epsilon zeta", published=T0 + 2 * DAY)
    add_item(env, "d", "other topic again", "delta epsilon zeta", published=T0 + 3 * DAY)
    embed_items(env.conn, env.cfg["id"], FakeEmbedder())


def test_run_single_line_matches_db(env, capsys):
    _seed(env)
    cluster(env.conn, env.cfg["id"], env.cfg)
    golden = env.data_dir / "golden.json"
    golden.write_text(json.dumps(GOLDEN))
    ec.run(env.conn, env.cfg["id"], env.cfg, golden, sweep=False)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["threshold"] == 0.70 and line["window_days"] == 21
    assert line["f1"] == 1.0 and line["n_items"] == 4 and line["n_stories"] == 2


def test_run_sweep_no_db_writes_and_best_f1(env, capsys):
    _seed(env)
    golden = env.data_dir / "golden.json"
    golden.write_text(json.dumps(GOLDEN))
    before = env.conn.execute(
        "SELECT COUNT(*) FROM stories WHERE reader_id = ?", (env.cfg["id"],)
    ).fetchone()[0]
    ec.run(env.conn, env.cfg["id"], env.cfg, golden, sweep=True)
    out = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert len(out) == 10  # 9 thresholds + best line
    lines = out[:-1]
    assert [l["threshold"] for l in lines] == ec.SWEEP_THRESHOLDS
    assert all(l["f1"] == 1.0 for l in lines)  # two clean groups → perfect at every threshold
    assert out[-1] == {"best": lines[0]}
    after = env.conn.execute(
        "SELECT COUNT(*) FROM stories WHERE reader_id = ?", (env.cfg["id"],)
    ).fetchone()[0]
    assert before == after == 0  # the sweep wrote nothing
