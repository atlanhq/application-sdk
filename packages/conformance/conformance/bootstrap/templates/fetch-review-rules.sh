#!/usr/bin/env bash
# Fetch shared L2/L4 review rules into the local, gitignored cache.
set -euo pipefail

sdk_repo="atlanhq/application-sdk"
cache_dir=".mothership/.cache/review-rulesets"
lock_file=".mothership/.cache/rules.lock"

sha="$(gh api "repos/${sdk_repo}/commits/main" --jq .sha 2>/dev/null || true)"
if [ -z "${sha}" ]; then
  if [ -f "${lock_file}" ]; then
    echo "WARN: cannot reach GitHub; keeping cached review rules." >&2
    exit 0
  fi
  echo "ERROR: no shared rules cache and GitHub is unavailable." >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
for ruleset in connector-app platform; do
  gh api "repos/${sdk_repo}/contents/.mothership/review-rulesets/${ruleset}?ref=${sha}" \
    --jq '.[] | select(.type == "file") | .path' | while read -r path; do
      mkdir -p "${tmp}/${ruleset}"
      gh api "repos/${sdk_repo}/contents/${path}?ref=${sha}" --jq .content | \
        python3 -c 'import base64, sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.buffer.read()))' \
        > "${tmp}/${ruleset}/$(basename "${path}")"
    done
  rules_dir=".mothership/review-rulesets/${ruleset}/rules"
  gh api "repos/${sdk_repo}/contents/${rules_dir}?ref=${sha}" \
    --jq '.[] | select(.type == "file") | .path' | while read -r path; do
      mkdir -p "${tmp}/${ruleset}/rules"
      gh api "repos/${sdk_repo}/contents/${path}?ref=${sha}" --jq .content | \
        python3 -c 'import base64, sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.buffer.read()))' \
        > "${tmp}/${ruleset}/rules/$(basename "${path}")"
    done
done

mkdir -p "$(dirname "${cache_dir}")"
rm -rf "${cache_dir}"
mv "${tmp}" "${cache_dir}"
trap - EXIT
printf '{"sdk_repo":"%s","sha":"%s","fetched_at":"%s"}\n' \
  "${sdk_repo}" "${sha}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${lock_file}"
echo "Fetched L2/L4 review rules at ${sdk_repo}@${sha}"
