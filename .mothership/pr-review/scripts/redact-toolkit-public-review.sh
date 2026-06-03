#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <markdown-file> [<markdown-file> ...]" >&2
  exit 2
fi

PRIVATE_RE='(atlan-frontend|blaze|heracles|atlan-automation-engine-app|atlan-query-intelligence-app|atlan-publish-app|atlan-popularity-app|atlan-lineage-app|@atlanhq/[^[:space:])`,;!?]+|/workspace/[^[:space:])`,;!?]+|/tmp/toolkit-review-consumers[^[:space:])`,;!?]*|[0-9a-f]{40})'

for file in "$@"; do
  [ -f "$file" ] || continue

  perl -0pi -e '
    s#/workspace/[^[:space:]`),;!?]+#[private path]#g;
    s#/tmp/toolkit-review-consumers[^[:space:]`),;!?]*#[private path]#g;
    s#\@atlanhq/[^[:space:]`),;!?]+#[private package]#g;
    s/(atlan-frontend|blaze|heracles|atlan-automation-engine-app|atlan-query-intelligence-app|atlan-publish-app|atlan-popularity-app|atlan-lineage-app)/[private consumer]/g;
    s/[0-9a-f]{40}/[private sha]/g;
  ' "$file"

  if grep -Eq "$PRIVATE_RE" "$file"; then
    echo "toolkit public review redaction failed for $file" >&2
    exit 1
  fi
done
