"""gaceta CLI: init, ingest, stats, embed, cluster, write, eval, trial."""
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
        # [llm.editor] preflight = true: spend one 1-token request before the
        # run's real calls, so a quota-limited primary is skipped from the
        # start. The same chat object serves write-ups and change notes, so
        # the chain's down-state is shared across both.
        chat = None
        llm_cfg = (cfg.get("llm") or {}).get("editor") or {}
        if bool(llm_cfg.get("preflight", False)):
            from .providers import llm as llm_providers

            chat = llm_providers.from_config(llm_cfg)
            preflight = getattr(chat, "preflight", None)
            if callable(preflight):
                preflight()
        result = write_stories(
            conn, args.reader, cfg,
            days=args.days, max_llm_calls=args.max_llm_calls, chat=chat,
        )
        # what each rewrite added over the previous card ('Desde que la viste');
        # a failure here never costs the write-ups already committed
        try:
            ch = note_changes(conn, args.reader, cfg, days=args.days,
                              max_llm_calls=args.max_change_calls, chat=chat)
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
    conn = db.connect(_db_path())
    try:
        rows = conn.execute(
            "SELECT kind, status, COUNT(*) AS n FROM media GROUP BY kind, status ORDER BY kind, status"
        ).fetchall()
    finally:
        conn.close()
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


def cmd_agent(args) -> int:
    """`gaceta agent …`: the public agent surface (agent.py for the contract).

    export = owner side (reads the day's PUBLIC edition snapshot and the
    paper's own version evidence, writes the isolated export). receipt =
    owner side too, but opt-in and consent-shaped: one person's own seen
    mark for one public story becomes their baseline receipt on stdout.
    edition/story/since = agent side: one export file, read-only, fail
    closed on private/stale/unknown/foreign content; no marks, no writes.
    `since` answers the launch question from the caller's own baseline file.
    """
    import json
    from pathlib import Path

    from . import agent as agent_mod

    if args.action == "export":
        try:
            reader = agent_mod.validate_reader_id(args.reader)
            cfg = config.load_reader(reader)
            conn = db.connect(_db_path())
            try:
                db.init(conn)
                export, dated, current = agent_mod.export_for_date(
                    config.data_dir(), reader, cfg, date_str=args.date, conn=conn)
            finally:
                conn.close()
        except (agent_mod.ExportError, ValueError, OSError) as e:
            print(str(e), file=sys.stderr)
            return 2
        ed = export["edition"]
        print(f"agent export reader={reader} date={ed['date']}: "
              f"{len(export['stories'])} public stories → {dated} (and {current})")
        return 0

    if args.action == "receipt":
        # owner side, opt-in: ONE person's own seen mark for ONE public
        # story → the baseline receipt the existing since path accepts.
        # SELECTs only; gaceta writes nothing — stdout is the receipt.
        try:
            reader = agent_mod.validate_reader_id(args.reader)
            conn = db.connect(_db_path())
            try:
                receipt = agent_mod.derive_receipt(
                    conn, reader, args.by, args.story)
            finally:
                conn.close()
        except (agent_mod.ExportError, OSError) as e:
            print(str(e), file=sys.stderr)
            return 2
        by = str(args.by).strip()
        if receipt["status"] == "known":
            state = (f"verified → write-up version {receipt['version']} — this "
                     f"is {by}'s own memory: hand it to them, never into the "
                     "public export")
        else:
            state = f"unknown — {receipt['reason']}"
        print(f"agent receipt reader={reader} by={by} story={args.story}: "
              f"{state}", file=sys.stderr)
        print(json.dumps(agent_mod.receipt_baseline(receipt),
                         ensure_ascii=False, indent=2))
        return 0

    # agent side: read-only lookup; the database is never opened
    try:
        reader = agent_mod.validate_reader_id(args.reader)
        path = (Path(args.path) if args.path
                else agent_mod.current_export_path(config.data_dir(), reader))
        export = agent_mod.load_export(
            path, reader=reader,
            max_age_hours=(args.max_age_hours if args.max_age_hours is not None
                           else agent_mod.DEFAULT_MAX_AGE_HOURS))
        if args.action == "edition":
            view = agent_mod.edition_view(export)
        elif args.action == "story":
            view = agent_mod.story_view(export, args.id)
        else:
            baseline = agent_mod.load_baseline(args.baseline, reader=reader,
                                               export=export)
            view = agent_mod.since_view(export, baseline)
    except agent_mod.ExportError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(json.dumps(view, ensure_ascii=False, indent=2))
    return 0



