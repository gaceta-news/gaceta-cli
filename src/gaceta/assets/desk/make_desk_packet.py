"""Build the synthetic desk packet: an offline, bilingual (EN/ES), three-day
sample fixture for gaceta's story pipeline.

Every outlet, person, company, regulator, currency, place and event in the
packet is invented, in the house style of the other fixtures (``*.example``
hosts, fictional jurisdictions). No site is ever fetched: the packet ships as
local files and every link on the rendered page resolves inside this directory.

Two output trees, written by the same deterministic pass:

- the blind packet, redistributable with the package:
  ``packet.json``, ``feed.rss``, ``sources/*.html``, ``index.html``
  (all in this directory — no labels, no expected answers of any kind);
- the internal answer key, isolated under ``tests/fixtures/desk/``
  (``oracle.json`` + ``golden.json``), which must never be copied into this
  directory or shipped beside the blind files.

Determinism: everything below is fixed content — no clock, no randomness,
no network — so regenerating reproduces every file byte for byte.

Usage:
    python src/gaceta/assets/desk/make_desk_packet.py   # refresh in place

    import importlib.util ...  # tests load this file and call build()
"""

from __future__ import annotations

import json
import pathlib
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import escape

PACKET_ID = "desk-2026-11-16-18-en-es-v1"
TZ_LABEL = "UTC-06:00"
DAYS = ("2026-11-16", "2026-11-17", "2026-11-18")

DAY_META = {
    "2026-11-16": {"rfc": "Mon", "en": "Monday", "es": "lunes"},
    "2026-11-17": {"rfc": "Tue", "en": "Tuesday", "es": "martes"},
    "2026-11-18": {"rfc": "Wed", "en": "Wednesday", "es": "miércoles"},
}

# Words that exist only on the answer-key side. The build refuses to emit any
# of them into the blind tree, and the tests re-check the shipped files.
ORACLE_ONLY_TOKENS = (
    "oracle", "golden", "answer", "rubric", "distractor", "taxonomy",
    "classification", "canonical", "expected_facts", "repeat_docs",
    "per_document", "event_key", "material_decision", "similar_unrelated",
)

