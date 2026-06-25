# SDK Evolution Tools

## Standard Tools

- `Read`, `Glob`, `Grep`, `Bash`, `Edit`, `Write` — full access
- `Agent` — dispatch sub-agents (see `.mothership/agents/`)

## Environment

Mothership's `_base` snapshot injects these env vars into the sandbox.
They are not configurable from this repo — they come from mothership's
GitHub App + credential proxy setup.

| Env var | Source | Used for |
|---|---|---|
| `GITHUB_TOKEN` | mothership GitHub App installation | `gh` CLI + `git push`; blocked from `env_vars` override |
| `PROXY_BASE` | mothership credential proxy | base URL for LiteLLM (GPT) and Linear proxies |
| `PROXY_JWT` | mothership credential proxy | bearer for the proxies above |

## Git + GitHub

Mothership has already cloned the repo on `main` into
`/workspace/application-sdk`. Stage 0 only needs to authenticate `gh`:
```bash
echo "$GITHUB_TOKEN" | gh auth login --with-token
```

```bash
# Working directory
cd /workspace/application-sdk

# Create branch
git checkout -b BLDX-456 origin/main

# Push
git push origin BLDX-456

# Create PR
gh pr create --repo atlanhq/application-sdk --base main \
  --title "fix(scope): description [BLDX-456]" \
  --label "Autonomous SDK Evolution" \
  --body "..."

# Check CI
gh pr checks <PR_NUMBER> --repo atlanhq/application-sdk --watch

# Read PR diff (for Gate 2 — always use this, not local files)
gh pr diff <PR_NUMBER> --repo atlanhq/application-sdk
```

## Cross-Model Challenge (GPT-5.3-codex via Proxy)

For Gate 1 challengers, call GPT via the credential proxy:

```bash
curl -s "$PROXY_BASE/proxy/litellm/chat/completions" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.3-codex",
    "temperature": 0.2,
    "max_tokens": 12000,
    "messages": [
      {"role": "system", "content": "<gate1 challenger prompt>"},
      {"role": "user", "content": "<finding + file content + rules>"}
    ]
  }'
```

Parse: `jq -r '.choices[0].message.content'`
Retry once on failure. If retry fails, note "GPT unavailable" and skip challenge for this finding (keep the finding — fail-open on challenge).

## Linear API (via Proxy)

Create and manage tickets via the Linear proxy:

```bash
# List issue statuses for a team
curl -s "$PROXY_BASE/proxy/linear" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ workflowStates(filter: { team: { key: { eq: \"BLDX\" }}}) { nodes { id name } }}"}'

# Create issue
curl -s "$PROXY_BASE/proxy/linear" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier url } }}",
    "variables": {
      "input": {
        "title": "[HIGH] bug: race condition in heartbeat handler",
        "teamId": "<team_id>",
        "projectId": "<project_id>",
        "parentId": "<parent_ticket_id>",
        "description": "..."
      }
    }
  }'

# Update issue status
curl -s "$PROXY_BASE/proxy/linear" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success }}",
    "variables": { "id": "<issue_id>", "input": { "stateId": "<state_id>" }}
  }'

# Add comment
curl -s "$PROXY_BASE/proxy/linear" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success }}",
    "variables": { "input": { "issueId": "<issue_id>", "body": "PR #42 created" }}
  }'
```

## Handoff (Stage 6)

No external handler needed. Claude posts comments directly:

```bash
# FIX PRs → trigger sdk-review auto-complete
gh pr comment <PR_NUMBER> --repo atlanhq/application-sdk \
  --body "@sdk-review auto-complete Autonomous SDK Evolution fix."

# DESIGN PRs → trigger sdk-review (review-only)
gh pr comment <PR_NUMBER> --repo atlanhq/application-sdk \
  --body "@sdk-review Design proposal from SDK Evolution."
```

## Summary (Stage 8)

Update Linear parent ticket directly via proxy (see Linear API section above).
No external handler needed.

## Glean (context search — max 5 queries per run)

Search internal docs, Slack threads, Confluence for prior discussions:

```bash
# Dispatch a Glean search sub-agent
# The sub-agent uses the Glean MCP tool
# Query example: "application-sdk run_sync deprecation discussion"
```

Use Glean in Stage 3b (feasibility check) to find prior discussions
about findings before creating tickets. Avoids re-raising issues that
were already discussed and accepted.

Budget: max 5 Glean queries per run. Prioritize DESIGN findings.

## Pre-commit + Tests (inside sandbox)

```bash
uv run pre-commit run --files <changed_files>
uv run pytest tests/unit/ -x -q --timeout=60
```

## Prohibited

- No direct Linear API calls outside the proxy
- No pushing to main (always PRs)
- No force-push
- No `git add -A` or `git add .`
