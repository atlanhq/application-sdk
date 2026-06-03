#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <markdown-file> [<markdown-file> ...]" >&2
  exit 2
fi

PRIVATE_RE='(atlan-frontend|blaze|heracles|atlan-automation-engine-app|atlan-query-intelligence-app|atlan-publish-app|atlan-popularity-app|atlan-lineage-app|@atlanhq/[^ )`]+|/workspace/[^ )`]+|/tmp/toolkit-review-consumers[^ )`]*|[0-9a-f]{40})'

for file in "$@"; do
  [ -f "$file" ] || continue

  perl -0pi -e '
    s#/workspace/[^ \n`)]+#[private path]#g;
    s#/tmp/toolkit-review-consumers[^ \n`)]*#[private path]#g;
    s#\@atlanhq/[^ \n`)]+#[private package]#g;
    s/\b(atlan-frontend|blaze|heracles|atlan-automation-engine-app|atlan-query-intelligence-app|atlan-publish-app|atlan-popularity-app|atlan-lineage-app)\b/[private consumer]/g;
    s/\b[0-9a-f]{40}\b/[private sha]/g;
  ' "$file"

  if grep -Eq "$PRIVATE_RE" "$file"; then
    echo "toolkit public review redaction failed for $file" >&2
    exit 1
  fi
done