# The eight documents, in published order. Derivative items (news/analysis)
# carry an "original": the gazette record they cite, in its source language.
DOCUMENTS = [
    {
        "id": "gaz-4127-arav-117",
        "date": "2026-11-16",
        "published": "2026-11-16T06:00:00-06:00",
        "language": "es",
        "kind": "gazette",
        "outlet": "Gaceta Oficial de Valdora",
        "url": "https://gazetavaldora.example/edicion-4127/resolucion-117-2026",
        "title": "Resolución No. 117-2026 de la Autoridad Reguladora de Aguas de Valdora",
        "subtitle": "Responsabilidad por el deterioro del acuífero de la Serranía del "
                    "Toldo: plan de remediación en noventa días y multa de V$ 4.200.000.",
        "body": [
            "Vado Prieto, lunes 16 de noviembre de 2026. La Autoridad Reguladora de "
            "Aguas de Valdora (ARAV) resuelve en el expediente ARAV-EX-2025-0418 lo siguiente:",
            "PRIMERO. Declarar a Compañía Minera Cerro Alto, S.A. responsable del "
            "deterioro del acuífero de la Serranía del Toldo, sector Vega Norte, por las "
            "concentraciones de arsénico y sulfatos registradas en los pozos de monitoreo "
            "desde marzo de 2026.",
            "SEGUNDO. Ordenar a Compañía Minera Cerro Alto, S.A. la presentación de un "
            "plan de remediación integral del acuífero dentro de los noventa (90) días "
            "calendario siguientes a esta publicación.",
            "TERCERO. Ordenar obras inmediatas de contención en el dique de relaves de "
            "la unidad minera Cerro Alto, bajo supervisión de la ARAV, con informes "
            "mensuales de avance.",
            "CUARTO. Imponer a la responsable una multa de V$ 4.200.000, pagadera dentro "
            "de los treinta (30) días hábiles.",
            "La resolución es de cumplimiento inmediato; contra ella procede recurso de "
            "reconsideración ante el Directorio de la ARAV dentro de los quince (15) días "
            "hábiles. Publíquese en la Gaceta Oficial de Valdora, Edición No. 4127.",
        ],
        "gazette_ref": "Año CII, Edición No. 4127, lunes 16 de noviembre de 2026, página 3",
    },
    {
        "id": "anuv-0847",
        "date": "2026-11-16",
        "published": "2026-11-16T18:05:00-06:00",
        "language": "es",
        "kind": "news",
        "outlet": "Agencia de Noticias Unidas de Valdora",
        "url": "https://anuv.example/despachos/0847",
        "title": "La ARAV declara responsable a Compañía Minera Cerro Alto por el "
                 "acuífero de la Serranía del Toldo",
        "body": [
            "La Autoridad Reguladora de Aguas de Valdora (ARAV) declaró este lunes a "
            "Compañía Minera Cerro Alto, S.A. responsable del deterioro del acuífero de "
            "la Serranía del Toldo, sector Vega Norte.",
            "La Resolución No. 117-2026, publicada en la Gaceta Oficial de Valdora, "
            "impone una multa de V$ 4.200.000, exige un plan de remediación integral del "
            "acuífero dentro de los noventa (90) días calendario y ordena obras inmediatas "
            "de contención en el dique de relaves de la unidad minera Cerro Alto, con "
            "informes mensuales de avance.",
            "La resolución es de cumplimiento inmediato; procede recurso de "
            "reconsideración ante el Directorio de la ARAV dentro de los quince (15) días "
            "hábiles. La empresa no emitió comentarios esta tarde.",
        ],
        "original": {"id": "gaz-4127-arav-117", "language": "es",
                     "url": "https://gazetavaldora.example/edicion-4127/resolucion-117-2026"},
    },
    {
        "id": "vmp-0411",
        "date": "2026-11-16",
        "published": "2026-11-16T21:10:00-06:00",
        "language": "en",
        "kind": "news",
        "outlet": "Valdora Morning Post",
        "url": "https://valdoramorningpost.example/articles/0411",
        "title": "Valdora water regulator finds Cerro Alto liable for aquifer damage, "
                 "orders 90-day cleanup plan",
        "body": [
            "Valdora's water authority on Monday found Compañía Minera Cerro Alto, S.A. "
            "liable for degrading the Serranía del Toldo aquifer in the Vega Norte sector.",
            "Resolution 117-2026, published in the official gazette, fines the company "
            "V$ 4.2 million and orders a comprehensive remediation plan for the aquifer "
            "within 90 days, plus immediate containment works at the Cerro Alto tailings "
            "dam under the regulator's supervision, with monthly progress reporting.",
            "The resolution takes effect immediately; Compañía Minera Cerro Alto may seek "
            "reconsideration before the authority's board within 15 business days.",
        ],
        "original": {"id": "gaz-4127-arav-117", "language": "es",
                     "url": "https://gazetavaldora.example/edicion-4127/resolucion-117-2026"},
    },
    {
        "id": "her-1129",
        "date": "2026-11-17",
        "published": "2026-11-17T07:30:00-06:00",
        "language": "es",
        "kind": "news",
        "outlet": "El Heraldo de Valdora",
        "url": "https://elheraldo.example/nacional/1129",
        "title": "Noventa días para el plan de remediación del acuífero de la "
                 "Serranía del Toldo",
        "body": [
            "La Resolución No. 117-2026 de la ARAV, publicada este lunes en la Gaceta "
            "Oficial de Valdora, declara a Compañía Minera Cerro Alto, S.A. responsable "
            "del deterioro del acuífero de la Serranía del Toldo, sector Vega Norte.",
            "El instrumento impone una multa de V$ 4.200.000, exige el plan de "
            "remediación integral del acuífero dentro de los noventa (90) días calendario "
            "y ordena obras inmediatas de contención en el dique de relaves de la unidad "
            "minera Cerro Alto, bajo supervisión de la ARAV.",
            "«Responsabilidad, remediación y plazo en un solo instrumento», resumió "
            "Helena Marsal, directora ejecutiva de la ARAV. La resolución es de "
            "cumplimiento inmediato y admite recurso de reconsideración dentro de los "
            "quince (15) días hábiles.",
        ],
        "original": {"id": "gaz-4127-arav-117", "language": "es",
                     "url": "https://gazetavaldora.example/edicion-4127/resolucion-117-2026"},
    },
]
DOCUMENTS += [
    {
        "id": "pul-0563",
        "date": "2026-11-17",
        "published": "2026-11-17T09:15:00-06:00",
        "language": "es",
        "kind": "news",
        "outlet": "El Pulso Económico",
        "url": "https://elpulso.example/empresas/0563",
        "title": "Cerro Alto asume multa de V$ 4.200.000; la remediación del acuífero "
                 "pesa más que la sanción",
        "body": [
            "La ARAV publicó este lunes la Resolución No. 117-2026 contra Compañía "
            "Minera Cerro Alto, S.A.: multa de V$ 4.200.000, plan de remediación "
            "integral del acuífero de la Serranía del Toldo dentro de los noventa (90) "
            "días calendario y obras inmediatas de contención en el dique de relaves.",
            "Desde Ferrer & Linares estiman que el costo del plan de remediación "
            "superará la multa. La resolución es de cumplimiento inmediato; procede "
            "recurso de reconsideración ante el Directorio de la ARAV dentro de los "
            "quince (15) días hábiles.",
        ],
        "original": {"id": "gaz-4127-arav-117", "language": "es",
                     "url": "https://gazetavaldora.example/edicion-4127/resolucion-117-2026"},
    },
    {
        "id": "ledger-2026-111",
        "date": "2026-11-17",
        "published": "2026-11-17T14:00:00-06:00",
        "language": "en",
        "kind": "analysis",
        "outlet": "The Enforcement Ledger",
        "url": "https://enforcementledger.example/notes/2026-111",
        "title": "Resolution 117-2026: immediate effect, a 90-day clock and a "
                 "4.2 million fine",
        "body": [
            "Monday's gazette carried Resolution 117-2026 from the Autoridad Reguladora "
            "de Aguas de Valdora (ARAV), finding Compañía Minera Cerro Alto, S.A. liable "
            "for the degradation of the Serranía del Toldo aquifer.",
            "Three features make the instrument consequential: it takes effect "
            "immediately upon publication, it starts a 90-day clock for the comprehensive "
            "remediation plan ordered for the aquifer, and it imposes a V$ 4.2 million "
            "fine on the company.",
            "Containment works at the Cerro Alto tailings dam proceed under "
            "supervision, with monthly progress reporting until the remediation plan "
            "is approved. The company may seek reconsideration before the "
            "authority's board within 15 business days.",
        ],
        "original": {"id": "gaz-4127-arav-117", "language": "es",
                     "url": "https://gazetavaldora.example/edicion-4127/resolucion-117-2026"},
    },
    {
        "id": "gaz-4129-arev-098",
        "date": "2026-11-18",
        "published": "2026-11-18T06:00:00-06:00",
        "language": "es",
        "kind": "gazette",
        "outlet": "Gaceta Oficial de Valdora",
        "url": "https://gazetavaldora.example/edicion-4129/resolucion-098-2026",
        "title": "Resolución No. 098-2026 de la Autoridad Reguladora de Energía de "
                 "Valdora",
        "subtitle": "Sanción a Generadora Marbrisa, S.A. por la pluma de diésel en el "
                    "polígono portuario de Puerto Gris; saneamiento en sesenta días.",
        "body": [
            "Vado Prieto, miércoles 18 de noviembre de 2026. En uso de sus "
            "atribuciones, el Directorio de la Autoridad Reguladora de Energía de "
            "Valdora (AREV), con fundamento en el expediente AREV-EX-2026-0107, "
            "adopta la siguiente decisión:",
            "SANCIONAR a Generadora Marbrisa, S.A. por la fuga de diésel detectada "
            "en septiembre de 2026 en su terminal del polígono portuario de Puerto "
            "Gris, que afectó las aguas subterráneas del polígono.",
            "La sancionada deberá presentar, en un plazo de sesenta (60) días "
            "calendario, un plan de saneamiento con medidas inmediatas de "
            "contención de la pluma de diésel, y abonar una sanción de "
            "V$ 2.750.000 dentro de los veinte (20) días hábiles.",
            "La decisión rige a partir de esta publicación; los interesados pueden "
            "pedir revisión ante el propio Directorio en el término de diez (10) "
            "días hábiles. Insértese en la Gaceta Oficial de Valdora, "
            "Edición No. 4129.",
        ],
        "gazette_ref": "Año CII, Edición No. 4129, miércoles 18 de noviembre de 2026, página 5",
    },
    {
        "id": "vmp-0412",
        "date": "2026-11-18",
        "published": "2026-11-18T19:40:00-06:00",
        "language": "en",
        "kind": "news",
        "outlet": "Valdora Morning Post",
        "url": "https://valdoramorningpost.example/articles/0412",
        "title": "Energy regulator fines Marbrisa 2.75 million valdoros over diesel "
                 "plume at Puerto Gris",
        "body": [
            "Valdora's energy regulator on Wednesday fined Generadora Marbrisa, S.A. "
            "V$ 2.75 million after a diesel leak contaminated groundwater at the Puerto "
            "Gris port zone.",
            "Resolution 098-2026, published in the official gazette, orders a cleanup "
            "plan within 60 days and immediate containment of the plume; Generadora "
            "Marbrisa may seek reconsideration before the energy board within 10 "
            "business days.",
        ],
        "original": {"id": "gaz-4129-arev-098", "language": "es",
                     "url": "https://gazetavaldora.example/edicion-4129/resolucion-098-2026"},
    },
]

