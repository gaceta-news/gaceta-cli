#!/usr/bin/env bash
# gaceta packaged-install test: a clean wheel install OUTSIDE the checkout,
# the PACKAGED skills' demo through the installed CLI, archive integrity,
# and — when a stock codex CLI is present — a sandboxed stock
# marketplace/plugin round trip. No test-only adapters anywhere: every
# client step is a stock command a real user would run.
#
# Proves, in order:
#   1. uv.lock still exports --locked from pyproject.toml.
#   2. The wheel builds from this tree and installs into a throwaway venv
#      with the locked pins.
#   3. Version parity on the installed artifact: `gaceta --version` and the
#      wheel METADATA both equal the single source (src/gaceta/__init__.py).
#   4. The wheel ships the agent-demo generator and ships NO pre-built
#      export.json (the 48 h staleness rule).
#   5. The demo works from the PACKAGED skill files (packaging/dist, not
#      docs/): generator -> fresh export; the three read-only commands run
#      against it with --path only; the version-only baseline brief shows
#      the evidence-backed material change and the honest unknowns.
#   6. The companion archives unpack to trees identical to packaging/dist.
#   7. Codex stock client (only stock commands, no experimental features,
#      no adapters): sandboxed CODEX_HOME, marketplace add -> plugin add ->
#      list shows installed+enabled at the candidate version -> plugin
#      files under CODEX_HOME -> remove plugin -> remove marketplace.
#      SKIPPED with a clear line if no codex binary exists. Hermes: the
#      stock registry/hub install needs the public release source, so it is
#      reported as NOT exercised here — conventions/layout are covered by
#      scripts/check-parity.py and the demo above; no adapter fakes it.
#
# Isolation: one disposable scratch dir (arg 1 or a fresh mktemp); the repo,
# its .venv/ and ./data are never touched; the real ~/.codex is never
# touched (CODEX_HOME points into scratch); no network after the build
# steps, no credentials, no model calls.
#
# Usage: scripts/test-packaged-install.sh [SCRATCH_DIR]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH="${1:-$(mktemp -d -t gaceta-pkgtest-XXXXXX)}"
PY="${GACETA_SMOKE_PYTHON:-3.12}"
VENV="$SCRATCH/venv"
DIST="$REPO_ROOT/packaging/dist"
HS="$DIST/hermes-skill/research/gaceta"     # packaged Hermes skill
CS="$DIST/codex-skill/gaceta"               # packaged Codex USER-scope skill

if [ -e "$SCRATCH" ] && [ -n "$(ls -A "$SCRATCH")" ]; then
    echo "scratch dir is not empty (pass a fresh dir): $SCRATCH" >&2
    exit 2
fi
mkdir -p "$SCRATCH"
cd "$REPO_ROOT"

step() { printf '\n== %s\n' "$*"; }

step "scratch (disposable): $SCRATCH"
step "candidate version from the single source (src/gaceta/__init__.py)"
EXPECTED="$(python3 - <<'PYEOF'
import ast, pathlib
tree = ast.parse(pathlib.Path("src/gaceta/__init__.py").read_text())
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
        if any(isinstance(t, ast.Name) and t.id == "__version__"
               for t in node.targets):
            print(node.value.value)
PYEOF
)"
echo "expected: $EXPECTED"

step "pinned requirements from uv.lock (fails if the lock is stale)"
uv export --locked --no-dev --no-emit-project -o "$SCRATCH/requirements.txt"

step "build the wheel from this tree"
uv build -o "$SCRATCH/dist" "$REPO_ROOT"
WHEEL="$(ls "$SCRATCH/dist"/gaceta-*.whl)"
echo "built: $(basename "$WHEEL")"

step "fresh isolated venv (python $PY) + wheel + pinned deps"
uv venv --python "$PY" "$VENV"
uv pip install --python "$VENV/bin/python" \
    -r "$SCRATCH/requirements.txt" "$WHEEL"
G="$VENV/bin/gaceta"

step "installed version parity (CLI flag, wheel METADATA, single source)"
GOT="$("$G" --version)"
[ "$GOT" = "$EXPECTED" ] || { echo "gaceta --version said '$GOT', expected '$EXPECTED'" >&2; exit 1; }
echo "gaceta --version -> $GOT"
META="$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("gaceta"))')"
[ "$META" = "$EXPECTED" ] || { echo "wheel METADATA says '$META', expected '$EXPECTED'" >&2; exit 1; }
echo "wheel METADATA  -> $META"

step "wheel contents: demo generator shipped, no pre-built export.json"
python3 - "$WHEEL" <<'PYEOF'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as zf:
    names = zf.namelist()
assert "gaceta/assets/agent_demo/make_demo_export.py" in names, \
    "wheel does not ship the agent-demo generator"
stale = [n for n in names if n.endswith("/export.json") or n == "export.json"]
assert not stale, f"wheel ships pre-built export(s) (48 h staleness): {stale}"
print("wheel ships gaceta/assets/agent_demo/make_demo_export.py; no export.json")
PYEOF

step "packaged demo (dist skill files, NOT docs/) — outside the checkout"
cd "$SCRATCH"
cmp -s "$HS/scripts/make_demo_export.py" "$CS/scripts/make_demo_export.py" \
    || { echo "hermes/codex skill demo generators differ" >&2; exit 1; }
echo "hermes and codex skill copies of the demo generator are byte-identical"
python3 "$HS/scripts/make_agent_demo.py" "$SCRATCH/work" >/dev/null
[ -s "$SCRATCH/work/export.json" ] || { echo "demo generator wrote no export" >&2; exit 1; }
echo "make_agent_demo.py -> work/export.json (fresh, synthetic)"

