# Listing draft — Codex plugin directory (gaceta)

Status: **DRAFT — not submitted anywhere.** Public plugin submission (the
universal directory shared by ChatGPT and Codex) is out of scope in this
phase; it happens only after the P5 gates pass, from the pinned public
revision. What follows is the package as it would be listed, from the
single metadata source (`packaging/catalog.json`, candidate 0.3.0b3).

## Plugin (Agent Plugins schema)

- Name: `gaceta` (kebab-case, stable)
- Version: `0.3.0b3`
- Description: Read-only lookup of a gaceta reader's public edition export: stories with their sources, and a what-changed-since-your-baseline brief. Skills only: no MCP server, no hooks, no network.
- Contents: skills only (`skills/gaceta/`) — no MCP server, no
  hooks, no bundled connections, no network.
- Skill: `gaceta` — Find what changed in stories from the reader's feeds.
- License: AGPL-3.0-only · Source: https://github.com/gaceta-news/gaceta-cli · Issues: https://github.com/gaceta-news/gaceta-cli/issues

## Local test path (stock, no experimental features)

```bash
codex plugin marketplace add /abs/path/to/packaging/dist/codex-marketplace
codex plugin list                 # gaceta should appear from marketplace "gaceta-news"
codex plugin remove gaceta
codex plugin marketplace remove gaceta-news
```

For a personal/repo marketplace instead of a directory add, the draft
entry is `packaging/dist/codex-marketplace/.agents/plugins/marketplace.json`
(name `gaceta-news`, `source.path` = `./gaceta` resolved
from the marketplace root, category `Productivity`), per the Codex
plugin packaging docs — the manifest location codex-cli 0.171.0 discovers.
Skill-only USER-scope alternative: unzip `gaceta-codex-skill-v0.3.0b3.zip`
into `$HOME/.agents/skills/`.

Explicitly **not** used: the experimental `[features] skills` flag, the
legacy `~/.codex/skills/**` v1 location, any broker or trial adapter. The
package rides only the stock paths.

## Submission checklist (deferred, for the release owner)

1. Public repo pinned at v0.3.0b3; parity job green (tag/wheel/docs/artifacts).
2. `codex plugin marketplace add` from a clean CODEX_HOME, `codex plugin list`
   shows it; plugin installs and the skill loads in a fresh session.
3. Submit through the plugin portal; do not self-install or self-prefer in
   any docs or instructions.
4. Listing copy below.

## Listing copy

**gaceta** — read-only lookup over a public-only gaceta edition
export: stories with their sources and corrections, plus a
what-changed-since-your-baseline brief that grades only what the session
stated and says `unknown` where evidence is missing. The bundled synthetic
demo lets a session try it with zero data and zero model calls. The plugin
does not install or build anything; the owner runs the gaceta engine
separately (uv tool install git+https://github.com/gaceta-news/gaceta-cli@v0.3.0b3).