BY_ID = {d["id"]: d for d in DOCUMENTS}
# ---------------------------------------------------------------------------
# Internal answer key. Written ONLY to tests/fixtures/desk/ — never shipped.
# ---------------------------------------------------------------------------

_ORACLE_EVENTS = [
    {
        "key": "arav-resolution-117-2026",
        "label": "material_decision",
        "canonical_doc": "gaz-4127-arav-117",
        "documents": ["gaz-4127-arav-117", "anuv-0847", "vmp-0411", "her-1129",
                      "pul-0563", "ledger-2026-111"],
        "expected_facts": {
            "authority": "Autoridad Reguladora de Aguas de Valdora (ARAV)",
            "instrument": "Resolución No. 117-2026",
            "first_published": "2026-11-16",
            "gazette_edition": 4127,
            "respondent": "Compañía Minera Cerro Alto, S.A.",
            "site": "acuífero de la Serranía del Toldo, sector Vega Norte",
            "fine": "V$ 4.200.000",
            "remediation_deadline_days": 90,
            "fine_payment_business_days": 30,
            "appeal_business_days": 15,
            "effective": "cumplimiento inmediato",
        },
        "why": "New, authoritative instrument first published in the official "
               "gazette on day 1: a liability finding, a 90-day remediation "
               "clock, a V$ 4.200.000 fine and immediate effect.",
    },
    {
        "key": "arev-resolution-098-2026",
        "label": "similar_unrelated_event",
        "canonical_doc": "gaz-4129-arev-098",
        "documents": ["gaz-4129-arev-098", "vmp-0412"],
        "expected_facts": {
            "authority": "Autoridad Reguladora de Energía de Valdora (AREV)",
            "instrument": "Resolución No. 098-2026",
            "first_published": "2026-11-18",
            "gazette_edition": 4129,
            "respondent": "Generadora Marbrisa, S.A.",
            "site": "aguas subterráneas del polígono portuario de Puerto Gris",
            "fine": "V$ 2.750.000",
            "remediation_deadline_days": 60,
            "fine_payment_business_days": 20,
            "appeal_business_days": 10,
            "effective": "cumplimiento inmediato",
        },
        "why": "Deliberate near-miss: the same domain vocabulary (fine, "
               "cleanup plan, groundwater, reconsideration window) but a "
               "different authority, respondent, instrument, site and "
               "magnitude. Not the same matter. A purely lexical embedding "
               "cannot tell the two Spanish gazette records apart (they "
               "share the function-word mass of legal Spanish) — that is "
               "the failure mode this packet exists to expose, and the "
               "fake-embedder baseline below records it.",
    },
]