def _package_version() -> str:
    """The one version number this CLI reports (release check: tag/wheel/CLI parity)."""
    from . import __version__
    return __version__


def _trial_print_state(rep: dict) -> None:
    print(f"trial {rep['id']} — {rep['state']} (started {rep['start_day']})")
    if rep["expired"]:
        print(f"  expired: the seven-day window {rep['start_day']}..{rep['last_day']} "
              "ended; no catchup")
    else:
        print(f"  day {rep['day_number']} of {rep['days_total']} "
              f"(window {rep['start_day']}..{rep['last_day']})")
    today = rep["today"]
    print(f"  run today ({today['day']}): {today['used']}/{today['cap']} spent")
    print(f"  runs: {rep['runs_total']}/{rep['runs_cap']} spent, {rep['runs_left']} left "
          "(no fourth run, on any day)")
    totals = rep["totals"]
    print(f"  history: {len(rep['interrupted'])} interrupted, "
          f"{len(rep['failed'])} failed; fake-chat calls: {totals['llm_calls']} write + "
          f"{totals['change_calls']} change (lifetime caps by construction: "
          f"{totals['llm_calls_lifetime_cap']} write, "
          f"{totals['change_calls_lifetime_cap']} change)")
    if rep["lock"]:
        print(f"  lock: pid {rep['lock']['pid']} "
              f"({'STALE — a run was killed; --recover-lock to continue' if rep['lock']['stale'] else 'held — a run may be in progress'})")
    for snap in rep["snapshots"]:
        print(f"  snapshot: {snap}")


def cmd_trial(args) -> int:
    """`gaceta trial …`: the finite manual trial (p3; src/gaceta/trial.py for
    the contract). preview/status are read-only; every write action prints its
    plan and needs --yes (or an interactive 'yes') before anything is written."""
    import time
    from datetime import datetime

    from . import trial as trial_mod

    def confirm(plan: str) -> bool:
        print(plan)
        if getattr(args, "yes", False):
            return True
        if sys.stdin.isatty():
            try:
                return input("Proceed? type yes: ").strip().lower() == "yes"
            except EOFError:
                return False
        print("no consent given (stdin is not interactive; pass --yes)", file=sys.stderr)
        return False

    base = config.data_dir()
    now = time.time()
    try:
        if args.action == "preview":
            rep = trial_mod.preview(base, getattr(args, "id", None), now=now)
            if "start_would_create" in rep:      # no trial yet
                print(f"no trial yet — nothing under {rep['root']}")
                would = rep["start_would_create"]
                print(f"start would create trial {would['id']} at {would['root']}")
                for p in would["files"]:
                    print(f"  {p}")
                print(f"  bounds: {would['runs_per_day']} run/day, at most "
                      f"{would['runs_total']} runs total, {would['days_total']} days, "
                      "no scheduling, no catchup")
                print(f"next: {rep['hint']}")
            else:
                _trial_print_state(rep)
                if rep["would_run"] == "ok":
                    print(f"next run: {rep['hint']}")
                else:
                    print(f"next run: refused — {rep['would_run']}")
            return 0

        if args.action == "status":
            rep = trial_mod.status(base, getattr(args, "id", None), now=now)
            _trial_print_state(rep)
            return 0

        if args.action == "start":
            rep = trial_mod.start(base, getattr(args, "id", None), now=now,
                                  confirm=confirm)
            print(f"trial {rep['id']} started → {rep['root']}")
            for p in rep["created"]:
                print(f"  wrote {p}")
            print(f"  bounds: {rep['runs_per_day']} run per day, at most "
                  f"{rep['runs_total']} runs total, window "
                  f"{rep['start_day']}..{rep['last_day']} ({rep['days_total']} days, "
                  "then expired; no fourth run, no catchup, no scheduling)")
            print(f"next: gaceta trial run --yes")
            return 0

        if args.action == "run":
            rep = trial_mod.run(
                base, getattr(args, "id", None), now=now, confirm=confirm,
                max_llm_calls=getattr(args, "max_llm_calls", trial_mod.DEFAULT_MAX_LLM_CALLS),
                max_change_calls=getattr(args, "max_change_calls", trial_mod.DEFAULT_MAX_CHANGE_CALLS),
                recover_lock=getattr(args, "recover_lock", False))
            r, o = rep["run"], rep["outcome"]
            print(f"trial {rep['id']} run {r['run']}/{trial_mod.MAX_RUNS_TOTAL} "
                  f"on {r['day']}: {r['status']}")
            print(f"  ingest {o['items']} new · embedded {o['embedded']} · "
                  f"{o['stories']} stories · written {o['written']} "
                  f"(fake-chat calls {o['llm_calls']}, "
                  f"budget{' exhausted' if o['budget_exhausted'] else ' ok'})")
            if o["change_note"]:
                print(f"  changes: failed ({o['change_note']}); retried next run")
            print(f"  edition {o['edition_stories']} stories → {o['snapshot']}")
            if o["html"]:
                print(f"  render → {o['html']}")
            print(f"  run today: {rep['today']['used']}/{rep['today']['cap']}; "
                  f"runs left: {rep['runs_left']}/{trial_mod.MAX_RUNS_TOTAL}")
            if rep["runs_left"] == 0:
                print(f"  {trial_mod.MAX_RUNS_TOTAL}/{trial_mod.MAX_RUNS_TOTAL} runs "
                      "spent — the trial is run-terminal: keep or remove it "
                      "(a fresh day buys nothing)")
            return 0

        if args.action == "stop":
            rep = trial_mod.stop(base, getattr(args, "id", None), now=now,
                                 confirm=confirm)
            print(f"trial {rep['id']} stopped at "
                  f"{datetime.fromtimestamp(rep['stopped_at']).strftime('%Y-%m-%d %H:%M')} "
                  "(runs refuse; keep or remove when ready)")
            return 0

        if args.action == "keep":
            rep = trial_mod.keep(base, getattr(args, "id", None), now=now,
                                 confirm=confirm)
            print(f"trial {rep['id']} kept — artifacts preserved under {rep['root']}; "
                  "remove now requires --really")
            return 0

        if args.action == "remove":
            rep = trial_mod.remove(base, getattr(args, "id", None), now=now,
                                   confirm=confirm,
                                   really=getattr(args, "really", False))
            print(f"trial {rep['id']} removed (files deleted): {rep['removed']}")
            return 0
    except trial_mod.TrialError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 2  # unknown action: argparse already rejects it

