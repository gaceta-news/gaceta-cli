# gaceta skill — packaged for Codex (inside the gaceta plugin, `skills/gaceta/`)

Generated from `packaging/catalog.json` + `packaging/templates/` at candidate
**0.3.0b3** by `scripts/build-packages.py`. Do not edit by hand: edit the
catalog/templates and rebuild, so every surface stays in parity (CI checks
this with `scripts/check-parity.py`).

- **Skill:** Find what changed in stories from the reader's feeds.
- **Runtime:** read-only `gaceta agent edition|story|since` over one
  public-only export; nothing else. The unchanged discipline text from
  `docs/agent/SKILL.md` is embedded in `SKILL.md`.
- **Home:** inside the plugin; enabled and removed with the plugin itself
- **Engine (not included):** uv tool install git+https://github.com/gaceta-news/gaceta-cli@v0.3.0b3
- **License:** AGPL-3.0-only · **Source:** https://github.com/gaceta-news/gaceta-cli · **Issues:** https://github.com/gaceta-news/gaceta-cli/issues

## Files

```
SKILL.md                     the skill (frontmatter + discipline + demo)
README.md                    this file
scripts/make_agent_demo.py   regenerates the synthetic demo export into a
                             directory you own (no network, no model calls)
scripts/make_demo_export.py  byte-identical copy of docs/agent/demo/
                             make_demo_export.py — the demo generator
assets/demo-baseline.json    version-only example memory (demo-trail v1)
```

## Try it offline (0.3.0b3, synthetic data)

```bash
python scripts/make_agent_demo.py WORKDIR
gaceta agent edition --reader demo --path WORKDIR/export.json
gaceta agent story   --reader demo --id demo-aurora --path WORKDIR/export.json
gaceta agent since   --reader demo --baseline assets/demo-baseline.json \
    --path WORKDIR/export.json
```

No export ships in this package on purpose: exports carry a 48-hour freshness
gate, so a pre-built file would arrive stale. The generator writes a fresh
one in seconds. Nothing here contains real coverage or anyone's private data.

## Uninstall

Remove this skill directory with the plugin (`codex plugin remove gaceta`). The engine, if you installed
it too: `uv tool uninstall gaceta`, then remove its data directory (`$GACETA_DATA`).

