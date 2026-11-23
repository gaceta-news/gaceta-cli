#!/usr/bin/env bash
# gaceta land: put a green PR on main WITHOUT GitHub's merge button.
#
# Why: any GitHub-side merge (merge, squash or rebase) re-stamps the commit with
# the merging account's email and local UTC offset. Maintainers keep a noreply
# email and UTC dates in public history, so the admin "lands" instead: this
# script pushes the PR's own commits to main as a fast-forward, and GitHub
# marks the PR merged by itself. The admin can bypass the PR/check ruleset only for
# this; force-push and branch deletion stay blocked for everyone.
#
# Refuses unless:
#   1. the PR is open, targets main, and comes from this repo;
#   2. its head is a fast-forward of origin/main (rebase the branch first);
#   3. every new commit is authored AND committed by this clone's configured
#      identity (git config user.name/email), with +0000 dates;
#   4. the required checks passed on the exact head commit.
# The privacy pre-push hook runs on the push as usual.
#
# Usage: scripts/land.sh <pr-number>
set -euo pipefail

PR="${1:?usage: scripts/land.sh <pr-number>}"
REPO="gaceta-news/gaceta-cli"
IDENT="$(git config user.name) <$(git config user.email)>"   # the maintainer identity configured in this clone
NEED=("test (3.12)" "test (3.13)")
cd "$(dirname "${BASH_SOURCE[0]}")/.."
die() { echo "land: $*" >&2; exit 1; }

read -r STATE BASE HEAD_REF HEAD_SHA CROSS < <(gh pr view "$PR" -R "$REPO" \
  --json state,baseRefName,headRefName,headRefOid,isCrossRepository \
  -q '"\(.state) \(.baseRefName) \(.headRefName) \(.headRefOid) \(.isCrossRepository)"')
[[ "$STATE" == OPEN ]] || die "PR #$PR is $STATE"
[[ "$BASE" == main ]] || die "PR #$PR targets $BASE, not main"
[[ "$CROSS" == false ]] || die "PR #$PR comes from a fork; land forks by re-committing under the gaceta identity"

git fetch -q origin main "$HEAD_REF"
[[ "$(git rev-parse "origin/$HEAD_REF")" == "$HEAD_SHA" ]] || die "local fetch of $HEAD_REF != PR head"
git merge-base --is-ancestor origin/main "$HEAD_SHA" || die "not a fast-forward of main: rebase $HEAD_REF on main and push"
N=$(git rev-list --count "origin/main..$HEAD_SHA")
[[ "$N" -gt 0 ]] || die "nothing to land"

BAD=$(git log --format='%h %an <%ae> | %cn <%ce> | %ad %cd' --date=format:%z "origin/main..$HEAD_SHA" \
  | grep -v -F "$IDENT | $IDENT | +0000 +0000" || true)
[[ -z "$BAD" ]] || die "commits not authored+committed as '$IDENT' in UTC:
$BAD"

RUNS=$(gh api "repos/$REPO/commits/$HEAD_SHA/check-runs" -q '[.check_runs[] | {n:.name,s:.status,c:.conclusion}]')
python3 - "$RUNS" "${NEED[@]}" <<'PY' || die "required checks are not green on $HEAD_SHA"
import json, sys
runs = json.loads(sys.argv[1]); need = set(sys.argv[2:])
latest = {}
for r in runs:
    latest.setdefault(r["n"], r)          # API lists newest first
ok = True
for n in sorted(need):
    r = latest.get(n)
    print(f"  {n}: {r['s'] + '/' + str(r['c']) if r else 'missing'}")
    ok &= bool(r) and r["s"] == "completed" and r["c"] == "success"
sys.exit(0 if ok else 1)
PY

echo "landing PR #$PR: $N commit(s), $HEAD_SHA → main"
git push origin "$HEAD_SHA:refs/heads/main"
for _ in 1 2 3 4 5 6; do
  [[ "$(gh pr view "$PR" -R "$REPO" --json state -q .state)" == MERGED ]] && break
  sleep 5
done
echo "PR #$PR: $(gh pr view "$PR" -R "$REPO" --json state -q .state)"
git push -q origin --delete "$HEAD_REF" 2>/dev/null || true
