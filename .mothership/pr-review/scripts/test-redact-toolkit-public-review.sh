#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDACTOR="$SCRIPT_DIR/redact-toolkit-public-review.sh"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

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

if grep -Eq '(atlan-frontend|blaze|heracles|atlan-automation-engine-app|atlan-query-intelligence-app|atlan-publish-app|atlan-popularity-app|atlan-lineage-app|@atlanhq/[^[:space:])`,;!?]+|/workspace/[^[:space:])`,;!?]+|/tmp/toolkit-review-consumers[^[:space:])`,;!?]*|[0-9a-f]{40})' "$review"; then
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
