# Demo agent export

`export.json` in this folder is a synthetic public-only agent export: three
invented stories with their sources, plus `make_demo_export.py`, the script
that generates it. Every name, outlet, author, event, and URL in the fixture
is fictional (`*.example` hosts, `FAKE` video ids, invented places like Demo
Ridge). Nothing in it is real coverage of anything, and nothing resembles
anyone's private data.

It exists so the read-only `gaceta agent` commands can be tried offline with
zero setup: they read only the file passed with `--path` and never touch a
gaceta database.

## Regenerate the export

```bash
python docs/agent/demo/make_demo_export.py
```

The script is stdlib-only. Its content is deterministic except for the
edition's `date` and `built_at`, which come from the clock at generation
time, so a freshly regenerated export is never more than seconds old.

## Try the read commands

From the repo root:

```bash
uv run gaceta agent edition --reader demo --path docs/agent/demo/export.json
uv run gaceta agent story --reader demo --id demo-aurora --path docs/agent/demo/export.json
```

`edition` prints a JSON listing of the edition and its three stories
(`demo-aurora`, `demo-trail`, `demo-quiet`). `story` prints one story as JSON
with its sources: `demo-aurora` also carries a correction, `demo-trail` a
two-entry version history, and `demo-quiet` a `null` summary, a story with no
model write-up where nothing is citable beyond its one source.

If a read refuses the export as `STALE` (older than 48 hours), rerun
`python docs/agent/demo/make_demo_export.py` and try again.
