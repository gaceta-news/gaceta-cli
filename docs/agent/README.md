# Agent lookup: a public, read-only window into the paper

`gaceta agent` is the surface for **external AI agent sessions** (Claude Code,
Hermes, OpenCode, anything that can run a CLI). An agent session never gets
the gaceta database. It gets one artifact — a **versioned, public-only JSON
export** derived from a public edition snapshot — and three read-only
commands to look inside it, including the launch brief: *what changed since
I last read?*

```
gaceta edition --max-privacy public   →  <data>/editions/<reader>/<date>-public.json
gaceta agent export                  →  <data>/agent-exports/<reader>/<date>.json
                                                and <data>/agent-exports/<reader>/current.json
gaceta agent edition | story          ←  the paper's public stories, read-only
gaceta agent since                   ←  the launch brief: what changed since your baseline
```

## Why this shape

- **Public-only by construction.** The export is derived from an edition built
  with `--max-privacy public`, which physically cannot contain private or
  household stories. The export step re-checks every story (label `public`,
  no `why_it_matters`, http(s) source urls only) and refuses the whole export
  if anything is off. Taste signals (scores, portada order, reactions),
  discovery blocks, conditions boxes and media paths are never exported.
- **Isolation.** The reader commands (`agent edition`, `agent story`,
  `agent since`) open exactly one file. No database, no reader config, no
  writes: reading cannot mark stories seen, record reactions, or change
  anything at all.
- **Bound reader.** The manifest records which reader an export belongs to.
  A lookup must pass that same `--reader`; a baseline bound to another reader
  refuses. Another paper's stories, editions or memory never cross over.
- **Fail closed.** Unknown schema version, unknown export kind, reader
  mismatch, non-public content, a non-http(s) source url, a stale (or
  future-dated) `built_at`, a malformed evidence block, an unknown story id —
  every one is a hard error: exit code 2, a message on stderr, and **no data
  on stdout**. Never a partial answer.
- **Citation contract.** Every payload carries the `citation` block: sources
  are the only citable evidence; the `summary` field and the change notes are
  model-written text, never evidence; links and model summaries are never
  promoted to "confirmed" claims. The skill (below) teaches agents to honor
  it; the data itself states it so no prompt can quietly drop it.
- **Honest time.** Three clocks exist and the payload keeps them apart (the
  `semantics` block): a source's `published` (when the *outlet* published it),
  the paper's trace times (`added`, `written_at`, `first_seen`, `last_seen` —
  when the *paper* ingested, clustered or wrote something), and READING
  HISTORY, which this surface does not have: it holds no per-person marks
  (the `seen` table never leaves the owner side), so every "since" comparison
  uses the caller's own stated baseline or trace times — a `published` stamp
  is never compared with anyone's reading.

## Owner setup

Build the public edition, then export. `agent export` takes the day's public
snapshot (default today, `--date` to pick another) and writes
`agent-exports/<reader>/<date>.json` plus `current.json`, atomically. The
same command attaches the launch evidence from the paper's own tables (the
public build trace from `editions`, each story's write-up history from
`story_versions` with the public sources that joined between versions,
corrections with their evidence item, and coverage no write-up has assessed
yet) — everything re-checked against privacy, nothing per-person (no `seen`
marks, no reactions, no taste) ever queried for it:

```bash
gaceta edition --reader main --max-privacy public --force
gaceta agent export  --reader main
```

In cron, right after the edition build (`agent export` fails closed if the
public snapshot is missing, so a skipped edition is never silently papered
over):

```cron
30 8 * * *  gaceta edition --reader main --max-privacy public --force && gaceta agent export --reader main
```

Exports refuse to serve after 48h (`--max-age-hours` to change, on the agent
side, per call). That is the point: an agent reading last month's export as
"the paper" fails closed instead of answering.

## Giving an agent session access

The agent needs: the export file, the reader id, and the three read-only
commands. Two patterns:

**Sandbox copy (simplest).** Copy `agent-exports/<reader>/current.json` into
the session's sandbox and point the commands at it. Nothing else travels:

```bash
gaceta agent edition --reader main --path /sandbox/current.json
gaceta agent story   --reader main --id <id> --path /sandbox/current.json
gaceta agent since   --reader main --baseline /sandbox/baseline.json --path /sandbox/current.json
```

**Wrapper (restricts the CLI itself).** If the session can run `gaceta`
directly, hand it a shim that only forwards the read-only subcommands:

```sh
#!/bin/sh
# gaceta-agent-lookup — the only gaceta surface an agent session gets.
set -eu
[ "$#" -ge 1 ] || { echo "usage: gaceta-agent-lookup edition|story …" >&2; exit 2; }
case "$1" in
  edition|story|since) exec gaceta agent "$@" ;;
  *) echo "read-only lookup: 'edition', 'story' or 'since' only" >&2; exit 2 ;;
esac
```

