# Listing draft — Hermes skills registry/hub (gaceta)

Status: **DRAFT — not submitted anywhere.** Registry/hub acceptance,
indexing and search are external dependencies; submission happens only
after the P5 gates pass, from the pinned public revision. This file records
exactly what would be listed, from the single metadata source
(`packaging/catalog.json`, candidate 0.3.0b5).

## Listing fields

| Field | Value |
|---|---|
| Identifier (registry) | `gaceta-news/gaceta` (repo tap: `hermes skills tap add gaceta-news/gaceta-cli`) |
| Name | `gaceta` (formerly `gaceta-public-reader`) |
| Description | Find what changed in stories from the reader's feeds. |
| Category | `research` (category placement is a listing decision; `research` matches the reading/feeds domain) |
| Tags | news, feeds, reading, change-tracking, citations |
| Version | `0.3.0b5` (stamped from `gaceta.__version__`; CI rejects drift) |
| Author | Gaceta News (gaceta-news) |
| License | AGPL-3.0-only |
| Platforms | linux, macos, windows |
| Source | https://github.com/gaceta-news/gaceta-cli |
| Issues | https://github.com/gaceta-news/gaceta-cli/issues |

## Long description (for the listing page)

Read-only lookup over a versioned, public-only JSON export of a gaceta
reader's edition: what is in today's paper, one story with its sources and
corrections, and an honest what-changed-since-you-last-read brief that
grades only the baseline your session stated. The export is public-only by
construction, refuses to serve after 48 hours, and the three commands
(`gaceta agent edition|story|since`) make no model calls. The skill does
not install or build the paper — the owner runs the gaceta engine on their
own machine; a bundled synthetic demo lets an agent try the surface with
zero data.

## Install instructions (for the listing)

From a checkout/tag of v0.3.0b5: unzip `packaging/dist/archives/gaceta-hermes-skill-v0.3.0b5.zip`
into `~/.hermes/skills/` (creates `research/gaceta/`), or:

```bash
mkdir -p ~/.hermes/skills/research
unzip gaceta-hermes-skill-v0.3.0b5.zip -d ~/.hermes/skills/
```

Engine (separate, pinned): `uv tool install git+https://github.com/gaceta-news/gaceta-cli@v0.3.0b5`

Direct registry/HTTPS install (once the release is public and listed):
`hermes skills install <listing url for SKILL.md>`

## Verification (what the listing may claim)

Validated: frontmatter against the stock Hermes skills conventions
(name/description/version/author/license/platforms, description ≤ 60 chars
one sentence), layout (one directory, `SKILL.md` + `scripts/` + `assets/`),
and the packaged offline demo end to end on a clean installed wheel
(gaceta 0.3.0b5 (P4 beta candidate; the v-tag is cut at release review — published 0.2.x tags predate these features, so 0.2.0 is not a valid pin for them)). Not claimed: blind discovery, organic search ranking,
configured usage, reader retention. Not tested end to end until the release
source is public: `hermes skills install` from the registry/HTTPS path
itself.