ORACLE = {
    "schema": 1,
    "kind": "gaceta.desk-oracle",
    "packet_id": PACKET_ID,
    "never_ship_with": "src/gaceta/assets/desk/ — the blind tree is "
                       "redistributable on its own; this file must stay under "
                       "tests/fixtures/desk/.",
    "events": _ORACLE_EVENTS,
    "per_document": {
        "gaz-4127-arav-117": {
            "classification": "material_decision", "event": "arav-resolution-117-2026",
            "note": "First authoritative publication of a new instrument."},
        "anuv-0847": {
            "classification": "repeat", "event": "arav-resolution-117-2026",
            "note": "Same-evening wire copy restating Resolution 117-2026; no new facts."},
        "vmp-0411": {
            "classification": "repeat", "event": "arav-resolution-117-2026",
            "note": "English-language daily restating Resolution 117-2026; no new facts."},
        "her-1129": {
            "classification": "repeat", "event": "arav-resolution-117-2026",
            "note": "Next-day broadsheet restating Resolution 117-2026; adds a quote."},
        "pul-0563": {
            "classification": "repeat", "event": "arav-resolution-117-2026",
            "note": "Business daily restating Resolution 117-2026; adds analyst colour."},
        "ledger-2026-111": {
            "classification": "repeat", "event": "arav-resolution-117-2026",
            "note": "English analysis of Resolution 117-2026; commentary, not a new decision."},
        "gaz-4129-arev-098": {
            "classification": "similar_unrelated_event", "event": "arev-resolution-098-2026",
            "note": "AREV instrument against Generadora Marbrisa; same domain words, "
                    "different authority, respondent, instrument, site, magnitude."},
        "vmp-0412": {
            "classification": "similar_unrelated_event", "event": "arev-resolution-098-2026",
            "note": "English coverage of the AREV instrument; the same near-miss "
                    "pattern across languages."},
    },
    "counts": {
        "documents": 8,
        "material_decision": 1,
        "repeat": 5,
        "similar_unrelated_event": 2,
        "languages": {"es": 5, "en": 3},
    },
    "rubric": [
        {"id": "identify",
         "requirement": "Report exactly one material decision for the window: "
                        "ARAV Resolution 117-2026, first published 2026-11-16 in "
                        "gazette edition 4127."},
        {"id": "collapse-repeats",
         "requirement": "The five derivative documents are coverage of that one "
                        "decision: one story, not six."},
        {"id": "reject-similar-unrelated",
         "requirement": "AREV Resolution 098-2026 is a separate matter and must "
                        "not be merged into the ARAV decision or reported as "
                        "part of it."},
        {"id": "source-language",
         "requirement": "English coverage must resolve back to the Spanish "
                        "original notice (the gazette record), not stand as an "
                        "independent decision."},
    ],
    "fake_embedder_baseline": {
        "note": "Deterministic baseline of gaceta's FakeEmbedder (signed "
                "bag-of-words) at the fixture threshold 0.70, window 21 "
                "days, measured on this exact packet. Same-language "
                "coverage of the decision joins into one story per "
                "language; the English coverage of the unrelated event "
                "stays out of every story; and the lookalike Spanish "
                "gazette record of the unrelated event MERGES into the "
                "decision's Spanish story — a purely lexical embedding "
                "cannot separate two same-register legal texts. That "
                "merge is the failure mode the packet is built to "
                "expose: score a run of `gaceta eval` against "
                "golden.json and the pairwise/B-cubed numbers fall out "
                "of exactly this partition. This is plumbing, not a "
                "quality claim: the packet asserts nothing about any "
                "live model.",
        "expected_stories": [
            ["gaz-4127-arav-117", "anuv-0847", "her-1129", "pul-0563",
             "gaz-4129-arev-098"],
            ["vmp-0411", "ledger-2026-111"],
            ["vmp-0412"],
        ],
        "expected_metrics": {
            "note": "gaceta.evals.clustering.evaluate() of that partition "
                    "against golden.json — the numbers a FakeEmbedder run "
                    "of `gaceta eval` must reproduce exactly.",
            "precision": 0.6364, "recall": 0.4375, "f1": 0.5185,
            "b3_precision": 0.8, "b3_recall": 0.5417, "b3_f1": 0.646,
            "n_stories": 3,
        },
    },
}

GOLDEN = {
    "items": [d["id"] for d in DOCUMENTS],
    "clusters": [
        ["gaz-4127-arav-117", "anuv-0847", "vmp-0411", "her-1129",
         "pul-0563", "ledger-2026-111"],
        ["gaz-4129-arev-098", "vmp-0412"],
    ],
    "note": "Ideal clustering for gaceta evals (pairwise + B-cubed): one "
            "material decision with its five repeats as one story; the "
            "similar unrelated event as a second story. Internal file — keep "
            "beside oracle.json, never ship with the blind packet.",
}
# ---------------------------------------------------------------------------
# Small deterministic helpers (fixed maps — never locale-dependent).
# ---------------------------------------------------------------------------

LANG_LABEL = {"es": "Español", "en": "English"}
KIND_LABELS = {
    "es": {"gazette": "registro de gaceta", "news": "nota informativa",
           "analysis": "análisis"},
    "en": {"gazette": "gazette record", "news": "news report", "analysis": "analysis"},
}
META_LABELS = {
    "es": {"language": "Idioma", "kind": "Tipo", "outlet": "Fuente",
           "published": "Publicado", "document": "Documento", "url": "URL citada",
           "gazette": "Referencia de gaceta"},
    "en": {"language": "Language", "kind": "Kind", "outlet": "Outlet",
           "published": "Published", "document": "Document", "url": "Cited URL",
           "gazette": "Gazette reference"},
}


def _esc(text: str) -> str:
    return escape(str(text), quote=True)


def _rfc822(published: str) -> str:
    """"2026-11-16T06:00:00-06:00" -> "Mon, 16 Nov 2026 06:00:00 -0600"."""
    date_s, rest = published.split("T")
    hhmm = rest[:5]
    off = published[-6:].replace(":", "")
    y, m, d = date_s.split("-")
    return f"{DAY_META[date_s]['rfc']}, {int(d)} Nov {int(y)} {hhmm}:00 {off}"