The skill below already restricts the agent to the read-only commands; the
shim makes the restriction physical rather than instructed.

## The launch brief: what changed since you last read

At the start of a session the agent's question is "what's new since I last
looked?" — and the only honest baseline for that is the agent's **own
memory**, which it states in a small baseline file (the caller writes it; a
gaceta surface never does):

```json
{
  "reader": "main",
  "date": "2026-11-12",
  "versions": {"story-id": 2},
  "provenance": {"date": "user_statement", "versions": {"story-id": "user_statement"}}
}
```

- `reader` is required and must match: one paper's memory never reads
  another's.
- `date` is the edition date of the last export the session read. Omitted on
  a first launch; the brief then says what it cannot know (explicit
  unknowns, never guesses).
- `versions` maps each story to the write-up version the session last saw —
  the same versions the paper numbers itself (`story_versions`), visible as
  `version` in `agent edition` rows and `agent story` payloads.
- `provenance` (optional, brief v2) labels where each stated fact came
  from: `user_statement`, `verified_receipt` (the receipt path's own
  output), or `unknown` — the default for baseline files written before
  the field existed. A label is the caller's own statement of origin,
  never authentication: the agent side cannot verify any origin, and
  grading never depends on the label.

Then:

```bash
gaceta agent since --reader main --baseline /sandbox/baseline.json
gaceta agent since --reader main            # no baseline yet → a first launch
```

The answer classifies, per story, against the export's evidence:

- **baseline.status** — `known` (your stated version matches the paper's
  write-up history), `unknown` (it doesn't: a version ahead of the paper, a
  foreign memory, or no version evidence at all — no diff is claimed), or
  `none_stated` (you claim nothing; only trace evidence against your date is
  compared). A story id not in the public edition is never confirmed or
  denied — it may be outside the window or non-public.
- **intervening editions** — the public editions BUILT between your stated
  baseline date and the current one, from the export's build trace:
  publication chronology only. It is never evidence that any edition was
  skipped, unread or undelivered — readership of any edition is unknown to
  this surface, which holds no reading history and says nothing about
  delivery or access cycles. Private builds are invisible by design. No
  date stated → explicit unknown (and none is inferred from a version, a
  trace time, or the edition that contained an earlier version). The
  pre-v2 name `skipped_editions` is retained, deprecated, and always
  null; the brief carries `brief_version` so clients can see which
  contract they are reading.
