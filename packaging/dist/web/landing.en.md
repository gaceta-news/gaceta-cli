# gaceta — what changed in your stories since you last read them

> One canonical page, two languages: **English** · [Español](landing.es.md)
> Draft — candidate 0.3.0b3 · single source: `packaging/catalog.json`
> (rendered by `scripts/build-packages.py`, CI-checked by `scripts/check-parity.py`)

## The recurring job

You follow a handful of feeds. The same story arrives five times in one
morning — two outlets report it, one translates the first, two repeat it. You
read it anyway, because the one story that *actually changed* is in there
somewhere. gaceta does that sorting for you: items from the feeds you already
follow become one **story** with its sources, and when a story you already
read changes, the paper says what changed — and never invents when you read
it.

## A real output

From the packaged offline demo (synthetic data — invented stories, `*.example`
hosts; no network, no model calls). A session stated a version-only memory
("I last saw `demo-trail` write-up v1") and asked what changed:

```json
"skipped_editions_note": "unknown: your baseline states no edition date, so
  this surface cannot say which public editions you have no record of (trace
  times are never used as a stand-in for reading history)",
"updates": [
  {
    "story_id": "demo-trail",
    "title": "Demo Loop trail reopens after boardwalk repairs",
    "baseline": {
      "claimed_version": 1,
      "status": "known",
      "reason": "baseline version 1 of 2 matches the paper's write-up history"
    },
    "current_version": 2,
    "change": {
      "status": "material_change",
      "reason": "evidence-supported change since your baseline; each proposal
        carries the public sources behind it",
      "proposals": [
        {
          "kind": "proposed_update",
          "version": 2,
          "what_changed": "Added the walker's note; the detour sign now has a
            stated end condition.",
          "what_changed_origin": "model",
          "support": [
            {
              "title": "Demo Loop boardwalk repairs finished",
              "url": "https://trails.example/demo-loop-reopens",
              "kind": "rss", "role": "origin",
              "published": "2026-11-15T08:30:00+01:00"
            },
            {
              "title": "Walk report: Demo Loop on the first dry weekend",
              "url": "https://notes.example/notes/b66e8a0f1ec9",
              "kind": "nostr", "role": "update",
              "published": "2026-11-17T09:00:00+01:00"
            }
          ]
        }
      ]
    }
  }
]
```

Reproduce it: `python scripts/make_agent_demo.py WORKDIR` from a packaged
skill, then `gaceta agent since --reader demo --baseline … --path WORKDIR/export.json`.

## When NOT to adopt it

- **You don't already follow feeds.** gaceta reads yours (RSS/Atom, YouTube
  channels, Nostr); it is not a news app with its own editorial sources.
- **You want a hosted product with accounts.** It runs on your machine,
  CLI-first; there is no service to log in to.
- **You need breaking-news alerts.** It is a daily paper: stories settle, and
  a rerun only rewrites what changed.
- **You want a shared or global ranking.** Editions, reactions and taste are
  per reader by design; nothing ranks you against anyone.
- **You just want a one-off article summarized.** Any assistant does that;
  this adds nothing unless a reader's paper feeds your sessions.
- **You can't run Python ≥ 3.12 or an editor model.** Real write-ups
  need a model (local Ollama or any OpenAI-compatible endpoint). The offline
  demo proves plumbing with fake models, nothing more.
- **An agent skill is not the product.** The Hermes/Codex skill below is a
  read-only lens over an export your machine produces; installing the skill
  alone installs no paper and builds nothing.

## What you need, and what it costs

