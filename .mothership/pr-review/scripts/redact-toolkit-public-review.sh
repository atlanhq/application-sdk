#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <markdown-file> [<markdown-file> ...]" >&2
  exit 2
fi

# ── Hex-valued control markers are data, not prose ───────────────────────────
#
# The HTML-comment markers at the top of a review summary are read back by the
# approval chain. The hex-valued ones carry machine data — this repo's own head
# SHA, the toolkit artifact hash — never a fact about a consumer repo. This
# repo is public, so its head SHA is public by construction: it is in the PR
# URL and on every check run. Redacting it protects nothing.
#
# Redacting it does, however, break the chain silently. The 40-hex rule below
# rewrote `<!-- REVIEWED_HEAD: <sha> -->` to
# `<!-- REVIEWED_HEAD: [private sha] -->`, which
# `.github/scripts/sdk_review_approve.py` (matching
# `<!-- REVIEWED_HEAD: ([0-9a-f]+) -->`) reads as "no marker at all". It then
# skips every label AND the atlan-ci approval, warns, and exits 0 — so the
# workflow reports success while the PR sits unapproved. The `sed` safety net
# in ORCHESTRATION.md §3f does not recover it either: that repairs a literal
# `<HEAD_SHA>` placeholder, not an already-redacted value.
#
# Only the hex-valued markers are exempt, because only they are damaged: no
# rule below matches `<!-- SDK_REVIEW -->`, `<!-- VERDICT: READY_TO_MERGE -->`,
# or `<!-- ANSWERS_TRIGGER: 5402328241 -->`, so those need no exemption and do
# not get one.
#
# The exemption is safe by construction rather than by allowlist discipline: an
# exempt line's value must be bare `[0-9a-f]+`, and not one of the five private
# patterns can be spelled in that alphabet — every one of them requires a `/`,
# an `@`, a `-`, or a letter past `f`. A marker-shaped line carrying anything
# else falls through and is redacted like any other line.
MARKER_NAMES='REVIEWED_HEAD|TOOLKIT_ARTIFACT_HASH|SDK_REVIEW_RETRIGGER_HEAD|SDK_REVIEW_STARTED_HEAD'
MARKER_RE="^<!--[[:space:]]*(${MARKER_NAMES})[[:space:]]*:[[:space:]]*[0-9a-f]+[[:space:]]*-->[[:space:]]*$"

PRIVATE_RE='(atlan-frontend|blaze|heracles|atlan-automation-engine-app|atlan-query-intelligence-app|atlan-publish-app|atlan-popularity-app|atlan-lineage-app|@atlanhq/[^[:space:])`,;!?]+|/workspace/[^[:space:])`,;!?]+|/tmp/toolkit-review-consumers[^[:space:])`,;!?]*|[0-9a-f]{40})'

for file in "$@"; do
  [ -f "$file" ] || continue

  # Line-at-a-time (not the previous `-0` slurp) so a marker line can be skipped
  # wholesale. Every rule below matches within a single line, so nothing is lost
  # by it. Under `-p` the implicit `continue { print }` still runs after `next`,
  # so an exempt line is emitted verbatim.
  MARKER_RE="$MARKER_RE" perl -i -pe '
    BEGIN { $marker = qr/$ENV{MARKER_RE}/; }
    next if $_ =~ $marker;
    s#/workspace/[^[:space:]`),;!?]+#[private path]#g;
    s#/tmp/toolkit-review-consumers[^[:space:]`),;!?]*#[private path]#g;
    s#\@atlanhq/[^[:space:]`),;!?]+#[private package]#g;
    s/(atlan-frontend|blaze|heracles|atlan-automation-engine-app|atlan-query-intelligence-app|atlan-publish-app|atlan-popularity-app|atlan-lineage-app)/[private consumer]/g;
    s/[0-9a-f]{40}/[private sha]/g;
  ' "$file"

  # Verification skips the same exempt lines, or it would re-flag the very SHAs
  # the exemption exists to preserve.
  if grep -vE "$MARKER_RE" "$file" | grep -Eq "$PRIVATE_RE"; then
    echo "toolkit public review redaction failed for $file" >&2
    exit 1
  fi
done