- **change.status** — `material_change`: evidence-supported proposals, each
  carrying the public source excerpt/reference behind it (the change note
  itself is model text, marked as such); `no_supported_update`: the paper's
  change pass assessed the newer write-up as rewording/**duplicate coverage**
  only (which coverage it judged duplicate is listed), or no trace evidence
  exists after your point; `unknown`: **missing evidence** — a rewrite with
  no change note on record, or coverage no write-up has assessed yet (listed
  factually, no materiality claimed); `new_story`: first entered the paper's
  trace after your baseline date.

**Proposal to approval.** Every material note is a *proposal*, never a
"confirmed" claim (none exists in this data). Approval is the reader's own
act: they read the cited sources, and the agent then records its new
baseline — this edition's date and each story's current version from
`agent edition` — for the next launch. The paper's own 'Visto' marks stay on
the owner side; this surface never writes, so the brief cannot record your
approval for you.

**Time honesty.** The brief's `semantics` block pins the three clocks.
Comparisons use your stated version or the paper's trace times
(`added`/`written_at`/correction times). A source's `published` time is
carried for transparency but never compared — a long-published article
ingested yesterday counts as new coverage; a future-dated one ingested before
your baseline never surfaces. And because the export carries no per-person
marks, the brief can never tell *when anyone actually read anything* — it
only grades what *you* stated, as far as the evidence supports it.

## A person's own marks → their baseline receipt (owner, opt-in)

The paper's 'Visto' marks are reading history, and reading history never
rides with the public bundle: `agent export` never opens the `seen` table,
so the export is the same file whether or not anyone has read anything.
But a person who reads the paper through the local UI may want their OWN
memory back — "which write-up did I actually see?" — to seed their agent
session's baseline. `gaceta agent receipt` is that bridge, and it is
opt-in and owner-side on purpose:

```bash
gaceta agent receipt --reader main --by alice --story <story-id> > alice-<story-id>.json
```

It consults exactly one row — this reader, this person, this story — and
maps the mark's rev to the write-up version that state actually was. The
mapping is verified, not guessed: the rev is what the page carried, so a
version is stated only when the paper's own history reproduces that exact
rev; anything else is `unknown` and the receipt states nothing (exit stays
0; stderr says why). The receipt on stdout is a baseline fragment the
existing launch path already accepts:

```json
{"reader": "main", "versions": {"<story-id>": 1}, "provenance": {"versions": {"<story-id>": "verified_receipt"}}}
```

No person name, no mark timestamp, not even the rev — and no date: the
`seen` table records marks, not which edition was read, so the brief
answers the intervening-editions question with its honest unknown instead
of inventing one. The stated version carries its provenance
(`verified_receipt`) so the brief can say where the caller states the
fact came from — a label, never authentication. Public stories only: a
household or private story refuses (exit 2), because the agent surface
could never grade it anyway, so its id never travels. Hand the file to
the person it belongs to; they (or their agent session) pass it like any
baseline:

```bash
gaceta agent since --reader main --baseline alice-<story-id>.json
```

**Consent and the host boundary.** The marks are the person's reading, so
the receipt is their memory, not the host's content. The host runs the
command when the person asks for their own history back, and hands THEM
the file — it is never published with the paper, never merged into an
export, and gaceta itself writes nothing (stdout only; the derivation
mutates nothing). One person at a time, one story at a time: another
person's mark never answers (even on the same story), and another
reader's rows are never touched. Once handed over, the receipt is graded
like any caller-stated baseline — the brief still checks the claim
against the export's evidence and says `unknown` wherever it cannot. The
`verified_receipt` provenance label rides along for continuity, and it
buys nothing: it is the caller's statement of origin, never
authentication, and the evidence check is the same one every claim gets.

## Claude Code

Claude Code auto-discovers skills from `SKILL.md` files. Install it personally
(`~/.claude/skills/`) or per-project (`.claude/skills/` in a repo the session
works in):

```bash
mkdir -p ~/.claude/skills/gaceta-public-reader
cp docs/agent/SKILL.md ~/.claude/skills/gaceta-public-reader/SKILL.md
```

Then start a new session (skills load at startup). Tell the session the reader
id and where the export lives, or set `GACETA_DATA` so the conventional path
resolves. The skill's frontmatter already limits tools to the two read
commands; if you use the wrapper, point the commands at it instead.

## Hermes

Hermes reads the same convention: one directory per skill with a `SKILL.md`.
Drop the portable skill in wherever your Hermes looks for skills
(`~/.hermes/skills/` by default on this machine):

```bash
mkdir -p ~/.hermes/skills/gaceta-public-reader
cp docs/agent/SKILL.md ~/.hermes/skills/gaceta-public-reader/SKILL.md
```

If your Hermes configuration differs (custom skills root, skill allowlist),
register the same file there; the skill is plain markdown with YAML
frontmatter and no harness-specific parts. Give the session the same three
things as any harness: the export path (or `GACETA_DATA`), the reader id, and
— if Hermes lets you pin allowed commands — pin `gaceta agent edition` /
`gaceta agent story` / `gaceta agent since` (the wrapper shim above works
here too).

## Trying it with no data at all

`docs/agent/demo/` holds a synthetic demo export (invented stories, no real
data) and the generator that writes it — regenerate it if a read refuses the
checked-in file as stale.

## Limits

This surface will never: expose private or household stories, expose taste
signals or reactions, write anything (no seen marks, no saves, no sends),
serve an export whose schema it does not know, serve another reader's export,
serve stale data silently, or hand out a "confirmed" status — links and model
summaries are evidence-graded at most. Writes remain where they belong: the
owner's CLI, the local web UI's owner-only endpoints, and the `[agent]`
send-and-record channel.

The launch brief also has honest, known limits — it reports them in the
payload rather than papering over them:

- **Date-only baselines see whole days.** Without per-story versions,
  same-day events are invisible to the comparison; a baseline that states the
  write-up version catches story-level change regardless.
- **Coverage without a write-up assessment stays `unknown`.** When a source
  joins a story and no rewrite has assessed it, the brief lists the source
  and refuses to call the change material. It resolves when the editor's
  change pass next runs and writes a `what_changed` note ('' = duplicate
  coverage, text = material).
- **A correction recorded after the newest write-up re-proposes.** A
  version-only baseline cannot confirm you saw it, so it rides every brief
  (alone, with its evidence) until a newer write-up version exists.
- **`story_items` has no join time.** The paper itself uses `ingested_at` as
  the stand-in for when a source joined (same stand-in as the editor's change
  pass), so a support list can be off when an older item clustered late. The
  brief grades only from the evidence the export carries.
- **Only what is in the edition window.** Stories outside the 14-day window
  (or non-public ones) are invisible and never confirmed or denied.
- **Never reading history.** No per-person `seen` mark ever leaves the owner
  side into the public bundle, so the brief cannot say when anyone actually
  read anything — it grades only the caller's stated baseline, as far as
  the evidence supports it, and says `unknown` everywhere else. The one,
  consented exception is a person's own receipt (`gaceta agent receipt`,
  owner-side and opt-in: a verified version, no raw name or timestamp,
  handed to that person and never merged into the export).
