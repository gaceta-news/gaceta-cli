# Desk packet — internal key and authoring notes

This directory is the **isolated key** for the synthetic desk packet that
ships in the wheel at `src/gaceta/assets/desk/`. It stays on the test side
of the repository and never travels with the sample: the shipped tree is
blind (documents only), and `tests/test_desk_packet.py` scans every shipped
file to keep it that way. When distributing the sample, ship the blind unit
only — the exact list is in the desk `NOTICE.md`.

All packet content is invented: fictional Valdora, reserved `*.example`
hosts, fictional people. No private data, no scraped data, no network.

## The packet (what the key asserts)

Eight documents, EN/ES, over three days (2026-11-16 … 18, UTC-06:00):

- **one material decision** — `gaz-4127-arav-117`, the ARAV Resolution
  117-2026 gazette record (day 1): liability for the Serranía del Toldo
  aquifer, a 90-day remediation plan, a V$ 4.200.000 fine, immediate
  effect;
- **five repeats of it** — ES wire (day 1), EN daily (day 1), ES broadsheet
  (day 2), ES business daily (day 2), EN analysis (day 2); each cites the
  Spanish gazette original (source-language links);
- **one similar unrelated event** — `gaz-4129-arev-098`, the AREV
  Resolution 098-2026 gazette record (day 3), plus its EN coverage: the
  same domain vocabulary (fine, cleanup plan, groundwater) but a different
  authority, respondent, instrument, site and magnitude.

Files: `oracle.json` (events, per-document classification, expected facts,
the grading rubric, and the deterministic FakeEmbedder baseline) and
`golden.json` (the `gaceta.evals.clustering` golden: one story for the
decision and its five repeats, one story for the unrelated event).

## Deterministic replay

    python src/gaceta/assets/desk/make_desk_packet.py

writes both trees byte-identically — no clock, no randomness, no network.
`tests/test_desk_packet.py::test_replay_is_byte_identical` proves it by
rebuilding into a temp dir and comparing against the committed files.

## Scoring with the repo's own eval harness

Against a reader whose `[embed]` provider is `fake` (threshold 0.70,
window 21 days):

```bash
# in the reader's [[sources.rss.feeds]]: file = "<repo>/src/gaceta/assets/desk/feed.rss"
gaceta ingest rss --reader <id>
gaceta embed --reader <id> && gaceta cluster --reader <id>
gaceta eval clustering --reader <id> --golden tests/fixtures/desk/golden.json
```

The recorded FakeEmbedder baseline (see `oracle.json` →
`fake_embedder_baseline`) is: one Spanish story of the decision — **with
the lookalike AREV record merged into it**, because a purely lexical
embedding cannot separate two same-register legal texts; that merge is the
failure mode this packet exists to expose — one English story of the
decision, and the English coverage of the unrelated event as a singleton.
Against `golden.json` that partition scores pairwise precision 0.6364,
recall 0.4375, F1 0.5185 (B-cubed 0.8 / 0.5417 / 0.646). The pipeline
test asserts those exact numbers, so any edit to the packet's wording that
shifts the deterministic clustering fails the suite.

## No live models

The packet and its tests run only the deterministic FakeEmbedder. They
make no claims about any live model, vendor or API, and no live model was
run to build the packet. Cross-language clustering (the golden's ideal
single story across EN and ES) is a property of multilingual embedders,
which this fixture deliberately does not evaluate.
