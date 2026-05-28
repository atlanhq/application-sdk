#!/usr/bin/env bash
# Validate a PR's title against the conventional-commit rules that drive
# the SDK and contract-toolkit semver version bumps.
#
# Why this exists:
#   - `release.py` (SDK) computes the next semver from EVERY conventional
#     commit since the last `v*` tag — `feat` bumps minor, `fix` bumps
#     patch, breaking (`!:` or `BREAKING CHANGE`) bumps major. Squash-merge
#     means the PR title becomes the commit subject on main, so the title
#     directly drives the bump.
#   - `contract_toolkit_release.py` runs the same logic on commits filtered
#     to `contract-toolkit/**`, against the `contract-toolkit-v*` tag stream.
#
# The rules below mirror that split so the title accurately predicts which
# release surface (SDK, toolkit, or neither) will publish from this PR:
#
#   1. PR touches `application_sdk/`  →  feat:/fix: required.
#      Scope is free-form (any scope, or no scope). A `contract-toolkit`
#      scope is fine if there are also application_sdk changes — the SDK
#      release is the primary surface.
#   2. PR touches `contract-toolkit/` and NOT `application_sdk/`  →
#      feat(contract-toolkit):/fix(contract-toolkit): required.
#   3. PR touches NEITHER application_sdk/ NOR contract-toolkit/  →
#      anything OTHER than feat/fix at the type position. chore/docs/ci/
#      style/refactor/perf/test/build/revert are all fine. `feat` or `fix`
#      would silently bump the SDK semver with no functional change behind
#      it.
#
# Usage:
#   PR_NUMBER=1234 REPO=atlanhq/application-sdk ./validate_pr_title.sh
#
# Exit code 0 = valid, 1 = invalid. On invalid, prints a single
# `reason=...` line and a multi-line `message=` block to stdout in
# `GITHUB_OUTPUT` form so the caller can render a comment.

set -euo pipefail

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${REPO:?REPO is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

TITLE=$(gh pr view "${PR_NUMBER}" --repo "${REPO}" --json title --jq '.title')
FILES=$(gh pr view "${PR_NUMBER}" --repo "${REPO}" --json files --jq '.files[].path')

HAS_SDK=0
HAS_CT=0
HAS_OTHER=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    application_sdk/*) HAS_SDK=1 ;;
    contract-toolkit/*) HAS_CT=1 ;;
    *) HAS_OTHER=1 ;;
  esac
done <<< "$FILES"

# Conventional-commit anchor: type(optional-scope)!?: description
# Accept the canonical list of types; anything else is malformed.
TYPE_RE='^(feat|fix|chore|docs|style|refactor|perf|test|build|ci|revert)(\([^)]+\))?!?:[[:space:]]'
if ! echo "$TITLE" | grep -qE "$TYPE_RE"; then
  cat <<EOF
status=invalid
reason=not_conventional
title=${TITLE}
message<<TITLE_MSG
PR title \`${TITLE}\` is not a conventional commit.
Use: \`<type>(<optional-scope>)<!?>: <description>\` — e.g. \`feat(app): add X\`, \`fix(contract-toolkit): Y\`, \`chore(ci): Z\`.
Allowed types: feat, fix, chore, docs, style, refactor, perf, test, build, ci, revert.
TITLE_MSG
EOF
  exit 1
fi

TYPE=$(echo "$TITLE" | sed -E 's/^([a-z]+)(\([^)]+\))?!?:[[:space:]].*/\1/')
SCOPE=$(echo "$TITLE" | sed -nE 's/^[a-z]+\(([^)]+)\)!?:[[:space:]].*/\1/p')

emit_invalid() {
  local reason=$1
  local msg=$2
  cat <<EOF
status=invalid
reason=${reason}
title=${TITLE}
type=${TYPE}
scope=${SCOPE}
has_sdk=${HAS_SDK}
has_ct=${HAS_CT}
has_other=${HAS_OTHER}
message<<TITLE_MSG
${msg}
TITLE_MSG
EOF
  exit 1
}

case "${HAS_SDK}${HAS_CT}" in
  1?)
    # PR touches application_sdk/ (with or without other dirs). The SDK
    # release picks up every commit since the last `v*` tag — the title
    # must be feat: or fix: (or a breaking variant) so the bump is real.
    case "$TYPE" in
      feat|fix) ;;
      *)
        emit_invalid "sdk_needs_feat_or_fix" \
"PR touches \`application_sdk/\` so it will be picked up by the SDK release. The title type must be \`feat\` or \`fix\` (or a breaking variant with \`!\`), but got \`${TYPE}\`.

Use \`feat: ...\` for new behavior or \`fix: ...\` for a bug fix. Scope is up to you (e.g. \`feat(handler): ...\`)."
        ;;
    esac
    ;;
  01)
    # PR touches contract-toolkit/ only (no application_sdk/). The
    # toolkit release filters commits to `contract-toolkit/**`, and by
    # convention the scope on those titles is `contract-toolkit` so the
    # changelog entry is unambiguous.
    case "$TYPE" in
      feat|fix) ;;
      *)
        emit_invalid "ct_needs_feat_or_fix" \
"PR touches \`contract-toolkit/\` so it will be picked up by the contract-toolkit release. The title type must be \`feat\` or \`fix\` (or a breaking variant with \`!\`), but got \`${TYPE}\`.

Use \`feat(contract-toolkit): ...\` or \`fix(contract-toolkit): ...\`."
        ;;
    esac
    if [ "$SCOPE" != "contract-toolkit" ]; then
      emit_invalid "ct_needs_scope" \
"PR touches \`contract-toolkit/\` only — the title scope must be \`(contract-toolkit)\` so the toolkit changelog reads clean, but got \`(${SCOPE:-<none>})\`.

Rewrite as \`${TYPE}(contract-toolkit): ...\`."
    fi
    ;;
  00)
    # PR touches NEITHER application_sdk/ NOR contract-toolkit/. A
    # `feat`/`fix` here would still trigger a SDK semver bump from a PR
    # that ships no SDK code — force a non-bumping type.
    case "$TYPE" in
      feat|fix)
        emit_invalid "no_sdk_or_ct_needs_non_bumping" \
"PR doesn't touch \`application_sdk/\` or \`contract-toolkit/\`, but the title type is \`${TYPE}\` which would still trigger an SDK semver bump on merge to main.

Use a non-bumping type like \`chore: ...\`, \`docs: ...\`, \`ci: ...\`, \`refactor: ...\`, etc."
        ;;
    esac
    ;;
esac

echo "status=valid"
echo "title=${TITLE}"
echo "type=${TYPE}"
echo "scope=${SCOPE}"
echo "has_sdk=${HAS_SDK}"
echo "has_ct=${HAS_CT}"
echo "has_other=${HAS_OTHER}"
exit 0