def _dateline(doc: dict) -> str:
    """'2026-11-16T18:05:00-06:00' -> human text in the document's language."""
    date_s, rest = doc["published"].split("T")
    hhmm = rest[:5]
    y, _, d = date_s.split("-")
    if doc["language"] == "es":
        return (f"{DAY_META[date_s]['es']} {int(d)} de noviembre de {int(y)}, "
                f"{hhmm} ({TZ_LABEL})")
    return f"{DAY_META[date_s]['en']} {int(d)} November {int(y)}, {hhmm} ({TZ_LABEL})"


def _snapshot_path(doc: dict) -> str:
    return f"sources/{doc['id']}.html"


def _dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _packet_json() -> str:
    by_day = {d: sum(1 for x in DOCUMENTS if x["date"] == d) for d in DAYS}
    by_lang = {lang: sum(1 for x in DOCUMENTS if x["language"] == lang)
               for lang in ("es", "en")}
    return _dump_json({
        "schema": 1,
        "kind": "gaceta.desk-packet",
        "packet_id": PACKET_ID,
        "window": {"start": DAYS[0], "end": DAYS[-1], "days": len(DAYS),
                   "tz_offset": "-06:00"},
        "languages": ["en", "es"],
        "counts": {"documents": len(DOCUMENTS), "by_language": by_lang,
                   "by_day": by_day},
        "documents": DOCUMENTS,
        "provenance": {
            "generator": "make_desk_packet.py — deterministic, byte-identical replay",
            "content": "Every outlet, person, company, regulator, currency, place "
                       "and event is invented; hosts are reserved *.example names.",
            "offline": "Local files only; the packet was built and renders "
                       "without any network access.",
            "license": "CC BY 4.0 — see NOTICE.md and CC-BY-4.0.txt in this directory.",
        },
    })


def _feed_bytes() -> bytes:
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = (
        "Gaceta desk packet (synthetic EN/ES sample)")
    ET.SubElement(channel, "link").text = "https://gazetavaldora.example"
    ET.SubElement(channel, "description").text = (
        "Offline synthetic sample feed for the gaceta desk packet. Every item "
        "and host is invented (*.example); the packet ships as a local file "
        "and nothing is fetched.")
    for doc in DOCUMENTS:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = doc["title"]
        ET.SubElement(item, "link").text = doc["url"]
        guid = ET.SubElement(item, "guid")
        guid.text = doc["id"]
        ET.SubElement(item, "pubDate").text = _rfc822(doc["published"])
        ET.SubElement(item, "description").text = "\n\n".join(doc["body"])
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"

_CSS = """\
:root {
  color-scheme: light dark;
  --bg: #fbf9f3;
  --ink: #1c1b18;
  --muted: #6b675e;
  --rule: #d9d3c5;
  --accent: #8a2f2b;
  --card: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #151513;
    --ink: #ece8df;
    --muted: #a7a195;
    --rule: #3a3833;
    --accent: #e0908a;
    --card: #1e1d1a;
  }
}
* { box-sizing: border-box; }
html { background: var(--bg); color: var(--ink); }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  font-size: 1rem;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.wrap { width: min(100% - 2.5rem, 58rem); margin-inline: auto; padding-block-end: 3rem; }
a { color: var(--accent); }
a:focus-visible, .skip:focus-visible {
  outline: 3px solid var(--accent);
  outline-offset: 2px;
}
.skip {
  position: absolute;
  left: -100vw;
  top: 0;
  background: var(--card);
  color: var(--ink);
  border: 2px solid var(--ink);
  padding: 0.5rem 1rem;
}
.skip:focus { left: 0.5rem; top: 0.5rem; z-index: 10; }
header.masthead { padding-block: 1.2rem 0.9rem; margin-block-end: 1.1rem;
  border-block-end: 1px solid var(--ink); }
header.masthead h1 { margin: 0; font-size: 1.6rem; line-height: 1.2; }
header.masthead .lede { margin: 0.4rem 0 0; color: var(--muted); font-size: 0.95rem; }
.stats { margin: 0.3rem 0 0; color: var(--muted); font-size: 0.85rem; }
.legend { margin-block: 0.6rem 1.2rem; color: var(--muted); font-size: 0.85rem; }
nav.days { margin-block: 0 0.8rem; }
nav.days ul { margin: 0; padding-inline-start: 1.2rem; }
section.day { margin-block-start: 1.6rem; padding-block-start: 0.6rem;
  border-block-start: 2px solid var(--rule); }
section.day > h2 { margin: 0.2rem 0 0.1rem; font-size: 1.25rem; }
p.day-note { margin: 0.1rem 0 0.8rem; color: var(--muted); font-size: 0.85rem; }
article.doc {
  margin-block-end: 1.4rem;
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 6px;
  padding: 0.9rem 1.1rem 0.8rem;
}
article.doc > h1, article.doc > h3 { margin: 0 0 0.35rem; font-size: 1.05rem;
  line-height: 1.35; }
p.subtitle { margin: 0 0 0.4rem; color: var(--muted); font-size: 0.92rem; }
.badges { display: flex; gap: 0.4rem; flex-wrap: wrap; margin: 0 0 0.45rem; }
.badge {
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  border: 1px solid var(--muted);
  border-radius: 999px;
  padding: 0.05rem 0.55rem;
}
dl.meta { display: grid; grid-template-columns: max-content 1fr;
  gap: 0.15rem 0.8rem; margin: 0.3rem 0 0.6rem; font-size: 0.85rem; }
dl.meta dt { color: var(--muted); }
dl.meta dd { margin: 0; overflow-wrap: anywhere; }
div.body p { margin: 0.45rem 0; }
ul.links { margin: 0.6rem 0 0; padding-inline-start: 1.2rem; font-size: 0.9rem; }
footer.page { margin-block-start: 2rem; padding-block-start: 0.8rem;
  border-block-start: 2px solid var(--rule); color: var(--muted); font-size: 0.88rem; }
footer.page h2 { color: var(--ink); font-size: 1.05rem; margin: 0.2rem 0 0.4rem; }
footer.page ul { padding-inline-start: 1.2rem; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.85em; }
"""


