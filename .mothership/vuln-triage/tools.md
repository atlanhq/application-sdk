# Vuln Triage Tools

## Standard Tools

- `Read`, `Glob`, `Grep`, `Bash`, `Edit`, `Write` — full access
- `Agent` — dispatch sub-agents (e.g. for a Glean prior-discussion search)

## Environment

Mothership's `_base` snapshot injects these env vars into the sandbox. They are
not configurable from this repo.

| Env var | Source | Used for |
|---|---|---|
| `GITHUB_TOKEN` | mothership GitHub App installation | `gh` CLI + `git push` |
| `PROXY_BASE` | mothership credential proxy | base URL for LiteLLM (GPT) + Linear proxies |
| `PROXY_JWT` | mothership credential proxy | bearer for the proxies above |

The run prompt also passes: `TICKET` (Linear identifier to triage), `SEVERITY`
(the ticket's severity bucket), `RUN_DATE`, `GHA_RUN_URL`.

## Git + GitHub

Mothership has already cloned the repo on `main` into `/workspace/application-sdk`.
Authenticate `gh`:

```bash
echo "$GITHUB_TOKEN" | gh auth login --with-token
cd /workspace/application-sdk
```

Used for: read-only work (scan artifacts, advisories, inspecting
`pyproject.toml`/`uv.lock`) **and** pushing branches + opening the two
auto-mergeable PR shapes. Never push to `main`, never merge — the GHA gate
(`vuln-auto-merge.yml`) approves + merges once CI is green.

### Allowlist PR (Critical / High — every case)

Touches **only** `.security/base-allowlist.json`. Compute `expires` from the
severity SLA:

```bash
# CRITICAL = 7 days, HIGH = 30 days from the run date.
EXPIRES=$(date -u -d "${RUN_DATE} + 7 days" +%Y-%m-%d)    # CRITICAL
EXPIRES=$(date -u -d "${RUN_DATE} + 30 days" +%Y-%m-%d)   # HIGH

git checkout -b chore/allowlist-<cve-id> origin/main
# add one entry per CVE to .security/base-allowlist.json (see ORCHESTRATION 4-pre)
uv run python .github/scripts/validate_allowlist.py   # MUST pass before pushing
git add .security/base-allowlist.json
git commit -m "chore(security): allowlist <CVE-IDs> with <severity> SLA"
git push origin chore/allowlist-<cve-id>
gh pr create --repo atlanhq/application-sdk --base main \
  --title "chore(security): allowlist <CVE-IDs> (<severity>, SLA <N>d)" \
  --label "vuln-auto-merge" \
  --body "<per-CVE: case, package, expires/SLA, ticket link, GHA_RUN_URL>"
```

### Bump PR (Case 1 only — our dependency, scan-confirmed fix)

Touches **only** `pyproject.toml`, `uv.lock`, `requirements.txt`.

```bash
git checkout -b fix/bump-<pkg>-<version>-<cve-id> origin/main
# ... run the uv recipe + validation greps below ...
git push origin fix/bump-<pkg>-<version>-<cve-id>
gh pr create --repo atlanhq/application-sdk --base main \
  --title "fix(security): bump <pkg> to <version> to resolve <CVE-IDs>" \
  --label "vuln-auto-merge" \
  --body "..."
# If the bump needs source-code edits or a check fails: open it as a DRAFT
# WITHOUT the label and tag a human. Do NOT force it onto the auto-merge path.
```

## The scan artifacts (source of truth for ALL CVEs)

The ticket lists the CVEs, but the authoritative per-CVE data (package,
installed version, fixed version, severity) is in the scan's raw artifacts.

```bash
RUN_ID=$(gh run list -R atlanhq/application-sdk \
    --workflow daily-security-scan.yml --limit 1 \
    --json databaseId --jq '.[0].databaseId')
gh run download "$RUN_ID" -R atlanhq/application-sdk -n security-scan-raw-results

# Enumerate every unique CVE: severity | id | pkg | installed | fixed
jq -r '.Results[]?.Vulnerabilities[]?
  | "\(.Severity)|\(.VulnerabilityID)|\(.PkgName)|\(.InstalledVersion)|\(.FixedVersion // "N/A")"' \
  trivy-image-results.json trivy-fs-results.json | sort -u
```

If the artifact has expired (14-day retention) or there is no recent run:
`gh workflow run daily-security-scan.yml --ref main -R atlanhq/application-sdk`,
wait, then download.

## uv dependency-bump recipe (Case 1)

Run this to produce the Case-1 draft PR. **One CVE fix = full dep upgrade**
(PR #1995 policy). Run the validation greps below before pushing the draft PR.

```bash
# 1. Match uv to main's lockfile format so the regen doesn't downgrade it.
MAIN_REV=$(awk '/^revision = / { print $3; exit }' uv.lock | tr -d '"')
if [ "$MAIN_REV" = "2" ]; then
    UV_VER=$(grep -A1 "setup-uv" .github/actions/setup-deps/action.yaml \
           | grep "version:" | tr -d '" ' | cut -d: -f2)   # CI's pin
else
    UV_VER="0.9.0"   # writes revision = 3, preserves "# via" + upload-time
fi
curl -LsSf "https://astral.sh/uv/${UV_VER}/install.sh" -o /tmp/install-uv.sh
UV_UNMANAGED_INSTALL=/tmp/uv-pinned sh /tmp/install-uv.sh
export PATH="/tmp/uv-pinned:$PATH"
uv --version

# 2. Only edit pyproject.toml if its current range does NOT cover the fixed
#    version. If the range already allows it, leave pyproject.toml alone —
#    the lockfile is what pins the resolved version.

# 3. Regenerate the lockfile (blanket upgrade) + requirements in lockstep.
uv sync --all-extras --all-groups --upgrade
uv export --no-hashes --frozen > requirements.txt
```

## Validation greps (MUST pass before pushing the Case-1 draft PR)

If any fails, do NOT open the PR — a regen in this state recreates the regression
that motivated this flow. Detail the blocker on the ticket and tag Vaibhav/Chris.

```bash
grep -q '^revision = '  uv.lock          || { echo "FAIL: lock revision header lost"; exit 1; }
grep -q 'upload-time'   uv.lock          || { echo "FAIL: upload-time stripped"; exit 1; }
grep -q '# via'         requirements.txt || { echo "FAIL: # via annotations stripped"; exit 1; }

uv run pre-commit run --files uv.lock requirements.txt pyproject.toml
uv run pytest tests/unit/ -x -q --timeout=60
```

## Linear API (via Proxy)

```bash
# Read the ticket being triaged
curl -s "$PROXY_BASE/proxy/linear" \
  -H "Authorization: Bearer $PROXY_JWT" -H "Content-Type: application/json" \
  -d '{"query": "query($id: String!){ issue(id: $id){ id identifier title description url state { name } } }", "variables": {"id": "<TICKET>"}}'

# Add a comment (per-CVE classification + recommendation; draft PR link for Case 1)
curl -s "$PROXY_BASE/proxy/linear" \
  -H "Authorization: Bearer $PROXY_JWT" -H "Content-Type: application/json" \
  -d '{"query": "mutation($input: CommentCreateInput!){ commentCreate(input: $input){ success } }", "variables": {"input": {"issueId": "<issue_id>", "body": "..."}}}'
```

Leave the ticket open — do not set "Done". Reconciliation
(`reconcile_allowlist.py`) closes it once the scanner confirms its CVEs are gone
for 3 consecutive scans.

## Prohibited

- No direct Linear API calls outside the proxy.
- No pushing to `main`. No force-push. No `git add -A` / `git add .` — stage only
  the specific file(s) each PR shape allows.
- No merging — the GHA gate (`vuln-auto-merge.yml`) approves + merges.
- No allowlist entry for **Medium/Low** (the validator rejects them).
- No mixing PR shapes — an allowlist PR touches ONLY `.security/base-allowlist.json`;
  a bump PR touches ONLY the dep manifests. A mixed PR will not auto-merge.
- No editing the Dockerfile / base image; no `apk` workarounds (Case 4 → rebuild).
