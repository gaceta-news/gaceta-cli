"""gaceta CLI: init, ingest, stats, embed, cluster, write, eval."""
from __future__ import annotations

import argparse
import sys

from . import config, db, ingest, media as media_mod
from . import sources as sources_registry
from .edition import build_edition, snapshot_path
from .editor import cluster
from .editor.changes import note_changes
from .editor.writeup import write_stories
from .evals import clustering as eval_clustering
from .providers.embed import from_config as embedder_from_config
from .render import html as render_html


def _db_path() -> str:
    return str(config.data_dir() / "gaceta.db")


SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Sample local feed</title>
<link>https://example.com/</link>
<description>A tiny offline feed written by gaceta init so the quickstart
needs no network. Replace it with real feeds.</description>
{items}
</channel></rss>
"""

_SAMPLE_ITEMS = (
    ("solar-lamps-pass-the-winter-test", "Solar lamps pass the winter test",
     "The balcony solar lamps survived the season. Two connectors failed and "
     "explain the dark corner; everything else kept working through the coldest weeks."),
    ("a-cheaper-solar-panel-for-balconies", "A cheaper solar panel for balconies",
     "A small panel and a reused car battery now run the lights. Total cost fell "
     "below the price of one store-bought solar lamp kit."),
    ("solar-lamp-maker-publishes-repair-guide", "Solar lamp maker publishes a repair guide",
     "The guide shows how to replace the failed connectors that darkened most "
     "balcony lamps this winter. Soldering is optional; a screwdriver is enough."),
    ("the-neighbourhood-bakery-turns-one-hundred", "The neighbourhood bakery turns one hundred",
     "The oven has run every morning for a century. The current bakers kept the "
     "old starter culture and the 6 a.m. queue tradition."),
    ("a-bread-recipe-for-tired-evenings", "A bread recipe for tired evenings",
     "Flour, water, salt and an overnight rest. The bakery shared the simplified "
     "version of its daily loaf for home ovens."),
    ("bakers-slow-fermentation-wins-award", "Bakers' slow fermentation wins an award",
     "The jury praised the crust and the crumb of the hundred-year loaf. The "
     "bakers credit patience, flour quality and a very stable oven."),
)


def _sample_feed_xml(now: int) -> str:
    from email.utils import formatdate

    items = "\n".join(
        f"<item><title>{title}</title>"
        f"<link>https://example.com/{slug}</link>"
        f"<guid>{slug}</guid>"
        f"<pubDate>{formatdate(now - (i + 1) * 3600, usegmt=True)}</pubDate>"
        f"<description>{body}</description></item>"
        for i, (slug, title, body) in enumerate(_SAMPLE_ITEMS)
    )
    return SAMPLE_FEED.format(items=items)


EXAMPLE_READER_TOML = """# gaceta reader configuration. Everything personal lives here, never in src/.
# Docs: https://example.invalid/gaceta (see README.md for all options).

id = "{reader}"
name = "{name}"
langs = "en"          # write-up languages, comma list, e.g. "es,en" (first = main)
# ui_lang = "en"      # paper interface: en, es, pt, fr, de, it (default: first of langs)
# masthead = "La Gaceta"  # the paper's name on the masthead

[privacy]
# Words that must never appear outside the given privacy level (canaries).
pinned = []           # → private (only the owner ever sees them)
household = []        # → household (shared paper, still never public)

# --- sources ---------------------------------------------------------------
# Offline sample feed written by `gaceta init` (data/sample-feed.xml).
# Swap it for real feeds: one [[sources.rss.feeds]] block per feed.

[[sources.rss.feeds]]
id = "sample"
name = "Sample local feed"
file = "sample-feed.xml"   # relative to the data dir; offline, no network
# url = "https://example.com/feed.xml"   # a real feed instead of file =
# kind = "rss"                            # or "youtube" for channel feeds

# Example public feeds to try once online:
# [[sources.rss.feeds]]
# id = "arxiv-cs"
# name = "arXiv cs.AI"
# url = "https://rss.arxiv.org/rss/cs.AI"

