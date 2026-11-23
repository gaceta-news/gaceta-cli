#!/usr/bin/env bash
# gaceta release: tag and publish the version that is ALREADY on main.
#
# The version lives in src/gaceta/__init__.py and changes only through a PR
# (main is protected). This script never edits files; it refuses unless:
#   1. we are on main, the tree is clean and HEAD == origin/main;
#   2. CI passed on this exact commit (GitHub check runs, all success);
#   3. the requested version == __version__ and the tag does not exist yet;
#   4. `make check` passes and the release smoke (clean wheel + offline
#      edition) passes from this tree;
#   5. check-parity --release passes at the freshly made local tag.
# Only then it pushes the tag and creates the GitHub Release (notes = commits
# since the previous tag). A pre-release version becomes a GitHub pre-release.
#
# Usage: scripts/release.sh 0.3.0b1 [--dry-run]
#   --dry-run runs every check, then deletes the local tag and stops.
set -euo pipefail

VERSION="${1:?usage: scripts/release.sh VERSION [--dry-run]}"
DRY="${2:-}"
TAG="v$VERSION"
REPO="gaceta-news/gaceta-cli"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export TZ=UTC

die() { echo "release: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

step "1. branch, tree, origin"
[[ "$(git rev-parse --abbrev-ref HEAD)" == main ]] || die "not on main"
[[ -z "$(git status --porcelain)" ]] || die "tree not clean"
git fetch -q origin main --tags
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || die "HEAD != origin/main (pull or push first)"
SHA="$(git rev-parse HEAD)"

step "2. CI on $SHA"
RUNS="$(gh api "repos/$REPO/commits/$SHA/check-runs" -q '[.check_runs[] | {n:.name,s:.status,c:.conclusion}]')"
python3 - "$RUNS" <<'PY' || die "CI is not green on this commit"
import json, sys
runs = json.loads(sys.argv[1])
need = {"test (3.12)", "test (3.13)"}
seen = {r["n"]: r for r in runs}
missing = need - seen.keys()
bad = [r for r in runs if r["s"] != "completed" or r["c"] not in ("success", "skipped")]
for r in runs: print(f"  {r['n']}: {r['s']}/{r['c']}")
if missing: print("  missing:", ", ".join(sorted(missing)))
sys.exit(1 if missing or bad else 0)
PY

step "3. version"
PKG="$(python3 -c 'import re;print(re.search(r"__version__\s*=\s*\"([^\"]+)\"",open("src/gaceta/__init__.py").read())[1])')"
[[ "$PKG" == "$VERSION" ]] || die "__version__ is $PKG, asked for $VERSION (bump it in a PR first)"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists"
PREV="$(git describe --tags --abbrev=0 --match 'v*' HEAD 2>/dev/null || true)"
echo "  $TAG (previous: ${PREV:-none})"

step "4. full suite + release smoke"
make check
bash scripts/smoke-release.sh

step "5. tag parity"
git tag -a "$TAG" -m "gaceta $VERSION"
if ! python3 scripts/check-parity.py --release; then git tag -d "$TAG" >/dev/null; die "parity failed at $TAG"; fi

if [[ "$DRY" == "--dry-run" ]]; then
  git tag -d "$TAG" >/dev/null
  echo; echo "dry run OK: $TAG would be released from $SHA"; exit 0
fi

step "6. publish"
NOTES="$(mktemp)"
{
  echo "gaceta $VERSION, from \`$SHA\`."
  echo
  echo "Install: \`uv tool install git+https://github.com/$REPO@$TAG\`"
  echo
  echo "## Changes${PREV:+ since $PREV}"
  git log --no-merges --format='- %s' ${PREV:+$PREV..}HEAD
} > "$NOTES"
git push origin "$TAG"
PRE=""; [[ "$VERSION" =~ (a|b|rc)[0-9]+$ ]] && PRE="--prerelease"
gh release create "$TAG" -R "$REPO" --title "gaceta $VERSION" --notes-file "$NOTES" --verify-tag $PRE \
  packaging/dist/archives/*.zip
echo; echo "released $TAG"
