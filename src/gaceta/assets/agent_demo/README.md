# gaceta.assets.agent_demo — the synthetic agent demo, shipped in the wheel

`make_demo_export.py` in this directory is a byte-identical copy of
`docs/agent/demo/make_demo_export.py` (the single source; CI verifies the
two stay identical with `scripts/check-parity.py`). It is shipped as wheel
data so an installed gaceta — outside any checkout — can try the read-only
agent surface offline:

```bash
python "$(python -c 'from importlib.resources import files; print(files("gaceta") / "assets" / "agent_demo" / "make_demo_export.py")')" OUT_DIR
# → OUT_DIR/export.json, fresh, public-only, fully synthetic
gaceta agent edition --reader demo --path OUT_DIR/export.json
gaceta agent story   --reader demo --id demo-aurora --path OUT_DIR/export.json
gaceta agent since   --reader demo --path OUT_DIR/export.json     # first launch
```

Every story, outlet, author, event, and URL in the demo is invented
(`*.example` hosts, `FAKE` video ids). No real coverage, no private data,
no network, no model calls. The edition clock is read at generation time,
so a freshly generated export is never stale — which is why no pre-built
`export.json` ships here or in any package.

A baseline example for the `since` brief lives beside the packaged skills
(`assets/demo-baseline.json` in `packaging/dist/*/…/assets/`).