# [[sources.rss.feeds]]
# id = "bbc-tech"
# name = "BBC Technology"
# url = "https://feeds.bbci.co.uk/news/technology/rss.xml"

# Other adapters (see README): [sources.nostr_wot], [sources.sqlite],
# or any package registering a `gaceta.sources` entry point.

# --- model providers -------------------------------------------------------
# Zero-setup defaults use deterministic fakes (fine to try the pipeline;
# not useful output). Point them at real services when ready.

[embed]
provider = "fake"
# provider = "ollama"                     # local embeddings via Ollama
# model = "bge-m3"
# base_url = "http://localhost:11434"
# provider = "openai"                     # any OpenAI-compatible /embeddings
# model = "text-embedding-3-small"
# base_url = "https://api.openai.com/v1"
# api_key_env = "OPENAI_API_KEY"          # env var holding the key

[llm.editor]
provider = "fake"
# provider = "openai"                     # any OpenAI-compatible /chat/completions
# model = "gpt-4o-mini"
# base_url = "https://api.openai.com/v1"
# api_key_env = "OPENAI_API_KEY"          # env var holding the key; "" for local
#                                          # servers without auth (Ollama /v1, llama.cpp)
# allow_private = false                   # true only for providers you trust
#                                          # with the reader's private notes

[cluster]
threshold = 0.70      # cosine similarity for "same story"
window_days = 21      # how long a story stays open
"""


def cmd_init(args) -> int:
    import time

    d = config.data_dir()
    d.mkdir(parents=True, exist_ok=True)
    conn = db.connect(_db_path())
    db.init(conn)
    conn.close()
    wrote = []
    reader = getattr(args, "reader", None) or "main"
    reader_dir = config.data_dir() / "readers" / reader
    reader_dir.mkdir(parents=True, exist_ok=True)
    toml = reader_dir / "reader.toml"
    if not toml.exists():
        toml.write_text(
            EXAMPLE_READER_TOML.format(reader=reader, name=reader.replace("-", " ").title()),
            encoding="utf-8",
        )
        wrote.append(str(toml))
    sample = config.data_dir() / "sample-feed.xml"
    if not sample.exists():
        sample.write_text(_sample_feed_xml(int(time.time())), encoding="utf-8")
        wrote.append(str(sample))
    print(f"initialised {_db_path()}")
    for p in wrote:
        print(f"  wrote {p}")
    if wrote:
        print(f"next: gaceta ingest rss --reader {reader}")
    return 0


def _ingest_rss(args, cfg, sources, conn) -> int:
    rs = sources.get("rss")
    if not rs or not rs.get("feeds"):
        print(f"reader '{args.reader}' has no [[sources.rss.feeds]]", file=sys.stderr)
        return 2
    from .sources.rss import RssAdapter
    from .transcripts import Transcripts

    data = config.data_dir()
    for feed in rs["feeds"]:
        f = feed.get("file")
        if f and not str(f).startswith("/"):  # offline fixture: relative to the data dir
            feed["file"] = str((data / f).resolve())
    transcripts = Transcripts(rs.get("transcripts"), data)
    adapter = RssAdapter(rs, transcript=transcripts.get_with_origin)
    result = ingest.ingest(conn, adapter, cfg)
    bad = f", {len(adapter.errors)} feeds failed ({', '.join(f for f, _ in adapter.errors)})" if adapter.errors else ""
    print(f"ingest rss reader={args.reader}: {len(adapter.feeds)} feeds, {result['new']} new, "
          f"{result['updated']} updated, {adapter.videos_without_transcript} videos omitted (no transcript){bad}")
    return int(len(adapter.errors) == len(adapter.feeds))


def _ingest_nostr_wot(args, cfg, sources, conn) -> int:
    nw = sources.get("nostr_wot")
    if not nw or not nw.get("pubkey"):
        print(f"reader '{args.reader}' has no [sources.nostr_wot] pubkey", file=sys.stderr)
        return 2
    from .sources.nostr_wot import NostrWotAdapter, NotReadyError

    adapter = NostrWotAdapter(nw)
    # readiness BEFORE any write: without a resolvable observer the relay
    # silently falls back to the anonymous (spam) ranking, so a not-ready
    # lens must ingest nothing (roadmap 11a)
    verdict = adapter.readiness()
    if not verdict["ready"]:
        print(f"ingest nostr-wot reader={args.reader}: NOT READY ({verdict['state']}) "
              f"through {adapter.relay} — nothing ingested", file=sys.stderr)
        return 2
    try:
        result = ingest.ingest(conn, adapter, cfg)
    except NotReadyError as exc:  # lens flipped mid-run; defense in depth
        print(f"ingest nostr-wot reader={args.reader}: {exc} — nothing ingested",
              file=sys.stderr)
        return 2
    print(
        f"ingest nostr-wot reader={args.reader}: {adapter.fetched} fetched, "
        f"{result['new']} new, {result['updated']} updated, "
        f"{len(adapter.authors)} distinct authors (relay={adapter.relay}, "
        f"floor={adapter.floor}, window={adapter.window_hours}h)"
    )
    return 0


def _ingest_plugin(args, cfg, sources, conn, name: str) -> int:
    """Generic dispatch: table built-ins and entry-point plugins."""
    table = sources.get(name)
    if table is None:
        print(f"reader '{args.reader}' has no [sources.{name}]", file=sys.stderr)
        return 2
    try:
        adapter = sources_registry.build(name, table)
    except KeyError:
        print(f"unknown adapter '{name}' (available: {', '.join(sources_registry.available_adapters())})",
              file=sys.stderr)
        return 2
    result = ingest.ingest(conn, adapter, cfg)
    print(f"ingest {name} reader={args.reader}: {result['new']} new, {result['updated']} updated, "
          f"{result['told']} told rows")
    return 0


def cmd_ingest(args) -> int:
    if args.adapter == "all":
        # every adapter the reader configures; one failing adapter never blocks the others
        cfg = config.load_reader(args.reader)
        configured = cfg.get("sources", {})
        names = [a for a in sources_registry.available_adapters() if a in configured]
        if not names:
            print(f"reader '{args.reader}' configures no sources", file=sys.stderr)
            return 2
        failed = 0
        for name in names:
            try:
                rc = cmd_ingest(argparse.Namespace(adapter=name, reader=args.reader))
            except Exception as exc:  # noqa: BLE001
                print(f"ingest {name} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                rc = 1
            failed += rc != 0
        return 1 if failed == len(names) else 0
    cfg = config.load_reader(args.reader)
    sources = cfg.get("sources", {})
    conn = db.connect(_db_path())
    db.init(conn)
    try:
        if args.adapter == "rss":
            return _ingest_rss(args, cfg, sources, conn)
        if args.adapter in ("nostr-wot", "nostr_wot"):
            return _ingest_nostr_wot(args, cfg, sources, conn)
        return _ingest_plugin(args, cfg, sources, conn, args.adapter)
    finally:
        conn.close()


def cmd_stats(args) -> int:
    conn = db.connect(_db_path())
    reader = conn.execute("SELECT id FROM readers WHERE id = ?", (args.reader,)).fetchone()
    if reader is None:
        print(f"unknown reader '{args.reader}'", file=sys.stderr)
        return 2
    rows = conn.execute(
        "SELECT s.kind AS k, COUNT(*) AS n FROM annotations a "
        "JOIN items i ON i.id = a.item_id JOIN sources s ON s.id = i.source_id "
        "WHERE a.reader_id = ? GROUP BY s.kind ORDER BY n DESC",
        (args.reader,),
    ).fetchall()
    print("by kind:")
    for r in rows:
        print(f"  {r['k']}: {r['n']}")
    rows = conn.execute(
        "SELECT verdict AS k, COUNT(*) AS n FROM annotations WHERE reader_id = ? GROUP BY verdict ORDER BY n DESC",
        (args.reader,),
    ).fetchall()
    print("by verdict:")
    for r in rows:
        print(f"  {r['k']}: {r['n']}")
    rows = conn.execute(
        "SELECT item_privacy AS k, COUNT(*) AS n FROM annotations WHERE reader_id = ? GROUP BY item_privacy ORDER BY n DESC",
        (args.reader,),
    ).fetchall()
    print("by privacy:")
    for r in rows:
        print(f"  {r['k']}: {r['n']}")
    conn.close()
    return 0


def cmd_embed(args) -> int:
    cfg = config.load_reader(args.reader)
    try:
        embedder = embedder_from_config(cfg)
    except ValueError as e:
        print(f"reader '{args.reader}': {e}", file=sys.stderr)
        return 2
    conn = db.connect(_db_path())
    db.init(conn)

    def progress(done, total):
        print(f"embedding {args.reader}: {done}/{total}", flush=True)

    result = cluster.embed_items(conn, args.reader, embedder, progress=progress)
    conn.close()
    print(
        f"embed reader={args.reader} model={result['model']}: "
        f"{result['embedded']} embedded, {result['skipped']} unchanged/skipped"
    )
    return 0


def cmd_cluster(args) -> int:
    cfg = config.load_reader(args.reader)
    conn = db.connect(_db_path())
    try:
        db.init(conn)
        result = cluster.cluster(conn, args.reader, cfg, rebuild=args.rebuild)
    except ValueError as e:
        print(f"reader '{args.reader}': {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(
        f"cluster reader={args.reader}{' --rebuild' if args.rebuild else ''}: "
        f"{result['n_items']} new items → {result['n_new_stories']} new stories, "
        f"{result['n_joined_stories']} joined; total {result['n_stories']} stories "
        f"(threshold={result['threshold']}, window={result['window_days']}d, model={result['model']})"
    )
    return 0


def cmd_write(args) -> int:
    cfg = config.load_reader(args.reader)
    conn = db.connect(_db_path())
    try:
        db.init(conn)
        result = write_stories(
            conn, args.reader, cfg,
            days=args.days, max_llm_calls=args.max_llm_calls,
        )
        # what each rewrite added over the previous card ('Desde que la viste');
        # a failure here never costs the write-ups already committed
        try:
            ch = note_changes(conn, args.reader, cfg, days=args.days,
                              max_llm_calls=args.max_change_calls)
        except Exception as e:  # noqa: BLE001 — network/provider; retried next run
            ch = {"error": f"{type(e).__name__}: {e}"}
    except ValueError as e:
        print(f"reader '{args.reader}': {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    served = ",".join(result["served_models"]) or result["model"]
    print(
        f"write reader={args.reader} days={result['days']}: "
        f"{result['window_stories']} stories to write, {result['written']} written, "
        f"{result['failed']} failed (invalid JSON), {result['pending']} pending"
        f"{' (budget exhausted)' if result['budget_exhausted'] else ''}"
        f"{f', {n} rewritten in the reader language' if (n := result.get('relanguaged')) else ''}; "
        f"calls={result['llm_calls']} cache={result['llm_cache_hits']} "
        f"tokens={result['llm_prompt_tokens']}+{result['llm_completion_tokens']}; "
        f"served={served}"
    )
    if "error" in ch:
        print(f"changes reader={args.reader}: failed ({ch['error']}); retried next run")
    else:
        print(
            f"changes reader={args.reader}: {ch['candidates']} versions to explain, "
            f"{ch['noted']} noted, {ch['unchanged']} only reworded, {ch['failed']} failed, "
            f"{ch['skipped']} skipped (private){' (budget exhausted)' if ch['budget_exhausted'] else ''}; "
            f"calls={ch['llm_calls']} cache={ch['llm_cache_hits']}"
        )
    return 0


def cmd_media(args) -> int:
    conn = db.connect(_db_path())
    try:
        db.init(conn)
        result = media_mod.collect_media(
            conn, args.reader, config.data_dir(),
            days=args.days, max_fetch=args.max_fetch,
        )
    finally:
        conn.close()
    rows = db.connect(_db_path()).execute(
        "SELECT kind, status, COUNT(*) AS n FROM media GROUP BY kind, status ORDER BY kind, status"
    ).fetchall()
    summary = ", ".join(f"{r['kind']}/{r['status']}={r['n']}" for r in rows) or "none"
    print(
        f"media reader={args.reader} days={args.days}: {result['candidates']} candidate items, "
        f"{result['planned']} fetched this run ({result['bytes_downloaded'] / 1e6:.1f} MB); "
        f"table: {summary}"
    )
    return 0


def cmd_edition(args) -> int:
    cfg = config.load_reader(args.reader)
    conn = db.connect(_db_path())
    try:
        db.init(conn)
        edition = build_edition(
            conn, args.reader, cfg, date_str=args.date,
            max_privacy=args.max_privacy, force=args.force,
            data_dir=config.data_dir(),
        )
    except (ValueError, FileExistsError) as e:
        print(str(e), file=sys.stderr)
        return 2
    finally:
        conn.close()
    counts = ", ".join(f"{s['id']}={len(s['stories'])}" for s in edition["sections"])
    print(
        f"edition reader={args.reader} date={edition['date']} max={args.max_privacy}: "
        f"portada={len(edition['portada'])} stories={len(edition['stories'])} "
        f"sections[{counts}] → {snapshot_path(config.data_dir(), args.reader, edition['date'], args.max_privacy)}"
    )
    return 0


def _today() -> str:
    # same clock as edition.build's default (local time)
    from datetime import datetime
    return datetime.now().date().isoformat()


def cmd_render(args) -> int:
    import json
    import shutil

    args.date = args.date or _today()
    data = config.data_dir()
    snap = snapshot_path(data, args.reader, args.date, args.max_privacy)
    if not snap.exists():
        print(f"no edition snapshot at {snap} (run: gaceta edition --reader {args.reader} "
              f"--date {args.date} --max-privacy {args.max_privacy})", file=sys.stderr)
        return 2
    edition = json.loads(snap.read_text(encoding="utf-8"))
    try:
        doc = render_html.render(edition, args.max_privacy)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    site = data / "site" / args.reader
    site.mkdir(parents=True, exist_ok=True)
    out = site / f"{args.date}.html"
    out.write_text(doc, encoding="utf-8")
    shutil.copyfile(out, site / "index.html")
    print(f"render reader={args.reader} date={args.date} → {out} (and index.html)")
    return 0


def cmd_print(args) -> int:
    """Export an edition as local HTML, optionally with a PDF alongside it."""
    args.date = args.date or _today()
    import json
    import os
    import re
    from datetime import date
    from pathlib import Path
    from urllib.parse import quote

    from . import print as print_mod

    try:
        valid_date = date.fromisoformat(args.date).isoformat() == args.date
    except ValueError:
        valid_date = False
    if not valid_date or not re.fullmatch(r"[A-Za-z0-9_-]+", args.reader):
        print("expected a reader slug and a YYYY-MM-DD date", file=sys.stderr)
        return 2
    data = config.data_dir()
    snap = snapshot_path(data, args.reader, args.date, args.max_privacy)
    if not snap.exists():
        print(f"no edition snapshot at {snap} (run: gaceta edition --reader {args.reader} "
              f"--date {args.date} --max-privacy {args.max_privacy})", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else (
        data / "print" / args.reader / f"{args.date}-{args.max_privacy}.html")
    if out.suffix.lower() != ".html":
        print("--out must end in .html (use --pdf to also produce a PDF)", file=sys.stderr)
        return 2
    media_prefix = quote(os.path.relpath(data / "site" / "media", out.parent)) + "/"
    try:
        edition = json.loads(snap.read_text(encoding="utf-8"))
        doc = print_mod.print_html(edition, args.max_privacy, with_images=args.with_images,
                                   media_prefix=media_prefix)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(f"print reader={args.reader} date={args.date} → {out}")
    if args.pdf:
        pdf = out.with_suffix(".pdf")
        try:
            print_mod.render_pdf(out, pdf)
        except RuntimeError as e:
            print(f"pdf failed: {e}", file=sys.stderr)
            return 2
        print(f"pdf → {pdf} ({pdf.stat().st_size // 1024} KB)")
    return 0


def cmd_feedback(args) -> int:
    from . import feedback

    cfg = config.load_reader(args.reader)
    conn = db.connect(_db_path())
    db.init(conn)
    out, rep = feedback.export(conn, args.reader, cfg, out=args.out, days=args.days)
    conn.close()
    t = rep["totals"]
    print(f"feedback reader={args.reader}: más {t['mas']} · menos {t['menos']} · visto {t['visto']}, "
          f"{len(rep['sources'])} sources → {out}")
    return 0


def cmd_sources(args) -> int:
    """`gaceta sources pending`: JSON of decisions not yet exported; the
    reader's agent applies them upstream (gaceta never edits a collector)."""
    import json
    import time

    conn = db.connect(_db_path())
    db.init(conn)
    try:
        if conn.execute("SELECT 1 FROM readers WHERE id = ?",
                        (args.reader,)).fetchone() is None:
            print(f"unknown reader '{args.reader}'", file=sys.stderr)
            return 2
        rows = conn.execute(
            "SELECT source_key, decision, by_name, at FROM source_decisions "
            "WHERE reader_id = ? AND exported_at IS NULL ORDER BY at",
            (args.reader,),
        ).fetchall()
        payload = [
            {"reader": args.reader, "source_key": r["source_key"],
             "decision": r["decision"], "by": r["by_name"], "at": r["at"]}
            for r in rows
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.mark_exported:
            conn.execute(
                "UPDATE source_decisions SET exported_at = ? "
                "WHERE reader_id = ? AND exported_at IS NULL",
                (int(time.time()), args.reader),
            )
            conn.commit()
        return 0
    finally:
        conn.close()


def cmd_eval_clustering(args) -> int:
    cfg = config.load_reader(args.reader)
    conn = db.connect(_db_path())
    db.init(conn)
    try:
        eval_clustering.run(
            conn, args.reader, cfg, args.golden, sweep=args.sweep, confirm=args.confirm,
            threshold=args.threshold, confirm_low=args.confirm_low,
            max_llm_calls=args.max_llm_calls,
        )
    except ValueError as e:
        print(f"reader '{args.reader}': {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gaceta")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="create data dir, gaceta.db and an example reader config")
    pi.add_argument("--reader", default="main", help="reader id for the example config (default main)")
    pi.set_defaults(func=cmd_init)

    pi = sub.add_parser("ingest", help="ingest from a source adapter")
    isub = pi.add_subparsers(dest="adapter", required=True)
    prs = isub.add_parser("rss", help="RSS/Atom/YouTube feeds from [[sources.rss.feeds]]")
    prs.add_argument("--reader", required=True)
    pnw = isub.add_parser("nostr-wot", help="WoT-ranked nostr notes (readiness-gated)")
    pnw.add_argument("--reader", required=True)
    for _name in sorted(sources_registry.adapter_classes()):
        if _name in ("rss", "nostr_wot"):
            continue
        _p = isub.add_parser(_name, help=f"plugin source adapter [sources.{_name}]")
        _p.add_argument("--reader", required=True)
    pall = isub.add_parser("all", help="every adapter the reader configures")
    pall.add_argument("--reader", required=True)
    pi.set_defaults(func=cmd_ingest)

    ps = sub.add_parser("stats", help="counts by kind, verdict and privacy label")
    ps.add_argument("--reader", required=True)
    ps.set_defaults(func=cmd_stats)

    pem = sub.add_parser("embed", help="embed noted/told items for a reader")
    pem.add_argument("--reader", required=True)
    pem.set_defaults(func=cmd_embed)

    pcl = sub.add_parser("cluster", help="cluster embedded items into stories")
    pcl.add_argument("--reader", required=True)
    pcl.add_argument("--rebuild", action="store_true", help="wipe the reader's stories and recluster")
    pcl.set_defaults(func=cmd_cluster)

    pw = sub.add_parser("write", help="write/rewrite story cards for changed stories")
    pw.add_argument("--reader", required=True)
    pw.add_argument("--days", type=int, default=14, help="last_seen window in days (default 14)")
    pw.add_argument("--max-llm-calls", type=int, default=150, help="LLM call budget (default 150)")
    pw.add_argument("--max-change-calls", type=int, default=40,
                    help="LLM call budget for 'what changed' notes (default 40)")
    pw.set_defaults(func=cmd_write)

    pm = sub.add_parser("media", help="discover and cache media for recent story items")
    pm.add_argument("--reader", required=True)
    pm.add_argument("--days", type=int, default=14, help="last_seen window in days (default 14)")
    pm.add_argument("--max-fetch", type=int, default=300,
                    help="network request budget: page GETs + image downloads (default 300)")
    pm.set_defaults(func=cmd_media)

    ped = sub.add_parser("edition", help="build an immutable edition snapshot")
    ped.add_argument("--reader", required=True)
    ped.add_argument("--date", default=None, help="edition date YYYY-MM-DD (default today)")
    ped.add_argument("--max-privacy", default="private", choices=("public", "household", "private"))
    ped.add_argument("--force", action="store_true", help="overwrite an existing snapshot")
    ped.set_defaults(func=cmd_edition)

    pr = sub.add_parser("render", help="render an edition snapshot to static HTML")
    pr.add_argument("--reader", required=True)
    pr.add_argument("--date", default=None, help="edition date YYYY-MM-DD (default today)")
    pr.add_argument("--max-privacy", default="private", choices=("public", "household", "private"))
    pr.set_defaults(func=cmd_render)

    ppr = sub.add_parser("print", help="edition as a file (HTML) or PDF, for readers without an account")
    ppr.add_argument("--reader", required=True)
    ppr.add_argument("--date", default=None, help="edition date YYYY-MM-DD (default today)")
    ppr.add_argument("--max-privacy", default="public", choices=("public", "household", "private"))
    ppr.add_argument("--out", default=None, help="HTML path (default data/print/<reader>/<date>-<privacy>.html)")
    ppr.add_argument("--pdf", action="store_true", help="also render a PDF next to the HTML")
    ppr.add_argument("--with-images", action="store_true",
                     help="include cached local images (default: text only)")
    ppr.set_defaults(func=cmd_print)

    pfb = sub.add_parser("feedback", help="export the owner's verdicts for an upstream collector")
    pfb.add_argument("--reader", required=True)
    pfb.add_argument("--out", default=None,
                     help="markdown path (default [feedback] out, else <reader dir>/feedback-export.md)")
    pfb.add_argument("--days", type=int, default=30, help="window in days (default 30)")
    pfb.set_defaults(func=cmd_feedback)

    psrc = sub.add_parser("sources", help="discovered-source decisions (roadmap 6b)")
    srcsub = psrc.add_subparsers(dest="action", required=True)
    spend = srcsub.add_parser("pending", help="print JSON of decisions not yet exported")
    spend.add_argument("--reader", required=True)
    spend.add_argument("--mark-exported", action="store_true",
                       help="stamp the printed rows as exported (exported_at)")
    psrc.set_defaults(func=cmd_sources)

    pev = sub.add_parser("eval", help="run an eval")
    evsub = pev.add_subparsers(dest="eval", required=True)
    evc = evsub.add_parser("clustering", help="clustering metrics vs a golden file")
    evc.add_argument("--reader", required=True)
    evc.add_argument("--golden", required=True, help="path to golden-clusters JSON")
    evc.add_argument("--sweep", action="store_true", help="sweep thresholds 0.50..0.90 in memory")
    evc.add_argument("--confirm", action="store_true",
                     help="cluster in memory with the LLM confirm step (writes only llm_decisions)")
    evc.add_argument("--threshold", type=float, default=None, help="threshold for --confirm")
    evc.add_argument("--confirm-low", type=float, default=None,
                     help="confirm zone floor / anti-chaining gate for --confirm")
    evc.add_argument("--max-llm-calls", type=int, default=None,
                     help="LLM call budget for --confirm (default 400)")
    evc.set_defaults(func=cmd_eval_clustering)

    # --- serve (p4): local web server with reactions; one block, nothing else changes
    from .serve import cmd_serve  # local import keeps the block self-contained

    psv = sub.add_parser("serve", help="serve the built paper locally with reactions (localhost only)")
    psv.add_argument("--host", default="127.0.0.1",
                     help="bind address: 127.0.0.1, ::1 or localhost (default 127.0.0.1)")
    psv.add_argument("--port", type=int, default=8711, help="listen port (default 8711)")
    psv.set_defaults(func=cmd_serve)
    # --- end serve

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