def _meta_rows(doc: dict, labels: dict) -> list[tuple[str, str]]:
    rows = [
        (labels["language"], LANG_LABEL[doc["language"]]),
        (labels["kind"], KIND_LABELS[doc["language"]][doc["kind"]]),
        (labels["outlet"], doc["outlet"]),
        (labels["published"], _dateline(doc)),
        (labels["document"], doc["id"]),
        (labels["url"], doc["url"]),
    ]
    if "gazette_ref" in doc:
        rows.append((labels["gazette"], doc["gazette_ref"]))
    return rows


def _meta_dl(doc: dict, labels: dict) -> str:
    rows = []
    for k, v in _meta_rows(doc, labels):
        if k == labels["published"]:
            rows.append(f'        <dt>{_esc(k)}</dt><dd><time datetime="{doc["published"]}">'
                        f"{_esc(v)}</time></dd>")
        else:
            rows.append(f"        <dt>{_esc(k)}</dt><dd>{_esc(v)}</dd>")
    return f'      <dl class="meta">\n{chr(10).join(rows)}\n      </dl>'


def _body_html(doc: dict, indent: str = "      ") -> str:
    paras = "\n".join(f"{indent}<p>{_esc(p)}</p>" for p in doc["body"])
    return f"{indent}<div class=\"body\">\n{paras}\n{indent}</div>"

def _snapshot_page(doc: dict) -> str:
    """One standalone offline page per document, in the document's language."""
    lang = doc["language"]
    labels = META_LABELS[lang]
    orig = doc.get("original")
    if lang == "es":
        note = f"Ejemplar sintético de «{_esc(doc['outlet'])}» — copia local; no se accedió a ningún sitio."
        back = "Volver al escritorio"
        snap_label = "Copia local de la fuente"
        orig_label = "Aviso original (español)"
    else:
        note = f"Synthetic sample from {_esc(doc['outlet'])} — local copy; no site was ever accessed."
        back = "Back to the desk"
        snap_label = "Local source snapshot"
        orig_label = "Original notice (Spanish)"
    badges = (f'        <p class="badges"><span class="badge">{lang.upper()}</span>'
              f'<span class="badge">{doc["kind"].upper()}</span></p>')
    links = [f'          <li><a href="{doc["id"]}.html">{_esc(snap_label)}</a></li>']
    if orig:
        lang_attr = ' lang="es"' if lang == "es" else ""
        links.append(f'          <li><a href="{orig["id"]}.html" hreflang="es"'
                     f'{lang_attr}>{_esc(orig_label)}</a></li>')
    links.append(f'          <li><a href="../index.html">{_esc(back)}</a></li>')
    links_html = "\n".join(links)
    subtitle = ""
    if doc.get("subtitle"):
        subtitle = f'        <p class="subtitle">{_esc(doc["subtitle"])}</p>\n'
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Offline snapshot of a synthetic sample document. All content invented.">
  <title>{_esc(doc["title"])} — {_esc(doc["outlet"])} (offline copy)</title>
  <style>
{_CSS}  </style>
</head>
<body>
  <div class="wrap">
    <header class="masthead">
      <p class="lede">{note}</p>
    </header>
    <main>
      <article class="doc">
        <h1>{_esc(doc["title"])}</h1>
{subtitle}{badges}
{_meta_dl(doc, labels)}
{_body_html(doc)}
        <ul class="links">
{links_html}
        </ul>
      </article>
    </main>
    <footer class="page">
      <p>Synthetic sample content · CC BY 4.0 · see
        <a href="../NOTICE.md">NOTICE.md</a> and
        <a href="../CC-BY-4.0.txt">CC-BY-4.0.txt</a>.</p>
    </footer>
  </div>