- Python ≥ 3.12 + [uv](https://docs.astral.sh/uv/); the feeds you
  already follow; an editor model — fully local (Ollama: free, your CPU/time)
  or any OpenAI-compatible API (metered by your provider).
- **Hard budgets, not estimates:** `write` takes `--max-llm-calls` and
  `--max-change-calls`; clustering is local embeddings; unchanged stories
  aren't rewritten. A paper with zero changes spends zero editor calls.
- **The agent lookup is free:** `gaceta agent edition|story|since` read one
  JSON file — no model calls at all.

## Install (pinned)

```bash
uv tool install git+https://github.com/gaceta-news/gaceta-cli@v0.3.0b3
gaceta --version        # must print 0.3.0b3
```

**v0.3.0b3 is the intended beta pin**, cut at release review — until the tag
exists publicly, this is the shape of the install, not a live URL. The
published 0.2.x tags are real but predate the agent-demo wheel assets,
`--version` and the packaged skills: pinning an install at 0.2.0 gets a
gaceta without this candidate's features. Then the two-minute offline
quickstart in the repo [README](https://github.com/gaceta-news/gaceta-cli) — sample feed, fake models, no
keys. For a real paper, point it at your feeds and your model.

## For agent clients (Hermes, Codex)

Skill description: **"Find what changed in stories from the reader's feeds."** — the read-only runtime is
exactly `gaceta agent edition`, `story` and `since`.

| Client | Package (this repo) | Stock install path |
|---|---|---|
| Hermes | `packaging/dist/hermes-skill/research/gaceta/` · zip in `packaging/dist/archives/` | unzip into `~/.hermes/skills/` (registry/hub listing is a later, separate step) |
| Codex | `packaging/dist/codex-plugin/` (skill-only plugin) · `packaging/dist/codex-marketplace/` for local testing | `$HOME/.agents/skills/` (USER scope) or `codex plugin marketplace add <dir>`; stock paths, no experimental features |

Both packages carry the demo above, so an agent can be tried with zero data.
**Status, stated plainly:** packaging and the offline demo are what we have
exercised. No blind-discovery, catalog-indexing or configured-usage result is
claimed for any client; those are separate, pre-registered evidence.

## Boundaries

- The agent surface is **read-only** and **public-only by construction**:
  exports derive from a `--max-privacy public` edition and are re-checked;
  household/private stories, taste signals and reactions never travel.
- Exports refuse to serve after 48 hours — an agent reading last month's
  export fails closed instead of answering.
- Reading history is never invented: the brief grades only what a session
  *stated*, says `unknown` where evidence is missing, and a person's own
  seen-marks come back only via an opt-in, owner-side receipt.
- No hosted accounts, no shared ranking, no third-party analytics, no
  telemetry. Keys are read from the environment, never stored in config.
- Sharing (when the recipe candidate merges) defaults to **share nothing**;
  a recipe carries only feeds and labels the owner explicitly marked public.
- A trial (when the trial candidate merges) is bounded: at most three
  editions, hard call/token ceilings, expiry in calendar days, and nothing
  that renews itself.

## Tested with (packaging smoke only)

| What | Result |
|---|---|
| Candidate | gaceta 0.3.0b3 (P4 beta candidate; the v-tag is cut at release review — published 0.2.x tags predate these features, so 0.2.0 is not a valid pin for them) — `gaceta --version`, wheel METADATA and every artifact stamp agree |
| Codex | codex-cli 0.171.0 — stock `codex plugin marketplace add` + `codex plugin list` on a sandboxed CODEX_HOME; no experimental features or adapters enabled |
| Hermes | Hermes Agent v0.23.2 — packaged skill validated against the stock skills conventions (frontmatter, layout, description limit); registry/hub install is exercised only once the release source is public |
| Python | 3.12 (floor of requires-python) |

Catalog acceptance and indexing are external dependencies, not CI
assumptions. Cold GitHub install and the tag/wheel/docs parity record are
release-check items, run from the pinned public revision when one exists.

## Uninstall

```bash
uv tool uninstall gaceta
rm -rf "$GACETA_DATA"          # the paper's own data dir; nothing else
```

Skills: remove `~/.hermes/skills/research/gaceta/` (Hermes) or
`~/.agents/skills/gaceta/` / `codex plugin remove gaceta`
(Codex). No background jobs exist to stop: the agent surface schedules
nothing.

## Links

- Source and issues: https://github.com/gaceta-news/gaceta-cli · https://github.com/gaceta-news/gaceta-cli/issues
- The agent surface contract: `docs/agent/README.md`
- The bilingual demo desk: `src/gaceta/assets/desk/index.html` (bilingual demo desk from the desk workstream; ships as a wheel asset — published URL pinned at release)
- Portable recipe preview: portable recipe (gaceta recipe export/inspect/import) is a parallel candidate (P2), not part of this candidate; the landing page links it once merged
- Finite trial: finite trial (gaceta trial preview/start/...) is a parallel candidate (P3), not part of this candidate; the landing page links it once merged
- This page's source (one catalog, two renderings): `packaging/templates/landing.en.md.tmpl`, `landing.es.md.tmpl`

*Draft status: this page is generated, not hand-maintained; publishing it —
and where — is a release decision after the P5 gates. Nothing on it is
telemetered.*

