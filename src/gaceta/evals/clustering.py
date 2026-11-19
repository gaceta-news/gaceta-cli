"""Clustering eval: pairwise (and B-cubed) precision/recall/F1 vs a hand-labelled golden.

Golden JSON: {"items": [ids...], "clusters": [[ids...], ...]} — items listed but in no
cluster are singletons. Metrics are restricted to golden items (WORKFLOW §6).
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np

from ..editor import cluster
from ..providers import embed as embedders
from ..providers import llm as llm_providers

SWEEP_THRESHOLDS = [round(0.50 + 0.05 * i, 2) for i in range(9)]  # 0.50..0.90


def load_golden(path) -> dict:
    with open(path) as f:
        g = json.load(f)
    items, clusters = g["items"], g["clusters"]
    seen = [i for c in clusters for i in c]
    assert len(seen) == len(set(seen)), "an item appears in more than one golden cluster"
    assert set(seen) <= set(items), "golden clusters reference unknown items"
    return {"items": list(items), "clusters": [list(c) for c in clusters]}


def _pairs(clusters) -> set[frozenset]:
    return {frozenset((a, b)) for c in clusters for i, a in enumerate(c) for b in c[i + 1 :]}


def _singletons(ids) -> list[list[str]]:
    return [[i] for i in ids]


def evaluate(pred_clusters: list[list[str]], golden: dict) -> dict:
    """Pairwise + B-cubed P/R/F1 restricted to golden items. Unclustered golden items
    count as predicted singletons."""
    gold_ids = golden["items"]
    gold_map = {}
    for ci, c in enumerate(golden["clusters"]):
        for i in c:
            gold_map[i] = ci
    gi = 0
    for i in gold_ids:
        if i not in gold_map:
            gold_map[i] = f"s{gi}"
            gi += 1
    pred_map = {}
    for ci, c in enumerate(pred_clusters):
        for i in c:
            if i in gold_ids:
                pred_map[i] = ci

    gold_clusters = {}
    pred_clusters_r = {}
    for i in gold_ids:
        gold_clusters.setdefault(gold_map[i], []).append(i)
        pred_clusters_r.setdefault(pred_map.get(i, f"p{i}"), []).append(i)
    gold_list = list(gold_clusters.values())
    pred_list = list(pred_clusters_r.values())

    gold_pairs = _pairs(gold_list)
    pred_pairs = _pairs(pred_list)
    tp = len(gold_pairs & pred_pairs)
    precision = tp / len(pred_pairs) if pred_pairs else 0.0
    recall = tp / len(gold_pairs) if gold_pairs else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    # B-cubed (cheap): per item, overlap of its gold and predicted clusters
    n = len(gold_ids)
    b3p = b3r = 0.0
    for i in gold_ids:
        g = set(gold_clusters[gold_map[i]])
        p = set(pred_clusters_r[pred_map.get(i, f"p{i}")])
        inter = len(g & p)
        b3p += inter / len(p)
        b3r += inter / len(g)
    b3p, b3r = b3p / n, b3r / n
    b3f = 2 * b3p * b3r / (b3p + b3r) if b3p + b3r else 0.0

    n_stories = len({pred_map[i] for i in gold_ids if i in pred_map})
    return {"n_items": n, "precision": round(precision, 4), "recall": round(recall, 4),
            "f1": round(f1, 4), "b3_precision": round(b3p, 4), "b3_recall": round(b3r, 4),
            "b3_f1": round(b3f, 4), "n_stories": n_stories}


def _db_pred_clusters(conn: sqlite3.Connection, reader_id: str) -> list[list[str]]:
    return [
        [r[0] for r in conn.execute(
            "SELECT si.item_id FROM story_items si JOIN stories s ON s.id = si.story_id "
            "WHERE s.reader_id = ? AND si.story_id = ? ORDER BY si.item_id", (reader_id, sid)
        )]
        for (sid,) in conn.execute(
            "SELECT id FROM stories WHERE reader_id = ? ORDER BY id", (reader_id,)
        )
    ]


def _sweep_items(conn: sqlite3.Connection, reader_id: str, model: str) -> list[tuple]:
    rows = cluster._reader_items(conn, reader_id, model=model)
    return [
        (r["id"], r["published"], np.frombuffer(r["vec"], dtype=np.float32), r["item_privacy"])
        for r in rows
    ]


def run(conn: sqlite3.Connection, reader_id: str, cfg: dict, golden_path, sweep: bool = False,
        confirm: bool = False, threshold: float | None = None,
        confirm_low: float | None = None, max_llm_calls: int | None = None):
    """Print one JSON line for the current clustering, or one per sweep threshold + best.

    confirm=True: cluster in memory WITH the LLM confirm step at `threshold`
    (default: the config threshold) and `confirm_low`. Writes only llm_decisions
    (the decision cache); the reader's stories are untouched. The line carries
    P/R/F1 (pairwise + B-cubed) plus llm_calls / llm_cache_hits / tokens.
    """
    golden = load_golden(golden_path)
    c = cfg.get("cluster") or {}
    window_days = int(c.get("window_days", cluster.DEFAULT_WINDOW_DAYS))
    model = embedders.model_name(cfg)

    if confirm:
        th = float(threshold if threshold is not None else c.get("threshold", cluster.DEFAULT_THRESHOLD))
        cl = float(confirm_low if confirm_low is not None
                   else c.get("confirm_low", cluster.DEFAULT_CONFIRM_LOW))
        budget = int(max_llm_calls if max_llm_calls is not None
                 else c.get("max_llm_calls", cluster.DEFAULT_MAX_LLM_CALLS))
        chat = llm_providers.from_config((cfg.get("llm") or {}).get("editor") or {})
        confirmer = cluster.Confirmer(
            chat, conn, cluster._desc_fn(conn, reader_id), confirm_low=cl, max_llm_calls=budget)
        items = _sweep_items(conn, reader_id, model)
        m = evaluate(cluster.partition(items, th, window_days, confirmer=confirmer), golden)
        line = {"threshold": th, "confirm_low": cl, "window_days": window_days, **m,
                **confirmer.report()}
        print(json.dumps(line))
        return line

    if not sweep:
        m = evaluate(_db_pred_clusters(conn, reader_id), golden)
        line = {"threshold": float(c.get("threshold", cluster.DEFAULT_THRESHOLD)),
                "window_days": window_days, **m}
        print(json.dumps(line))
        return line

    items = _sweep_items(conn, reader_id, model)
    lines = []
    for th in SWEEP_THRESHOLDS:
        m = evaluate(cluster.partition(items, th, window_days), golden)
        lines.append({"threshold": th, "window_days": window_days, **m})
        print(json.dumps(lines[-1]))
    best = max(lines, key=lambda l: (l["f1"], l["precision"]))
    print(json.dumps({"best": best}))
    return lines