</body>
</html>
"""

def _desk_article(doc: dict) -> str:
    """One article card on the desk page (English chrome, per-document lang)."""
    lang = doc["language"]
    orig = doc.get("original")
    if lang == "es":
        snap_label = "Copia local de la fuente"
        orig_label = "Aviso original (español)"
    else:
        snap_label = "Local source snapshot"
        orig_label = "Original notice (Spanish)"
    links = [f'          <li><a href="{_snapshot_path(doc)}">{_esc(snap_label)}</a></li>']
    if orig:
        lang_attr = ' lang="es"' if lang == "es" else ""
        links.append(f'          <li><a href="sources/{orig["id"]}.html" '
                     f'hreflang="es"{lang_attr}>{_esc(orig_label)}</a></li>')
    links_html = "\n".join(links)
    subtitle = ""
    if doc.get("subtitle"):
        subtitle = f'        <p class="subtitle">{_esc(doc["subtitle"])}</p>\n'
    badges = (f'        <p class="badges"><span class="badge">{lang.upper()}</span>'
              f'<span class="badge">{doc["kind"].upper()}</span></p>')
    return f"""      <article class="doc" lang="{lang}" aria-labelledby="doc-h-{doc['id']}">
        <h3 id="doc-h-{doc['id']}">{_esc(doc["title"])}</h3>
{subtitle}{badges}
{_meta_dl(doc, META_LABELS["en"])}
{_body_html(doc)}
        <ul class="links">
{links_html}
        </ul>
      </article>"""


def _desk_page() -> str:
    by_day: dict[str, list[dict]] = {d: [] for d in DAYS}
    for doc in DOCUMENTS:
        by_day[doc["date"]].append(doc)

    nav_items = "\n".join(
        f'        <li><a href="#day-{d}">{DAY_META[d]["en"]} {int(d[-2:])} November 2026'
        f' ({len(by_day[d])} documents)</a></li>'
        for d in DAYS)

    day_sections = []
    for i, d in enumerate(DAYS, start=1):
        n_es = sum(1 for x in by_day[d] if x["language"] == "es")
        n_en = len(by_day[d]) - n_es
        articles = "\n".join(_desk_article(x) for x in by_day[d])
        day_sections.append(f"""    <section class="day" id="day-{d}" aria-labelledby="day-h-{d}">
      <h2 id="day-h-{d}">Day {i} · {DAY_META[d]["en"]} {int(d[-2:])} November 2026</h2>
      <p class="day-note">{len(by_day[d])} documents · {n_es} Español · {n_en} English</p>
{articles}
    </section>""")
    days_html = "\n".join(day_sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="An accessible offline rendering of a synthetic bilingual (EN/ES) three-day desk packet. All content invented; every link is local.">
  <title>Discovery desk — synthetic EN/ES three-day packet (16–18 November 2026)</title>
  <style>
{_CSS}  </style>
</head>
<body>
  <div class="wrap">
    <a class="skip" href="#main">Skip to main content</a>
    <header class="masthead">
      <h1>Discovery desk</h1>
      <p class="lede">A three-day, bilingual packet of invented documents as it sits on
        the desk before clustering: official gazette records and press coverage in Spanish
        and English. A sample of documents only — nothing here is labelled, ranked or
        scored.</p>
      <p class="stats">Eight documents · five Español, three English ·
        16–18 November 2026 · every outlet, person, company, regulator, currency, place
        and event is invented.</p>
    </header>
    <p class="legend">Badges mark the document's language (ES / EN) and kind
      (GAZETTE = official gazette record, NEWS = press report, ANALYSIS = commentary).
      Every link on this page resolves to a local file; no site was accessed.</p>
    <nav class="days" aria-label="Packet days">
      <ul>
{nav_items}
      </ul>
    </nav>
    <main id="main">
{days_html}
    </main>
    <footer class="page">
      <h2>About this sample</h2>
      <p>This page renders the blind desk packet: eight invented documents over three
        days, fully offline. It was generated deterministically — regenerate byte for
        byte with <code>python src/gaceta/assets/desk/make_desk_packet.py</code>.</p>
      <ul>
        <li>Packet data: <a href="packet.json">packet.json</a> ·
          ingestable form: <a href="feed.rss">feed.rss</a></li>
        <li>Attribution and provenance: <a href="NOTICE.md">NOTICE.md</a> ·
          data license: <a href="CC-BY-4.0.txt">CC-BY-4.0.txt</a></li>
      </ul>
      <p>The data in this directory is CC BY 4.0; the generator script follows the
        repository's AGPL-3.0. No live model was run to build this page, and nothing
        here asserts anything about any live model.</p>
    </footer>
  </div>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Self-verification and build
# ---------------------------------------------------------------------------

def _check_doc_invariants() -> None:
    ids = [d["id"] for d in DOCUMENTS]
    assert len(ids) == len(set(ids)) == 8, "eight unique document ids"
    assert {d["date"] for d in DOCUMENTS} == set(DAYS), "documents must cover exactly the 3-day window"
    for d in DAYS:
        langs = {x["language"] for x in DOCUMENTS if x["date"] == d}
        assert langs == {"es", "en"}, f"day {d} must carry both languages"
    assert [d["published"] for d in DOCUMENTS] == sorted(d["published"] for d in DOCUMENTS), \
        "documents must be listed in published order"
    gazettes = {d["id"] for d in DOCUMENTS if d["kind"] == "gazette"}
    assert len(gazettes) == 2, "two gazette records (one per event)"
    for doc in DOCUMENTS:
        url = doc["url"]
        assert re.match(r"https://[^/]+\.example(/|$)", url), f"invented host expected: {url}"
        if doc["kind"] == "gazette":
            assert "gazette_ref" in doc and "original" not in doc, doc["id"]
        else:
            orig = doc.get("original")
            assert orig and orig["id"] in gazettes, f"{doc['id']} must cite a gazette record"
            assert orig["language"] == "es", "originals are in their source language (es)"
            assert orig["url"] == BY_ID[orig["id"]]["url"], "original url must match the record"
    assert sum(1 for d in DOCUMENTS if d["language"] == "es") == 5
    assert sum(1 for d in DOCUMENTS if d["language"] == "en") == 3


# Authored sidecars that complete the shipped unit (not written by build()).
SIDE_FILES = {"NOTICE.md", "CC-BY-4.0.txt"}
SIDE_SNAPSHOT_HREFS = {f"../{name}" for name in SIDE_FILES}


def _check(blind: dict[str, str], oracle: dict[str, str],
           blind_root: pathlib.Path, require_sidecars: bool = False) -> None:
    """Fail fast on anything that would break the packet's guarantees."""
    _check_doc_invariants()

    expected = {"packet.json", "feed.rss", "index.html"} | {
        f"sources/{d['id']}.html" for d in DOCUMENTS}
    assert set(blind) == expected, f"blind tree mismatch: {set(blind) ^ expected}"

    # --- isolation: the shipped tree must carry no key-side vocabulary
    for name, text in blind.items():
        low = text.lower()
        for token in ORACLE_ONLY_TOKENS:
            assert token not in low, f"blind file {name} leaks {token!r}"
        assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", text), \
            f"no email-shaped text in {name}"
        if name.endswith(".html"):
            assert not re.search(r'(?:href|src)="https?://', text), \
                f"{name} must link only local files"
            assert "<script" not in text.lower(), f"no scripts in {name}"

    # --- index.html: anchors, links, datetimes
    index = blind["index.html"]
    ids = set(re.findall(r'id="([^"]+)"', index))
    for href in re.findall(r'href="([^"]+)"', index):
        if href.startswith("#"):
            assert href[1:] in ids, f"dangling anchor {href}"
        elif href in SIDE_FILES:
            if require_sidecars:
                assert (blind_root / href).exists(), f"shipped unit must carry {href}"
        else:
            assert (blind_root / href).exists(), f"index link must resolve: {href}"
    assert index.count("<h1") == 1
    assert 'class="skip"' in index and 'href="#main"' in index and 'id="main"' in index
    for doc in DOCUMENTS:
        assert f'datetime="{doc["published"]}"' in index, doc["id"]
        assert f'article class="doc" lang="{doc["language"]}"' in index, doc["id"]

    # --- snapshots: titles, datelines, source-language links
    for doc in DOCUMENTS:
        snap = blind[_snapshot_path(doc)]
        assert f'lang="{doc["language"]}"' in snap.split("\n", 2)[1], doc["id"]
        assert _esc(doc["title"]) in snap and _dateline(doc) in snap, doc["id"]
        for href in re.findall(r'href="([^"]+)"', snap):
            if href in SIDE_SNAPSHOT_HREFS:
                if require_sidecars:
                    assert (blind_root / "sources" / href).exists(), \
                        f"shipped unit must carry {href[3:]}"
            else:
                assert (blind_root / "sources" / href).exists(), \
                    f"snapshot link must resolve: {href}"
        if doc["kind"] != "gazette":
            assert f'href="{doc["original"]["id"]}.html" hreflang="es"' in snap, \
                f"{doc['id']} must link its Spanish original"

    # --- feed.rss round-trips through gaceta's own parser expectations
    root = ET.fromstring(blind["feed.rss"].encode("utf-8"))
    items = list(root.iter("item"))
    assert [i.findtext("guid") for i in items] == [d["id"] for d in DOCUMENTS]
    for i, doc in zip(items, DOCUMENTS):
        assert i.findtext("title") == doc["title"]
        assert i.findtext("link") == doc["url"]
        parsedate_to_datetime(i.findtext("pubDate"))
        assert (i.findtext("description") or "").split("\n\n") == doc["body"]

    # --- packet.json is the documents, verbatim, with public counts only
    packet = json.loads(blind["packet.json"])
    assert packet["documents"] == DOCUMENTS
    assert packet["counts"]["documents"] == 8
    assert packet["counts"]["by_language"] == {"es": 5, "en": 3}

    # --- the key side: partitions and counts must line up
    oracle_json = json.loads(oracle["oracle.json"])
    assert set(oracle_json["per_document"]) == {d["id"] for d in DOCUMENTS}
    classes = [v["classification"] for v in oracle_json["per_document"].values()]
    assert classes.count("material_decision") == 1
    assert classes.count("repeat") == 5
    assert classes.count("similar_unrelated_event") == 2
    docs_seen = [doc for ev in oracle_json["events"] for doc in ev["documents"]]
    assert sorted(docs_seen) == sorted(d["id"] for d in DOCUMENTS), \
        "events must partition the packet"
    golden = json.loads(oracle["golden.json"])
    clustered = [i for c in golden["clusters"] for i in c]
    assert sorted(clustered) == sorted(golden["items"]), "golden clusters partition the items"
    assert len(clustered) == len(set(clustered)), "no item in two golden clusters"
    baseline = oracle_json["fake_embedder_baseline"]["expected_stories"]
    assert sorted(i for s in baseline for i in s) == sorted(golden["items"])


