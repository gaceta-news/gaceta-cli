#!/usr/bin/env bash
# gaceta release smoke: a fresh, pinned, isolated install of this tree, then
# the offline edition end to end.
#
# Proves, in order:
#   1. uv.lock is in sync with pyproject.toml and exports exactly (--locked).
#   2. The release distributions (sdist + wheel) build from this tree.
#   3. The wheel installs into a throwaway venv with exactly the locked pins.
#   4. The README offline quickstart runs end to end in a throwaway data dir
#      (fake providers, local sample feed: no network, no keys) and renders.
#
# Isolation: everything lands under one disposable scratch dir (arg 1, or a
# fresh mktemp dir); the repo's .venv/ and ./data are never touched, and
# GACETA_DATA is forced to the scratch dir. A non-empty scratch dir is
# refused, so every run is fresh.
#
# Stop rule: any failing command stops the run, and `write` must report
# "0 failed" — provider failures are never papered over.
#
# Usage: scripts/smoke-release.sh [SCRATCH_DIR]
#        GACETA_SMOKE_PYTHON pins the interpreter (default 3.12, the floor of
#        requires-python). The scratch dir is kept for inspection and is
#        disposable: delete it whenever.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH="${1:-$(mktemp -d -t gaceta-smoke-XXXXXX)}"
PY="${GACETA_SMOKE_PYTHON:-3.12}"
VENV="$SCRATCH/venv"
DATA="$SCRATCH/data"

if [ -e "$SCRATCH" ] && [ -n "$(ls -A "$SCRATCH")" ]; then
    echo "scratch dir is not empty (pass a fresh dir): $SCRATCH" >&2
    exit 2
fi
mkdir -p "$SCRATCH"
cd "$REPO_ROOT"

step() { printf '\n== %s\n' "$*"; }

step "scratch (disposable): $SCRATCH"

step "pinned requirements from uv.lock (fails if the lock is stale)"
uv export --locked --no-dev --no-emit-project -o "$SCRATCH/requirements.txt"

step "build release distributions from this tree"
uv build -o "$SCRATCH/dist" "$REPO_ROOT"

step "fresh isolated venv (python $PY) + wheel + pinned deps"
uv venv --python "$PY" "$VENV"
uv pip install --python "$VENV/bin/python" \
    -r "$SCRATCH/requirements.txt" "$SCRATCH/dist"/gaceta-*.whl
"$VENV/bin/python" -c 'from importlib.metadata import version; print("installed gaceta", version("gaceta"))'

step "offline edition in a throwaway data dir (fake providers, no keys)"
cd "$SCRATCH"               # even a stray ./data would land in scratch
export GACETA_DATA="$DATA"
G="$VENV/bin/gaceta"
"$G" init
"$G" ingest rss --reader main
"$G" embed --reader main
"$G" cluster --reader main
out="$("$G" write --reader main --days 14)"
printf '%s\n' "$out"
grep -q ' 0 failed (invalid JSON)' <<<"$out" \
    || { echo 'write reported write-up failures; stopping' >&2; exit 1; }
"$G" edition --reader main
"$G" render --reader main

step "the paper rendered"
SITE="$DATA/site/main/index.html"
test -s "$SITE"
grep -q 'La Gaceta' "$SITE"                        # masthead
grep -q 'Offline demo card (fake model)' "$SITE"   # fake-LLM write-ups
printf 'ok: %s (%s bytes)\n' "$SITE" "$(wc -c <"$SITE")"

printf '\nsmoke OK — scratch kept for inspection, dispose freely: %s\n' "$SCRATCH"
