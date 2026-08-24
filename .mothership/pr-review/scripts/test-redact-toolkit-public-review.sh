#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDACTOR="$SCRIPT_DIR/redact-toolkit-public-review.sh"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

PRIVATE_RE='(atlan-frontend|blaze|heracles|atlan-automation-engine-app|atlan-query-intelligence-app|atlan-publish-app|atlan-popularity-app|atlan-lineage-app|@atlanhq/[^[:space:])`,;!?]+|/workspace/[^[:space:])`,;!?]+|/tmp/toolkit-review-consumers[^[:space:])`,;!?]*|[0-9a-f]{40})'

# ── 1. Prose redaction (unchanged behaviour) ─────────────────────────────────

review="$TMP_DIR/review.md"

{
  printf '%s\n' 'atlan-frontends are pluralized references.'
  printf '%s\n' 'blazes should not block posting.'
  printf '%s\n' 'See /workspace/repo/foo.py, then continue.'
  printf '%s\n' 'Package @atlanhq/internal-app, then continue.'
  printf '%s\n' 'SHA cafe1234cafe1234cafe1234cafe1234cafe1234c'
  printf '%s\n' 'Public summary is otherwise fine.'
} > "$review"

"$REDACTOR" "$review"

if grep -Eq "$PRIVATE_RE" "$review"; then
  echo "redaction test failed: private token remained" >&2
  cat "$review" >&2
  exit 1
fi

grep -Fq '[private consumer]s are pluralized references.' "$review"
grep -Fq '[private consumer]s should not block posting.' "$review"
grep -Fq 'See [private path], then continue.' "$review"
grep -Fq 'Package [private package], then continue.' "$review"
grep -Fq 'SHA [private sha]c' "$review"
grep -Fq 'Public summary is otherwise fine.' "$review"

# ── 2. Control markers survive verbatim ──────────────────────────────────────
#
# The approval chain reads these back. A rewritten REVIEWED_HEAD reads as "no
# marker", and `sdk_review_approve.py` then skips every label and the atlan-ci
# approval while still exiting 0 — a green run on an unapproved PR.

markers="$TMP_DIR/markers.md"
HEAD_SHA='7512513d3c00b71c386edfefe54c20729acbc9b6'
ARTIFACT_HASH='7512513d3c00b71c386edfefe54c20729acbc9b6cb6ca1e3d9a8b90d561f11d1'

{
  printf '%s\n' '<!-- SDK_REVIEW -->'
  printf '%s\n' '<!-- VERDICT: READY_TO_MERGE -->'
  printf '%s\n' "<!-- REVIEWED_HEAD: ${HEAD_SHA} -->"
  printf '%s\n' '<!-- ANSWERS_TRIGGER: 5402328241 -->'
  printf '%s\n' "<!-- TOOLKIT_ARTIFACT_HASH: ${ARTIFACT_HASH} -->"
  printf '%s\n' ''
  printf '%s\n' 'Prose mentioning heracles and cafe1234cafe1234cafe1234cafe1234cafe1234 must still redact.'
} > "$markers"

"$REDACTOR" "$markers"

grep -Fqx "<!-- REVIEWED_HEAD: ${HEAD_SHA} -->" "$markers" \
  || { echo "FAIL: REVIEWED_HEAD was redacted" >&2; cat "$markers" >&2; exit 1; }
grep -Fqx "<!-- TOOLKIT_ARTIFACT_HASH: ${ARTIFACT_HASH} -->" "$markers" \
  || { echo "FAIL: TOOLKIT_ARTIFACT_HASH was redacted" >&2; cat "$markers" >&2; exit 1; }
# These three are untouched because no rule matches them, not because they are
# exempt — assert the behaviour either way.
grep -Fqx '<!-- VERDICT: READY_TO_MERGE -->' "$markers"
grep -Fqx '<!-- ANSWERS_TRIGGER: 5402328241 -->' "$markers"
grep -Fqx '<!-- SDK_REVIEW -->' "$markers"

# The exemption must not leak into prose on the same file.
grep -Fq '[private consumer]' "$markers" \
  || { echo "FAIL: prose consumer name was not redacted" >&2; exit 1; }
grep -Fq '[private sha]' "$markers" \
  || { echo "FAIL: prose sha was not redacted" >&2; exit 1; }

# The marker regex the reader uses must actually match what we emitted.
grep -Eq '<!--[[:space:]]*REVIEWED_HEAD:[[:space:]]*[0-9a-f]+[[:space:]]*-->' "$markers" \
  || { echo "FAIL: REVIEWED_HEAD no longer matches the reader's pattern" >&2; exit 1; }

# ── 3. The exemption is narrow ───────────────────────────────────────────────
#
# An allowlisted NAME is not enough — the value must be bare `[0-9a-f]+`. That
# alphabet cannot spell any of the five private patterns (each needs a `/`, an
# `@`, a `-`, or a letter past `f`), so an exempt line is safe by construction.
# Every marker-shaped line that falls outside it is redacted normally.
#
# `<!-- VERDICT: heracles -->` is the case that killed a value-shape-only
# exemption: a consumer name is bare alphanumeric, so "allowlisted name plus
# alphanumeric value" would have passed it straight through.

narrow="$TMP_DIR/narrow.md"
{
  printf '%s\n' '<!-- REVIEWED_HEAD: /workspace/repo/foo.py -->'
  printf '%s\n' '<!-- VERDICT: heracles -->'
  printf '%s\n' '<!-- NOT_AN_ALLOWLISTED_MARKER: cafe1234cafe1234cafe1234cafe1234cafe1234 -->'
  printf '%s\n' '<!-- REVIEWED_HEAD: cafe1234cafe1234cafe1234cafe1234cafe1234 --> trailing prose cafe1234cafe1234cafe1234cafe1234cafe1234 here'
} > "$narrow"

"$REDACTOR" "$narrow"

grep -Fq '<!-- REVIEWED_HEAD: [private path] -->' "$narrow" \
  || { echo "FAIL: marker-shaped line with a private path was not redacted" >&2; cat "$narrow" >&2; exit 1; }
grep -Fq '<!-- VERDICT: [private consumer] -->' "$narrow" \
  || { echo "FAIL: non-hex marker carrying a consumer name was not redacted" >&2; cat "$narrow" >&2; exit 1; }
grep -Fq '<!-- NOT_AN_ALLOWLISTED_MARKER: [private sha] -->' "$narrow" \
  || { echo "FAIL: non-allowlisted marker was not redacted" >&2; cat "$narrow" >&2; exit 1; }
grep -Fq 'trailing prose [private sha] here' "$narrow" \
  || { echo "FAIL: a marker with trailing prose was treated as exempt" >&2; cat "$narrow" >&2; exit 1; }

# ── 4. The verification gate still fires ─────────────────────────────────────
#
# Exempting marker lines from the final grep must not exempt everything else.
# Without a control here, a broken skip could silently pass every file.

gate="$TMP_DIR/gate.md"
printf '%s\n' 'A line the rules do not cover: atlanfrontend-lookalike is fine.' > "$gate"
"$REDACTOR" "$gate"
grep -Fq 'atlanfrontend-lookalike is fine.' "$gate" \
  || { echo "FAIL: a non-private token was rewritten" >&2; exit 1; }

echo "redaction tests passed"