"$G" agent edition --reader demo --path "$SCRATCH/work/export.json" > edition.json
"$G" agent story --reader demo --id demo-aurora \
    --path "$SCRATCH/work/export.json" > story.json
"$G" agent since --reader demo --baseline "$HS/assets/demo-baseline.json" \
    --path "$SCRATCH/work/export.json" > since.json
"$G" agent since --reader demo --path "$SCRATCH/work/export.json" > first.json
python3 - <<'PYEOF'
import json
edition = json.load(open("edition.json"))
story = json.load(open("story.json"))
since = json.load(open("since.json"))
first = json.load(open("first.json"))
ids = {s["id"] for s in edition["stories"]}
assert edition["edition"]["stories_total"] == 3, edition["edition"]
assert {"demo-aurora", "demo-trail", "demo-quiet"} <= ids, ids
assert len(story["story"]["sources"]) == 2, story["story"]["sources"]
assert len(story["story"]["corrections"]) == 1, story["story"]["corrections"]
updates = {u["story_id"]: u for u in since["updates"]}
trail = updates["demo-trail"]
assert trail["baseline"]["status"] == "known", trail["baseline"]
assert trail["change"]["status"] == "material_change", trail["change"]
assert len(trail["change"]["proposals"][0]["support"]) == 2, \
    trail["change"]["proposals"][0]
aurora = updates["demo-aurora"]
assert aurora["change"]["status"] == "unknown", aurora["change"]
assert "skipped_editions_note" in since, "version-only baseline must keep the honest unknown"
assert "semantics" in since and "semantics" in first
print("edition: 3 stories | demo-aurora: 2 sources, 1 correction")
print("since (baseline demo-trail v1): known -> material_change with 2 "
      "supporting sources; demo-aurora: unknown; skipped editions: honest note")
print("first launch (no baseline): exit 0")
PYEOF

step "companion archives unpack identical to packaging/dist"
mkdir -p "$SCRATCH/unpack"
unzip -q "$DIST/archives/gaceta-hermes-skill-v$EXPECTED.zip" -d "$SCRATCH/unpack/hermes"
diff -r "$SCRATCH/unpack/hermes/research/gaceta" "$HS" >/dev/null \
    || { echo "hermes archive differs from dist tree" >&2; exit 1; }
unzip -q "$DIST/archives/gaceta-codex-skill-v$EXPECTED.zip" -d "$SCRATCH/unpack/codex"
diff -r "$SCRATCH/unpack/codex/gaceta" "$CS" >/dev/null \
    || { echo "codex skill archive differs from dist tree" >&2; exit 1; }
unzip -q "$DIST/archives/gaceta-codex-plugin-v$EXPECTED.zip" -d "$SCRATCH/unpack/plugin"
diff -r "$SCRATCH/unpack/plugin" "$DIST/codex-plugin" >/dev/null \
    || { echo "codex plugin archive differs from dist tree" >&2; exit 1; }
echo "all three archives == their dist trees (byte-identical)"

step "codex stock client: sandboxed marketplace/plugin round trip (no experimental features)"
if ! command -v codex >/dev/null 2>&1; then
    echo "SKIP: no codex binary on PATH (stock client test needs codex-cli >= plugin support)"
else
    export CODEX_HOME="$SCRATCH/codex-home"
    mkdir -p "$CODEX_HOME"
    out="$(codex plugin marketplace add "$DIST/codex-marketplace" 2>&1)"
    grep -q "Added marketplace \`gaceta-news\`" <<<"$out" \
        || { echo "codex marketplace add failed: $out" >&2; exit 1; }
    echo "$out"
    out="$(codex plugin add gaceta@gaceta-news 2>&1)"
    grep -q "Added plugin \`gaceta\`" <<<"$out" \
        || { echo "codex plugin add failed: $out" >&2; exit 1; }
    echo "$out"
    out="$(codex plugin list 2>&1)"
    grep -qE "gaceta@gaceta-news +installed, enabled +$EXPECTED" <<<"$out" \
        || { echo "codex plugin list did not show installed+enabled $EXPECTED: $out" >&2; exit 1; }
    echo "codex plugin list: gaceta@gaceta-news installed, enabled $EXPECTED"
    test -f "$CODEX_HOME/plugins/cache/gaceta-news/gaceta/$EXPECTED/skills/gaceta/SKILL.md" \
        || { echo "installed plugin skill file missing under CODEX_HOME" >&2; exit 1; }
    echo "skill landed: \$CODEX_HOME/plugins/cache/gaceta-news/gaceta/$EXPECTED/skills/gaceta/SKILL.md"
    codex plugin remove gaceta@gaceta-news >/dev/null
    codex plugin marketplace remove gaceta-news >/dev/null
    echo "codex plugin + marketplace removed cleanly; real ~/.codex untouched (CODEX_HOME sandbox)"
fi

step "hermes stock client: honestly NOT exercised here"
echo "SKIP (by design): 'hermes skills install/inspect' only accepts registry"
echo "refs or HTTPS URLs to a public SKILL.md — it needs the pinned public"
echo "release source, so it is a release-time check (P5 cleanroom), not"
echo "something this offline test fakes with an adapter. Conventions,"
echo "layout and the 60-char description limit are enforced by"
echo "scripts/check-parity.py; the demo above ran through the installed wheel."

printf '\nPACKAGED INSTALL OK — candidate %s\n' "$EXPECTED"
echo "  wheel + CLI + METADATA parity: $EXPECTED"
echo "  packaged demo: 3 read-only commands OK (synthetic export, zero model calls)"
echo "  archives: 3/3 byte-identical to dist"
printf 'scratch kept for inspection, dispose freely: %s\n' "$SCRATCH"