def cmd_recipe(args) -> int:
    """`gaceta recipe …`: safe desk-recipe sharing (recipe.py for the contract).

    export = owner side: builds the recipe from the reader's config with an
    explicit allowlist (--feed/--label; private by default), refuses anything
    that is not a public web url or that touches a privacy canary, previews
    before consent and writes only with --consent. inspect = anyone: the
    safe parser on one file, read-only (no data dir, no db, no config).
    import = recipient side: the same parser, then a brand-new isolated
    reader — never an overwrite — written only with --consent.
    """
    from pathlib import Path

    from . import recipe as recipe_mod

    if args.action == "export":
        try:
            reader = recipe_mod.validate_slug(args.reader, what="reader id")
            cfg = config.load_reader(reader)
            recipe = recipe_mod.build_recipe(
                cfg, feeds=args.feed, labels=args.label,
                title=args.title, langs=args.langs, link=args.link)
            out = (Path(args.out) if args.out else
                   recipe_mod.default_export_path(config.data_dir(), reader, recipe))
            if not str(out).lower().endswith(".json"):
                raise recipe_mod.RecipeError(
                    "--out must end in .json (a desk recipe is one JSON file)")
        except (recipe_mod.RecipeError, OSError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        print(recipe_mod.preview_recipe(recipe))
        rss_feeds = ((cfg.get("sources") or {}).get("rss") or {}).get("feeds") or []
        n_other = len(rss_feeds) - len(dict.fromkeys(args.feed))
        m_other = len(cfg.get("sections") or []) - len(dict.fromkeys(args.label))
        print(f"not shared: {n_other} other feed(s), {m_other} other label(s) "
              "— private by default; pass --feed/--label to include them")
        if not args.consent:
            print(f"preview only — nothing written; re-run with --consent to write {out}")
            return 0
        try:
            recipe_mod.write_recipe(recipe, out)
        except OSError as e:
            print(str(e), file=sys.stderr)
            return 2
        rb = recipe["recipe"]
        print(f"recipe export reader={reader} title={rb['title']!r}: "
              f"{len(rb['feeds'])} feed(s), {len(rb['labels'])} label(s)"
              f"{', public-only renderer link' if 'link' in recipe else ''} → {out}")
        return 0

    if args.action == "inspect":
        try:
            recipe = recipe_mod.load_recipe(Path(args.path))
        except recipe_mod.RecipeError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(recipe_mod.preview_recipe(recipe))
        return 0

    # import: recipient side; the recipe never touches an existing reader
    try:
        recipe = recipe_mod.load_recipe(Path(args.path))
        target, toml_text = recipe_mod.plan_import(
            recipe, config.data_dir(), args.new_reader)
    except (recipe_mod.RecipeError, OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(recipe_mod.preview_recipe(recipe))
    print(f"import --as {args.new_reader}: creates a new isolated reader at {target} "
          "(never an overwrite; independent of the sender)")
    if not args.consent:
        print("preview only — nothing written; re-run with --consent to create the reader")
        return 0
    try:
        recipe_mod.commit_import(target, toml_text)
    except (recipe_mod.RecipeError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 2
    rb = recipe["recipe"]
    print(f"recipe import --as {args.new_reader}: {len(rb['feeds'])} feed(s), "
          f"{len(rb['labels'])} label(s)"
          f"{', public-only renderer link' if 'link' in recipe else ''} → {target}")
    print(f"next: gaceta ingest rss --reader {args.new_reader} "
          "(then embed, cluster, write, edition, render)")
    return 0



def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gaceta")
    p.add_argument("--version", action="version", version=_package_version())
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

    # --- agent (public lookup): export (owner) + edition/story (read-only)
    pag = sub.add_parser("agent", help="public-only agent surface: export + receipt (owner) + read-only lookup")
    agsub = pag.add_subparsers(dest="action", required=True)
    pax = agsub.add_parser("export", help="(owner) write the public-only export from the day's public edition")
    pax.add_argument("--reader", required=True)
    pax.add_argument("--date", default=None, help="edition date YYYY-MM-DD (default today)")
    par = agsub.add_parser("receipt", help="(owner, opt-in) one person's own seen mark for one story → their baseline receipt (stdout)")
    par.add_argument("--reader", required=True)
    par.add_argument("--by", required=True,
                     help="the person whose mark it is (their memory, their consent)")
    par.add_argument("--story", required=True, help="the story id (public stories only)")
    pae = agsub.add_parser("edition", help="(agent) list the export's public stories")
    pae.add_argument("--reader", required=True, help="must match the export's bound reader")
    pae.add_argument("--path", default=None,
                     help="export file (default <data>/agent-exports/<reader>/current.json)")
    pae.add_argument("--max-age-hours", type=float, default=None,
                     help="refuse exports older than this (default 48)")
    pas = agsub.add_parser("story", help="(agent) one story with its sources")
    pas.add_argument("--reader", required=True, help="must match the export's bound reader")
    pas.add_argument("--id", required=True, help="story id from `agent edition`")
    pas.add_argument("--path", default=None,
                     help="export file (default <data>/agent-exports/<reader>/current.json)")
    pas.add_argument("--max-age-hours", type=float, default=None,
                     help="refuse exports older than this (default 48)")
    psi = agsub.add_parser("since", help="(agent) launch brief: what changed since your baseline")
    psi.add_argument("--reader", required=True, help="must match the export's bound reader")
    psi.add_argument("--baseline", default=None,
                     help="your baseline JSON file ({reader, date, versions, provenance}), "
                         "or - for stdin (default: no baseline — first launch)")
    psi.add_argument("--path", default=None,
                     help="export file (default <data>/agent-exports/<reader>/current.json)")
    psi.add_argument("--max-age-hours", type=float, default=None,
                     help="refuse exports older than this (default 48)")
    pag.set_defaults(func=cmd_agent)
    # --- end agent


    # --- trial (p3): finite manual trial of the real pipeline, synthetic only
    from . import trial as trial_mod

    ptr = sub.add_parser("trial", help="finite manual trial: preview/start/run/status/stop/keep/remove (synthetic, offline, no scheduling)")
    trsub = ptr.add_subparsers(dest="action", required=True)
    trp = trsub.add_parser("preview", help="read-only: state, and what start/run would do")
    trp.add_argument("--id", default=None, help="trial id (default: the active trial, else the newest)")
    trs = trsub.add_parser("start", help="create a trial: synthetic reader, isolated db, fake models (consent)")
    trs.add_argument("--id", default=None, help="id for the new trial (default: trial-<timestamp>)")
    trs.add_argument("--yes", action="store_true", help="consent to the printed plan")
    trr = trsub.add_parser("run", help="spend today's one run (1/day, ≤3 total) on a real pipeline pass (consent)")
    trr.add_argument("--id", default=None, help="trial id (default: the active trial)")
    trr.add_argument("--yes", action="store_true", help="consent to the printed plan")
    trr.add_argument("--max-llm-calls", type=int, default=trial_mod.DEFAULT_MAX_LLM_CALLS,
                     help=f"fake-chat call budget for write-ups (default {trial_mod.DEFAULT_MAX_LLM_CALLS}, cap {trial_mod.MAX_LLM_CALLS_CAP})")
    trr.add_argument("--max-change-calls", type=int, default=trial_mod.DEFAULT_MAX_CHANGE_CALLS,
                     help=f"fake-chat call budget for change notes (default {trial_mod.DEFAULT_MAX_CHANGE_CALLS}, cap {trial_mod.MAX_CHANGE_CALLS_CAP})")
    trr.add_argument("--recover-lock", action="store_true",
                     help="clear a stale lock left by a killed run (its slot stays spent)")
    trst = trsub.add_parser("status", help="read-only report: slots, expiry, runs, lock")
    trst.add_argument("--id", default=None, help="trial id (default: the active trial, else the newest)")
    tro = trsub.add_parser("stop", help="end the trial's runs, final (consent)")
    tro.add_argument("--id", default=None, help="trial id (default: the active trial)")
    tro.add_argument("--yes", action="store_true", help="consent to the printed plan")
    trk = trsub.add_parser("keep", help="preserve the trial's artifacts for the record (consent)")
    trk.add_argument("--id", default=None, help="trial id (default: the active trial, else the newest)")
    trk.add_argument("--yes", action="store_true", help="consent to the printed plan")
    trrm = trsub.add_parser("remove", help="delete the trial and its files (consent; stop first; --really if kept)")
    trrm.add_argument("--id", default=None, help="trial id (default: the active trial, else the newest)")
    trrm.add_argument("--yes", action="store_true", help="consent to the printed plan")
    trrm.add_argument("--really", action="store_true", help="required to remove a kept trial")
    ptr.set_defaults(func=cmd_trial)
    # --- end trial

    # --- recipe: safe desk-recipe export/inspect/import
    from .recipe import DEFAULT_TITLE as _RECIPE_DEFAULT_TITLE  # help text only

    prc = sub.add_parser("recipe",
                         help="share a desk setup: export (owner) / inspect / import a "
                              "public-only recipe of feeds and labels")
    rcsub = prc.add_subparsers(dest="action", required=True)
    rex = rcsub.add_parser("export",
                           help="(owner) write a recipe of EXPLICITLY selected public "
                                "feeds and labels — private by default")
    rex.add_argument("--reader", required=True)
    rex.add_argument("--feed", action="append", default=[], metavar="SLUG",
                     help="share this rss feed id (repeatable; nothing is shared "
                          "unless you pass it)")
    rex.add_argument("--label", action="append", default=[], metavar="ID",
                     help="share this [[sections]] label (repeatable)")
    rex.add_argument("--title", default=None,
                     help=f"recipe title (default {_RECIPE_DEFAULT_TITLE!r})")
    rex.add_argument("--langs", default=None,
                     help='write-up languages to carry, e.g. "es,en" (default: none)')
    rex.add_argument("--link", default=None,
                     help="explicit public-only renderer link: https url of YOUR "
                          "public paper (gaceta render --max-privacy public)")
    rex.add_argument("--out", default=None,
                     help="recipe path (default <data>/recipes/<reader>/<slug>.json)")
    rex.add_argument("--consent", action="store_true",
                     help="write the file; without it the command only previews")
    rin = rcsub.add_parser("inspect", help="read-only preview of a recipe file (safe parser, no writes)")
    rin.add_argument("path", help="the recipe JSON file")
    rim = rcsub.add_parser("import",
                           help="(recipient) create a NEW isolated reader from a "
                                "recipe; never overwrites")
    rim.add_argument("path", help="the recipe JSON file")
    rim.add_argument("--as", required=True, dest="new_reader",
                     help="the NEW reader id to create (must not exist)")
    rim.add_argument("--consent", action="store_true",
                     help="create the reader; without it the command only previews")
    prc.set_defaults(func=cmd_recipe)
    # --- end recipe


    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
