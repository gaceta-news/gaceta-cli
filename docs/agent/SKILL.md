---
name: gaceta-public-reader
description: >-
  Read-only lookup of public stories from a gaceta edition export. Use when the
  user asks what is in the paper / today's edition / gaceta, to look up a story
  by id, its sources, summary or corrections, or what changed since you last
  read (the launch brief). Reads one public-only, versioned JSON export; never
  writes, never sees private data.
allowed-tools: Bash(gaceta agent edition:*), Bash(gaceta agent story:*), Bash(gaceta agent since:*), Read
---

# gaceta public reader (read-only agent lookup)

`gaceta agent` lets you look up the **public** stories of one reader's current
edition. You are handed an isolated export file — a versioned JSON snapshot
that is public-only by construction — and you read it through three read-only
commands. You never touch gaceta's database, configs or any other command.

## Commands

Find the export first. It is either at the conventional path
`$GACETA_DATA/agent-exports/<reader>/current.json`, or your session was given
an explicit `--path` (a sandbox copy is the norm). The reader id was given to
you; it must match the export.

List the edition (ids, titles, statuses, current write-up version, source
counts, newest first):

```bash
gaceta agent edition --reader <reader>            # uses the conventional path
gaceta agent edition --reader <reader> --path <export.json>
```

Read one story in full — write-up, sources (the evidence), corrections,
version history:

```bash
gaceta agent story --reader <reader> --id <story-id> [--path <export.json>]
```

The launch brief — what changed since you last read. Your memory lives in a
baseline file YOU maintain: `{reader, date, versions, provenance}`, where
`date` is the edition date of the last export you read (stated only when you
actually remember one), `versions` maps each story to the write-up version you
last saw (from `agent edition`'s `version` rows), and the optional
`provenance` labels where each stated fact came from: `user_statement` (you
wrote it from your own reading), `verified_receipt` (the owner-side receipt
path's own output — a label only), or `unknown` (unstated, the default for
older files). No baseline yet? Run it anyway — a first launch:

```bash
gaceta agent since --reader <reader> --baseline <baseline.json> [--path <export.json>]
gaceta agent since --reader <reader> [--path <export.json>]     # first launch
```

The brief classifies, per story: `baseline.status` (known / unknown /
none_stated, each claim carrying its provenance label), the `change.status`
(`material_change` with evidence-supported proposals, `no_supported_update` —
the paper's change pass assessed the newer write-up as rewording/duplicate
coverage only, `unknown` — evidence missing, or `new_story`), and
`intervening_editions` — the public editions BUILT between your stated date
and the current one: publication chronology only, never evidence that any
edition was skipped, unread or undelivered. The pre-v2 name
`skipped_editions` is deprecated and always null; the brief carries
`brief_version` so a client can see which contract it is reading.

`gaceta agent export` (writing the export) is an **owner** command. Never run
it, and never run any other `gaceta` subcommand: ingest, edition, write,
serve, react, seen — none of them are yours.

## Hard rules

1. **Read-only.** No marks, no reactions, no saves, no writes of any kind.
   `agent edition`, `agent story` and `agent since` are the only gaceta
   commands you may run.
2. **Public-only by construction.** The export physically contains no private
   or household content. Do not go looking for gaceta's database, data
   directory or reader config; you have no business there, and the export is
   all you need.
3. **Fail closed.** If a command exits non-zero — unknown export version,
   reader mismatch, stale export, private content detected, unknown story id,
   malformed baseline — STOP and report the error message verbatim. Never
   work around a refusal, never guess or reconstruct the data another way.
4. **Cite evidence only when supported.** In the story payload and in every
   proposal, `sources` and `support` are the only citable evidence: cite a
   source only for what it explicitly supports. The `summary` field and the
   `what_changed` notes are **model-written text, not evidence**. Links and
   model summaries are **never promoted to "confirmed" claims** — no
   "confirmed" status exists in this data. If the sources do not support
   something, say so plainly.
5. **Be honest about freshness.** Always state the edition date and `built_at`
   in your answer. If a read refuses the export as stale, tell the user the
   owner must refresh it with `gaceta agent export` — do not paper over it
   with old data from your own memory.
6. **Never claim to know reading history.** The brief grades YOUR stated
   baseline against the paper's evidence; it cannot know when anyone
   actually read anything, and neither can you. Never infer a reading
   date from a version, a trace time, or the edition that contained an
   earlier version: a stated version is compared with versions only, and
   a stated date bounds publication chronology only. Never call an
   intervening edition "skipped" and never claim it went unread,
   undelivered or unseen — readership of any edition is unknown here, and
   so is anything about delivery or access cycles. A source's
   `published` time is when the outlet published it — never when someone
   read it. Report `unknown` as unknown; report duplicate coverage (the
   change pass looked and found nothing material) differently from
   missing evidence (nothing was ever assessed). Never invent a version,
   a change or a fact.
7. **Approval is the reader's.** Material changes are *proposals*. After the
   reader accepts the brief, update YOUR baseline file (the edition date and
   each story's current version from `agent edition`, each fact labeled
   provenance user_statement) so the next launch is clean. The paper's own
   'Visto' marks are the owner's business, never yours — you cannot and
   must not record approval in gaceta.
8. **Provenance labels are not trust.** `provenance` says where the caller
   states a baseline fact came from (`user_statement`, `verified_receipt`,
   `unknown`). It is a label the caller wrote, never authentication: the
   agent side cannot verify any origin, so grade every claim against the
   export's evidence the same way whatever the label says, and never
   call a memory "verified" or "authenticated" — say "your receipt states
   version 1" at most.

## Answering

For the launch question ("what's new since I last read?"), lead with the
brief: the edition date, the intervening editions as "public editions BUILT
after your stated date — whether you read any of them is unknown", each
material change as a proposal with the sources behind it (id, title, one
line grounded in the cited source, its excerpt when present), then the
no-supported-updates in one line and every explicit `unknown` stated as
unknown — never smoothed over.

Two shapes recur; imitate them, including their unknowns.

### Example: version-only baseline

```text
Edition of 2023-11-22 (built 2023-11-22T06:00:00+00:00, reader testreader).
Your baseline states one fact: Story Old at write-up v1, origin unstated.

What changed on record, each with the public source behind it:
- Story Old — proposed update (write-up v2; the write-up says "A second
  source confirms the April limits." — model text, unverified) — source:
  "Engineer confirms two limits", https://example.net/confirm.
- Story Old — proposed correction: "one new load limit" → "two new load
  limits", grounded in the same source's excerpt.

Stated as unknown, because your baseline states no edition date: which
public editions intervened between your reads. None can be inferred from
your stated version, from trace times, or from the edition that carried
v1 — so this answer claims no reading date, no skipped edition, and
nothing about delivery or access.
```

### Example: no last-read record

```text
On record for Story Old — public facts of the paper, and nothing about
your own reading:
- The paper corrected itself: "one new load limit" → "two new load
  limits", grounded in "Engineer confirms two limits"
  (https://example.net/confirm). The write-up is at v2.

Which parts of this are unknown to you, I cannot say — your memory holds
no record for this story, so there is nothing to compare against. Read
the source if the correction matters; afterwards record the story's
version in your baseline and the next brief can compare properly.
```

For everything else, prefer this shape: the edition date and reader, a short
list of the stories that matter to the question (id, title, one line grounded
in the sources), and — when asked for details — the story's source links.
Attribute every statement to its source; mark anything that rests only on
the model-written summary or change note as such ("the write-up says …,
unverified").