def build(blind_dest: pathlib.Path, oracle_dest: pathlib.Path, *,
          require_sidecars: bool = False) -> list[pathlib.Path]:
    """Write the blind tree under blind_dest and the key under oracle_dest.

    Deterministic: same inputs, byte-identical outputs, no clock, no network.
    ``require_sidecars=True`` (used by the in-place refresh) additionally
    insists that NOTICE.md and CC-BY-4.0.txt sit beside the generated files:
    the shipped unit is only complete with them.
    """
    blind_dest = pathlib.Path(blind_dest)
    oracle_dest = pathlib.Path(oracle_dest)
    (blind_dest / "sources").mkdir(parents=True, exist_ok=True)
    oracle_dest.mkdir(parents=True, exist_ok=True)

    blind: dict[str, str] = {}
    written: list[pathlib.Path] = []

    def emit(rel: str, text: str) -> pathlib.Path:
        path = _write(blind_dest / rel, text)
        blind[rel] = text
        written.append(path)
        return path

    emit("packet.json", _packet_json())
    feed_path = blind_dest / "feed.rss"
    feed_path.write_bytes(_feed_bytes())
    blind["feed.rss"] = feed_path.read_text(encoding="utf-8")
    written.append(feed_path)
    for doc in DOCUMENTS:
        emit(_snapshot_path(doc), _snapshot_page(doc))
    emit("index.html", _desk_page())

    oracle: dict[str, str] = {}
    for rel, obj in (("oracle.json", ORACLE), ("golden.json", GOLDEN)):
        path = _write(oracle_dest / rel, _dump_json(obj))
        oracle[rel] = path.read_text(encoding="utf-8")
        written.append(path)

    _check(blind, oracle, blind_dest, require_sidecars=require_sidecars)
    return written


if __name__ == "__main__":
    repo = pathlib.Path(__file__).resolve().parents[4]
    files = build(repo / "src" / "gaceta" / "assets" / "desk",
                  repo / "tests" / "fixtures" / "desk", require_sidecars=True)
    for path in sorted(files):
        print(path.relative_to(repo))
    print(f"desk packet {PACKET_ID}: {len(files)} files, deterministic replay")
