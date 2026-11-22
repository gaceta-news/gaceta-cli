"""Two-edition offline demo for gaceta's story memory (beta story, v0.2).

Builds a synthetic collector database, then runs the whole pipeline twice —
edition 1 on day 1, edition 2 on day 3 after one deliberately skipped day —
with deterministic fake models only: no network, no keys, no clocks. The five
editor behaviours it demonstrates are the ones the regression tests pin:

  stable story        one item, no follow-up: no rewrite, no LLM call, and the
                      same story rev in both editions, so a reader who marked
                      it seen never sees it resurface.
  approval vs proposal  an approval backed by the primary source keeps its
                      verbatim quote; an invented quote is stripped of its
                      quotation marks by the evidence gate (quotes.py); a
                      proposal stays worded as a proposal and carries lower
                      confidence than the approval.
  repeat coverage     a second outlet re-reports with nothing new: the story
                      gains a member and a rewritten card, but what_changed is
                      '' — the paper never claims a development that isn't one.
  unknown baseline    a story the reader never marked seen still gets its
                      change note for the archive; stories the reader DID see
                      before the rewrite are explained first (resurfaced first).
  skipped edition     no snapshot exists for the middle day; both snapshots are
                      immutable and the skipped day changes nothing (day-1
                      stories stay inside the edition-2 window).

EVERYTHING HERE IS SYNTHETIC. Every item, outlet, author, URL and card is
invented: ids start with "syn:", URLs live under example.com/synthetic-demo/,
outlet ids end in "-demo", authors end in "(demo)", every body carries the
stamp "[synthetic demo] contenido inventado" plus a per-scenario marker
"[demo:<scenario>]", the masthead says so, and every demo card's
why_it_matters repeats it. Nothing here is a real publication.

Usage (offline, two minutes, no install needed if run from a checkout):
    uv run python tests/fixtures/make_two_edition_demo.py ~/gaceta-demo

    # or from a plain checkout:
    python tests/fixtures/make_two_edition_demo.py ~/gaceta-demo

    # or from tests (pythonpath fixture dir):
    import make_two_edition_demo as demo
    result = demo.run_demo(pathlib.Path("/tmp/gaceta-demo"))

Outputs under <root>: collector/items.db (the synthetic upstream), data/
(gaceta.db, reader config, two immutable edition snapshots), site/ (the two
rendered papers) and demo-manifest.json (what happened, for humans and tests).
run_demo() self-checks every scenario and raises AssertionError on regression.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

# Standalone run from a checkout: make `import gaceta` work without install.
# Inert under pytest (src is already on sys.path; the guard avoids duplicates).
_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
if (_SRC / "gaceta").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from gaceta import config as gconfig, db as gdb, ingest as gingest
from gaceta.edition import build_edition, snapshot_path
from gaceta.editor.changes import note_changes
from gaceta.editor.cluster import cluster, embed_items
from gaceta.editor.writeup import write_stories
from gaceta.providers.embed import FakeEmbedder
from gaceta.providers.llm import FakeChat
from gaceta.render import html as render_html
from gaceta.sources.sqlite import SqliteAdapter

# --------------------------------------------------------------------- timeline
# Fixed dates: the demo is deterministic, no wall clock ever enters an artifact.

DAY1 = dt.date(2026, 11, 16)  # edition 1 (lunes)
DAY2 = dt.date(2026, 11, 17)  # news day — NO edition is built (skipped)
DAY3 = dt.date(2026, 11, 18)  # edition 2 (miércoles)


def _ts(day: dt.date, hour: int, minute: int = 0) -> int:
    return int(dt.datetime(day.year, day.month, day.day, hour, minute,
                           tzinfo=dt.timezone.utc).timestamp())


NOW1 = _ts(DAY1, 20, 0)       # day-1 pipeline run + edition 1 built
SEEN_AT = _ts(DAY1, 21, 0)    # Lectora marks two stories seen (edition 1 revs)
NOW2 = _ts(DAY3, 20, 0)       # day-2 pipeline run + edition 2 built

READER_ID = "demo"
READER_NAME = "Lectora de demostración"
MASTHEAD = "La Gaceta Sintética (demostración offline)"
READER_BY = "Lectora"
MAX_PRIVACY = "private"  # demo cards set why_it_matters (reader-facing voice)
# → stories are private by the privacy rule; the demo builds the owner's paper.

SYNTHETIC_STAMP = "[synthetic demo] contenido inventado"
SYNTHETIC_NOTE = (
    "Every item, outlet, author, URL and card in this demo is invented synthetic "
    "content; nothing is a real publication. The pipeline runs fully offline with "
    "deterministic fake models (no network, no keys)."
)

# The span the approval card quotes verbatim; it must appear exactly in the item.
APPROVAL_QUOTE = "antes de las primeras heladas"
# The span the approval card ALSO quotes, but which NO item supports: the
# evidence gate (gaceta.editor.quotes) must strip its quotation marks.
INVENTED_QUOTE = "un compromiso irrenunciable para la ciudad"

PRESUPUESTO_CHANGE = (
    "La audiencia pública del presupuesto quedó fijada para el 3 de diciembre "
    "en el salón del concejo, según la Gaceta del Concejo."
)

# ---------------------------------------------------------------------- corpus
# Collector-style sqlite, exactly what gaceta's read-only sqlite adapter reads.
COLLECTOR_SCHEMA = """
CREATE TABLE items(id TEXT PRIMARY KEY, source TEXT, author TEXT, title TEXT,
                   url TEXT, published INTEGER, body TEXT, verdict TEXT, note TEXT);
CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);
"""

_URL = "https://example.com/synthetic-demo/{}"


def _item(item_id, source, author, title, body, marker, published):
    body = f"{body} [demo:{marker}] {SYNTHETIC_STAMP}"
    return {
        "id": item_id, "source": source, "author": author, "title": title,
        "url": _URL.format(item_id.removeprefix("syn:")), "published": published,
        "body": body, "verdict": "noted", "note": "",
    }


# Day 1 (all synthetic; the invented town is Puerto Bruma):
#   faro        — the stable story
#   farolas     — the primary-source-backed approval
#   presupuesto — the proposal (the reader never marks it seen)
DAY1_ITEMS = (
    _item(
        "syn:faro-0001", "gaceta-del-puerto-demo", "Redacción Faro (demo)",
        "El faro de Punta Bruma reabre tras reparar la barandilla",
        "El faro de Punta Bruma reabrió al público tras tres semanas de "
        "reparaciones. La barandilla exterior quedó firme, la linterna giró sin "
        "fallos toda la noche y las visitas guiadas retoman el domingo.",
        "estable", _ts(DAY1, 9, 0),
    ),
    _item(
        "syn:farolas-0001", "gaceta-del-concejo-demo", "Mesa del Concejo (demo)",
        "El concejo aprueba doce farolas para la pasarela este",
        "El concejo de Puerto Bruma aprobó la instalación de doce farolas "
        "solares en la pasarela este. La Mesa del Concejo confirmó que la obra "
        "se instalará \"antes de las primeras heladas\" y que el arranque será "
        "tras el próximo mercado de domingo. El gasto sale del remanente del año.",
        "aprobacion", _ts(DAY1, 12, 0),
    ),
    _item(
        "syn:presupuesto-0001", "gaceta-del-concejo-demo", "Mesa del Concejo (demo)",
        "Audiencia pública del presupuesto: propuesta para diciembre",
        "La Mesa del Concejo propuso una audiencia pública sobre el borrador del "
        "presupuesto de 2027. La fecha aún no está fijada y el borrador puede "
        "recibir comentarios de los vecinos antes de cualquier votación.",
        "propuesta", _ts(DAY1, 15, 0),
    ),
)

# Day 2 (published on the skipped day; edition 2 covers it from day 3):
#   farolas-0002      — repeat coverage: second outlet, nothing new
#   presupuesto-0002  — the proposal moves: hearing date fixed (a real change)
#   mercado-0001      — a brand new story, so edition 2 has fresh news
DAY2_ITEMS = (
    _item(
        "syn:farolas-0002", "cronica-del-puerto-demo", "Crónica Local (demo)",
        "Las farolas de la pasarela este siguen en calendario",
        "La Crónica del Puerto repasó el plan de doce farolas solares para la "
        "pasarela este que el concejo aprobó la semana pasada. El calendario no "
        "cambia: las farolas se instalan tras el próximo mercado de domingo. No "
        "hay datos nuevos de costos ni de plazos.",
        "repeticion", _ts(DAY2, 11, 0),
    ),
    _item(
        "syn:presupuesto-0002", "gaceta-del-concejo-demo", "Mesa del Concejo (demo)",
        "La audiencia del presupuesto se fijó para el 3 de diciembre",
        "La Mesa del Concejo fijó la fecha de la audiencia pública sobre el "
        "borrador del presupuesto de 2027: será el 3 de diciembre en el salón "
        "del concejo. El borrador ya está en el mostrador de la biblioteca y "
        "los vecinos pueden dejar comentarios antes de la votación.",
        "propuesta-avance", _ts(DAY2, 15, 0),
    ),
    _item(
        "syn:mercado-0001", "gaceta-del-puerto-demo", "Redacción Mercado (demo)",
        "El mercado de niebla se muda al salón viejo",
        "Por la lluvia prevista, el mercado de niebla de Puerto Bruma se muda al "
        "salón viejo este fin de semana. La entrada sigue libre y el puesto de "
        "pasteles no cambia de lugar.",
        "nueva", _ts(DAY2, 17, 0),
    ),
)


def _write_items(conn: sqlite3.Connection, items) -> None:
    conn.executemany(
        "INSERT INTO items (id, source, author, title, url, published, body, "
        "verdict, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(i["id"], i["source"], i["author"], i["title"], i["url"],
          i["published"], i["body"], i["verdict"], i["note"]) for i in items],
    )


def build_collector_db(collector_dir: pathlib.Path) -> pathlib.Path:
    """Day-1 world: a synthetic collector db with the three day-1 items."""
    collector_dir = pathlib.Path(collector_dir)
    collector_dir.mkdir(parents=True, exist_ok=True)
    db_path = collector_dir / "items.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(COLLECTOR_SCHEMA)
        _write_items(conn, DAY1_ITEMS)
        conn.executemany(
            "INSERT INTO kv (k, v) VALUES (?, ?)",
            [("schema_version", "synthetic-demo-1"),
             ("synthetic", "every row in this db is invented demo content")],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def append_day2_items(collector_dir: pathlib.Path) -> None:
    """The world moves: day-2 items land in the same collector db."""
    conn = sqlite3.connect(pathlib.Path(collector_dir) / "items.db")
    try:
        _write_items(conn, DAY2_ITEMS)
        conn.commit()
    finally:
        conn.close()


def write_reader_toml(data_dir: pathlib.Path, collector_dir: pathlib.Path) -> pathlib.Path:
    """The demo reader: Spanish paper, fake models, the synthetic sqlite source."""
    reader_dir = pathlib.Path(data_dir) / "readers" / READER_ID
    reader_dir.mkdir(parents=True, exist_ok=True)
    toml = (
        "# Demo reader for the offline two-edition demonstration.\n"
        "# EVERYTHING THIS PAPER PRINTS IS INVENTED SYNTHETIC CONTENT.\n"
        f"id = {READER_ID!r}\n"
        f"name = {READER_NAME!r}\n"
        'langs = "es"\n'
        f"masthead = {MASTHEAD!r}\n"
        "\n[privacy]\n"
        "pinned = []      # demo canaries off: all demo items are public source text\n"
        "household = []\n"
        "\n[sources.sqlite]\n"
        f"path = {str(collector_dir)!r}   # the synthetic collector db (read-only)\n"
        "\n[embed]\n"
        'provider = "fake"   # deterministic, offline\n'
        "\n[llm.editor]\n"
        'provider = "fake"   # the driver passes its own scripted demo editor\n'
        "\n[cluster]\n"
        "threshold = 0.70\n"
        "window_days = 21\n"
    )
    path = reader_dir / "reader.toml"
    path.write_text(toml, encoding="utf-8")
    return path


# ------------------------------------------------------------- scripted editor
# Deterministic stand-in for the reader's editor model. It routes each prompt
# by the synthetic scenario marker in the item text (markers are part of the
# corpus, so routing is content-based, never positional) and answers with the
# pre-written card for that scenario. Every card states it is synthetic.

_DEMO_WHY = ("Tarjeta sintética de demostración escrita por un modelo falso: "
             "conecta un modelo real para resúmenes de verdad.")

_CARDS = {
    "estable": {
        "title": "El faro de Punta Bruma vuelve a abrir",
        "summary": "El faro de Punta Bruma reabrió tras tres semanas de "
                   "reparaciones de barandilla. La linterna giró sin fallos "
                   "toda la noche y las visitas guiadas retoman el domingo.",
        "why_it_matters": _DEMO_WHY, "confidence": 0.8,
    },
    "aprobacion": {
        "title": "Doce farolas aprobadas para la pasarela este",
        "summary": "El concejo de Puerto Bruma aprobó doce farolas solares "
                   "para la pasarela este. La Mesa del Concejo fijó la "
                   f"instalación \u201c{APPROVAL_QUOTE}\u201d, con arranque "
                   "tras el próximo mercado de domingo; el gasto sale del "
                   f"remanente del año, \u201c{INVENTED_QUOTE}\u201d.",
        "why_it_matters": _DEMO_WHY, "confidence": 0.9,
    },
    "propuesta": {
        "title": "El presupuesto de 2027 tendrá audiencia pública",
        "summary": "La Mesa del Concejo propuso una audiencia pública sobre el "
                   "borrador del presupuesto de 2027. La fecha aún no está "
                   "fijada; el borrador puede recibir comentarios antes de "
                   "cualquier votación.",
        "why_it_matters": _DEMO_WHY, "confidence": 0.55,
    },
    "repeticion": {
        "title": "Las farolas de la pasarela este siguen en calendario",
        "summary": "El concejo de Puerto Bruma aprobó doce farolas solares para "
                   "la pasarela este. La Crónica del Puerto confirma que el "
                   "calendario no cambia y que no hay datos nuevos de costos "
                   "ni de plazos.",
        "why_it_matters": _DEMO_WHY, "confidence": 0.85,
    },
    "propuesta-avance": {
        "title": "La audiencia del presupuesto se fijó para el 3 de diciembre",
        "summary": "La audiencia pública del presupuesto se fijó para el 3 de "
                   "diciembre en el salón del concejo, según la Mesa del "
                   "Concejo. El borrador ya está en el mostrador de la "
                   "biblioteca y sigue abierto a comentarios hasta la votación.",
        "why_it_matters": _DEMO_WHY, "confidence": 0.7,
    },
    "nueva": {
        "title": "El mercado de niebla se muda al salón viejo",
        "summary": "Por la lluvia prevista, el mercado de niebla de Puerto Bruma "
                   "se muda al salón viejo este fin de semana. La entrada sigue "
                   "libre y el puesto de pasteles no cambia de lugar.",
        "why_it_matters": _DEMO_WHY, "confidence": 0.75,
    },
}

_CHANGES = {
    # repeat coverage: the card was rewritten, but nothing material changed
    "repeticion": '{"what_changed": null}',
    # the proposal moved: a real development, supported by the new item
    "propuesta-avance": json.dumps({"what_changed": PRESUPUESTO_CHANGE},
                                  ensure_ascii=False),
}

# Day-2 markers win over day-1 ones when both items are in one prompt.
_MARKER_PRIORITY = ("repeticion", "propuesta-avance", "aprobacion",
                    "propuesta", "estable", "nueva")


class DemoEditor(FakeChat):
    """Scripted editor for the demo. Records every prompt it sees (FakeChat);
    prompts with no scenario marker get a generic synthetic card and are
    reported via .unmatched so the driver can fail loudly."""

    def __init__(self):
        super().__init__(name="demo:editor-scripted", allow_private=True)
        self.unmatched: list[str] = []

    def _marker(self, prompt: str) -> str | None:
        for marker in _MARKER_PRIORITY:
            if f"[demo:{marker}]" in prompt:
                return marker
        return None

    def complete(self, messages, *, max_tokens: int = 300, temperature: float = 0.0):
        prompt = "\n".join(m["content"] for m in messages)
        self.prompts.append(prompt)
        usage = {"prompt_tokens": len(prompt.split()), "completion_tokens": 3,
                 "served_model": self.name}
        marker = self._marker(prompt)
        if "PREVIOUS CARD" in prompt and "CURRENT CARD" in prompt:
            if marker not in _CHANGES:
                self.unmatched.append(prompt)
                return '{"what_changed": null}', usage
            return _CHANGES[marker], usage
        card = _CARDS.get(marker) if marker else None
        if card is None:
            self.unmatched.append(prompt)
            card = self._generic_card(prompt)
        return json.dumps(card, ensure_ascii=False), usage

    @staticmethod
    def _generic_card(prompt: str) -> dict:
        title = next((line[6:].strip() for line in prompt.splitlines()
                      if line.startswith("TITLE:")), "Historia sintética")
        return {"title": title[:120],
                "summary": f"{title}. Contenido sintético de demostración.",
                "why_it_matters": _DEMO_WHY, "confidence": 0.5}


# ------------------------------------------------------------------- db helpers

def _pin_ingested_at(conn: sqlite3.Connection, ts: int,
                     only_new: set[str] | None = None) -> None:
    """ingest stamps ingested_at with the wall clock; pin it to the demo
    timeline (all of it, or only rows that did not exist before this pass).
    ingested_at is what the changes pass uses to tell which items joined
    between two cards, so the demo keeps it deterministic."""
    if only_new is None:
        conn.execute("UPDATE items SET ingested_at = ?", (ts,))
    elif only_new:
        conn.execute(f"UPDATE items SET ingested_at = ? WHERE id IN "
                     f"({','.join('?' * len(only_new))})", (ts, *only_new))
    conn.commit()


def _story_of(conn: sqlite3.Connection, item_id: str) -> str:
    row = conn.execute(
        "SELECT si.story_id FROM story_items si JOIN items i ON i.id = si.item_id "
        "WHERE i.id = ?", (item_id,)).fetchone()
    assert row is not None, f"item {item_id} never clustered"
    return row["story_id"]


def _versions(conn: sqlite3.Connection, story_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT version, title, summary, why_it_matters, confidence, "
        "what_changed, members, written_at "
        "FROM story_versions WHERE story_id = ? ORDER BY version", (story_id,))]


def _member_ids(conn: sqlite3.Connection, story_id: str) -> list[str]:
    return sorted(r["item_id"] for r in conn.execute(
        "SELECT item_id FROM story_items WHERE story_id = ?", (story_id,)))


def _rel(path: pathlib.Path, data_dir: pathlib.Path) -> str:
    return pathlib.Path(path).relative_to(data_dir).as_posix()


# ----------------------------------------------------------------------- driver

def run_demo(root: pathlib.Path) -> dict:
    """Drive the two-edition demo. Returns {'manifest', 'editor', 'passes'}
    (manifest is JSON-safe; editor/passes carry prompts for tests). Every
    scenario is self-checked here: AssertionError means a pipeline regression."""
    root = pathlib.Path(root)
    collector = root / "collector"
    data = root / "data"
    build_collector_db(collector)
    write_reader_toml(data, collector)
    cfg = gconfig.load_reader(READER_ID, data)
    conn = gdb.connect(data / "gaceta.db")
    gdb.init(conn)
    adapter = SqliteAdapter({"path": str(collector)})
    editor = DemoEditor()

    # ---- day 1: ingest → embed → cluster → write → edition 1 (D1)
    ing1 = gingest.ingest(conn, adapter, cfg)
    _pin_ingested_at(conn, NOW1)
    emb1 = embed_items(conn, READER_ID, FakeEmbedder())
    cl1 = cluster(conn, READER_ID, cfg)
    w1 = write_stories(conn, READER_ID, cfg, chat=editor, now=NOW1)
    write1_prompts = editor.prompts[:]
    ch1 = note_changes(conn, READER_ID, cfg, chat=editor, now=NOW1)
    ed1 = build_edition(conn, READER_ID, cfg, date_str=DAY1.isoformat(),
                        max_privacy=MAX_PRIVACY, data_dir=data, now=NOW1)

    faro_sid = _story_of(conn, "syn:faro-0001")
    farolas_sid = _story_of(conn, "syn:farolas-0001")
    presupuesto_sid = _story_of(conn, "syn:presupuesto-0001")

    # The reader marks two stories seen at their edition-1 revs. The proposal
    # is never marked: for it, the reader's baseline is unknown.
    for sid in (faro_sid, farolas_sid):
        conn.execute(
            "INSERT INTO seen (reader_id, by_name, story_id, rev, at) "
            "VALUES (?, ?, ?, ?, ?)",
            (READER_ID, READER_BY, sid, ed1["stories"][sid]["rev"], SEEN_AT))
    conn.commit()

    # ---- the skipped day: no pipeline run, no edition for DAY2.

    # ---- day 3 (covering day 2's news): ingest → embed → cluster → write → edition 2
    append_day2_items(collector)
    known = {r["id"] for r in conn.execute("SELECT id FROM items")}
    ing2 = gingest.ingest(conn, adapter, cfg)  # idempotent: only the day-2 rows
    _pin_ingested_at(conn, NOW2, only_new={r["id"] for r in conn.execute(
        "SELECT id FROM items")} - known)
    emb2 = embed_items(conn, READER_ID, FakeEmbedder())
    cl2 = cluster(conn, READER_ID, cfg)
    w2 = write_stories(conn, READER_ID, cfg, chat=editor, now=NOW2)
    write2_prompts = editor.prompts[len(write1_prompts):]
    # (the day-1 note_changes pass made no calls; write2 follows it directly)
    ch2 = note_changes(conn, READER_ID, cfg, chat=editor, now=NOW2)
    changes2_prompts = editor.prompts[len(write1_prompts) + len(write2_prompts):]
    ed2 = build_edition(conn, READER_ID, cfg, date_str=DAY3.isoformat(),
                        max_privacy=MAX_PRIVACY, data_dir=data, now=NOW2)

    mercado_sid = _story_of(conn, "syn:mercado-0001")

    # ---------------------------------------------------------- self-checks
    assert ing1["new"] == len(DAY1_ITEMS) and ing2["new"] == len(DAY2_ITEMS), \
        "ingest must be incremental: day-1 rows are re-read, not duplicated"
    assert cl1["n_new_stories"] == len(DAY1_ITEMS) and cl1["n_joined_stories"] == 0
    assert cl2["n_joined_stories"] == 2 and cl2["n_new_stories"] == 1, \
        "repeat coverage and the proposal move join their stories; mercado is new"
    assert _member_ids(conn, farolas_sid) == ["syn:farolas-0001", "syn:farolas-0002"]
    assert _member_ids(conn, presupuesto_sid) == ["syn:presupuesto-0001",
                                                  "syn:presupuesto-0002"]
    assert _member_ids(conn, faro_sid) == ["syn:faro-0001"]
    assert editor.unmatched == [], "a demo prompt hit no scripted scenario"

    # stable story: written once, never again, same rev in both editions
    assert w1["written"] == 3 and w1["llm_calls"] == 3 and w1["quotes_unquoted"] == 1
    assert ch1["candidates"] == 0, "day 1 has no version-2 cards to explain"
    assert len(_versions(conn, faro_sid)) == 1, "the stable story must not be rewritten"
    assert not any("[demo:estable]" in p for p in write2_prompts), \
        "the stable story must cost no day-2 LLM call"
    assert w2["written"] == 3 and w2["llm_calls"] == 3
    assert ed1["stories"][faro_sid]["rev"] == ed2["stories"][faro_sid]["rev"], \
        "an unchanged story must keep its rev (it never resurfaces)"
    assert ed1["stories"][farolas_sid]["rev"] != ed2["stories"][farolas_sid]["rev"], \
        "repeat coverage changes members, so the rev moves (it resurfaces)"

    # approval vs proposal: evidence gate + wording + confidence
    v1_farolas = _versions(conn, farolas_sid)[0]["summary"]
    assert f"\u201c{APPROVAL_QUOTE}\u201d" in v1_farolas, \
        "the primary-source quote must survive verbatim"
    assert f"\u201c{INVENTED_QUOTE}\u201d" not in v1_farolas and \
        INVENTED_QUOTE in v1_farolas, \
        "the invented quote must survive only as unquoted paraphrase"
    v1_presu = _versions(conn, presupuesto_sid)[0]["summary"]
    assert "propuso" in v1_presu and "aprob" not in v1_presu.casefold(), \
        "the proposal must stay a proposal"
    # confidence lives per version AND on stories (the latest card); report the
    # version-1 (day-1) confidence for the approval-vs-proposal contrast

    def v1_conf(sid_: str) -> float:
        return _versions(conn, sid_)[0]["confidence"]

    assert v1_conf(farolas_sid) > v1_conf(presupuesto_sid), \
        "the primary-source approval is scripted more confident than the proposal"

    # repeat coverage: new member, new version, but no change note
    farolas_v = _versions(conn, farolas_sid)
    assert [v["version"] for v in farolas_v] == [1, 2]
    assert farolas_v[1]["members"] == 2
    assert farolas_v[1]["what_changed"] == "", \
        "repeat coverage with nothing new must record '' (nothing material)"

    # unknown baseline: no seen row, change note still archived, resurfaced first
    presu_v = _versions(conn, presupuesto_sid)
    assert [v["version"] for v in presu_v] == [1, 2]
    assert presu_v[1]["what_changed"] == PRESUPUESTO_CHANGE
    seen_rows = {r["story_id"]: dict(r) for r in conn.execute(
        "SELECT story_id, rev, at FROM seen WHERE by_name = ?", (READER_BY,))}
    assert set(seen_rows) == {faro_sid, farolas_sid}, \
        "the proposal is never marked seen: its baseline is unknown"
    ch_markers = [editor._marker(p) for p in changes2_prompts]
    assert ch_markers == ["repeticion", "propuesta-avance"], \
        "changes pass explains the story the reader saw first (resurfaced first)"
    assert ch2["candidates"] == 2 and ch2["noted"] == 1 and ch2["unchanged"] == 1

    # skipped edition: two immutable snapshots, nothing for the middle day
    p1 = snapshot_path(data, READER_ID, DAY1.isoformat(), MAX_PRIVACY)
    p2 = snapshot_path(data, READER_ID, DAY3.isoformat(), MAX_PRIVACY)
    assert p1.exists() and p2.exists()
    assert not snapshot_path(data, READER_ID, DAY2.isoformat(), MAX_PRIVACY).exists()
    ed_dates = {r["date"] for r in conn.execute(
        "SELECT date FROM editions WHERE reader_id = ?", (READER_ID,))}
    assert ed_dates == {DAY1.isoformat(), DAY3.isoformat()}, \
        "the middle day must have no edition row (it was skipped)"
    assert ed1["folio"]["code"] != ed2["folio"]["code"]
    assert faro_sid in ed2["stories"], \
        "the skipped day must not drop day-1 stories from edition 2"

    # ------------------------------------------------------------- artifacts
    site = root / "site"
    site.mkdir(parents=True, exist_ok=True)
    html_paths = {}
    for day, ed in ((DAY1, ed1), (DAY3, ed2)):
        path = site / f"edicion-{day.isoformat()}.html"
        path.write_text(render_html.render(ed, MAX_PRIVACY), encoding="utf-8")
        html_paths[day.isoformat()] = path

    def _ed_facts(ed):
        return {
            "date": ed["date"], "built_at": ed["built_at"],
            "folio": ed["folio"], "portada": ed["portada"],
            "n_stories": len(ed["stories"]),
            "revs": {sid: entry["rev"] for sid, entry in ed["stories"].items()},
        }

    manifest = {
        "synthetic": True,
        "note": SYNTHETIC_NOTE,
        "reader": {"id": READER_ID, "name": READER_NAME, "langs": "es",
                   "masthead": MASTHEAD, "by": READER_BY,
                   "max_privacy": MAX_PRIVACY},
        "timeline": {"edition1": DAY1.isoformat(), "skipped": DAY2.isoformat(),
                     "edition2": DAY3.isoformat(), "now1": NOW1,
                     "seen_at": SEEN_AT, "now2": NOW2},
        "artifacts": {
            "collector_db": "collector/items.db",
            "gaceta_db": "data/gaceta.db",
            "manifest": "demo-manifest.json",
            "editions": {DAY1.isoformat(): _rel(p1, root),
                         DAY3.isoformat(): _rel(p2, root)},
            "html": {DAY1.isoformat(): _rel(html_paths[DAY1.isoformat()], root),
                     DAY3.isoformat(): _rel(html_paths[DAY3.isoformat()], root)},
        },
        "editions": {DAY1.isoformat(): _ed_facts(ed1), DAY3.isoformat(): _ed_facts(ed2)},
        "stories": {
            "stable": {"story": faro_sid, "item": "syn:faro-0001",
                       "versions": len(_versions(conn, faro_sid)),
                       "rev_edition1": ed1["stories"][faro_sid]["rev"],
                       "rev_edition2": ed2["stories"][faro_sid]["rev"],
                       "seen_rev": seen_rows[faro_sid]["rev"]},
            "approval": {"story": farolas_sid, "item": "syn:farolas-0001",
                         "quote_kept": APPROVAL_QUOTE,
                         "quote_unquoted": INVENTED_QUOTE,
                         "confidence_version1": v1_conf(farolas_sid)},
            "proposal": {"story": presupuesto_sid, "item": "syn:presupuesto-0001",
                          "confidence_version1": v1_conf(presupuesto_sid),
                          "seen": False},
            "repeat_coverage": {"story": farolas_sid, "item": "syn:farolas-0002",
                                "what_changed": ""},
            "unknown_baseline": {"story": presupuesto_sid,
                                 "item": "syn:presupuesto-0002",
                                 "what_changed": PRESUPUESTO_CHANGE},
            "fresh_day2": {"story": mercado_sid, "item": "syn:mercado-0001"},
        },
        "cards": {sid: _versions(conn, sid) for sid in
                  (faro_sid, farolas_sid, presupuesto_sid, mercado_sid)},
        "runs": {"ingest1": ing1, "ingest2": ing2,
                 "embed1": emb1, "embed2": emb2,
                 "cluster1": cl1, "cluster2": cl2,
                 "write1": w1, "write2": w2,
                 "changes1": ch1, "changes2": ch2},
    }
    (root / "demo-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    conn.close()
    return {"manifest": manifest, "editor": editor,
            "passes": {"write1": write1_prompts, "changes1": [],
                        "write2": write2_prompts,
                        "changes2": changes2_prompts}}


# ------------------------------------------------------------------- standalone

def _main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Offline two-edition gaceta demo. All content is synthetic.")
    ap.add_argument("root", nargs="?", default="gaceta-demo",
                    help="target directory (default ./gaceta-demo)")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root)
    result = run_demo(root)
    m = result["manifest"]
    print(f"gaceta two-edition offline demo → {root.resolve()}")
    print(f"  synthetic: {m['note']}")
    for day in (m["timeline"]["edition1"], m["timeline"]["edition2"]):
        ed = m["editions"][day]
        print(f"  edition {day}: folio {ed['folio']['code']}, "
              f"{ed['n_stories']} historias → {m['artifacts']['editions'][day]}")
    print(f"  skipped day (no edition): {m['timeline']['skipped']}")
    st = m["stories"]
    print(f"  stable story: {st['stable']['versions']} version, "
          f"rev unchanged {st['stable']['rev_edition1'] == st['stable']['rev_edition2']}")
    apv, prop = st["approval"], st["proposal"]
    print(f"  approval: quote kept {apv['quote_kept']!r}, invented unquoted "
          f"{apv['quote_unquoted']!r}, confidence {apv['confidence_version1']}")
    print(f"  proposal: stays a proposal, confidence "
          f"{prop['confidence_version1']}, seen by reader: {prop['seen']}")
    print(f"  repeat coverage: what_changed = "
          f"{st['repeat_coverage']['what_changed']!r} (nothing material)")
    print(f"  unknown baseline: change archived for the never-seen story")
    print("  paper: " + ", ".join(m["artifacts"]["html"].values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
