# SDK Evolution Tools

## Standard tools

- `Read`, `Glob`, `Grep`, `Bash`, `Edit`, `Write` — full access.
- `Agent` — dispatch the discovery + verification sub-agents.
- Skills available in this repo: `/audit-consumers`, `/remediate`,
  `/capability-manifest`, `toolkit-feature-workflow` (weekly stages use these).

## Environment

Mothership's `_base` snapshot injects these into the sandbox. Not configurable
from this repo.

| Env var | Source | Used for |
|---|---|---|
| `GITHUB_TOKEN` | mothership GitHub App installation | `gh` CLI + `git push` |
| `PROXY_BASE` | mothership credential proxy | base URL for the Linear proxy |
| `PROXY_JWT` | mothership credential proxy | bearer for the Linear proxy |

There is **no GPT / LiteLLM proxy** in this pipeline anymore — verification is
in-model (see ORCHESTRATION Stage 2). There is no Glean step and no shared
codebase index.

## Git + GitHub

Mothership already cloned the repo on `main` into `/workspace/application-sdk`.
Stage 0 only authenticates `gh`:
```bash
echo "$GITHUB_TOKEN" | gh auth login --with-token
```

```bash
cd /workspace/application-sdk
git checkout -b BLDX-456 origin/main
git push origin BLDX-456
gh pr create --repo atlanhq/application-sdk --base main \
  --title "fix(scope): description [BLDX-456]" \
  --label "Autonomous SDK Evolution" --body "…"
gh pr checks <PR_NUMBER> --repo atlanhq/application-sdk --watch
gh pr diff  <PR_NUMBER> --repo atlanhq/application-sdk   # use this for review, not local files
```

## Linear API (via proxy)

```bash
# List workflow states for the team
curl -s "$PROXY_BASE/proxy/linear" \
  -H "Authorization: Bearer $PROXY_JWT" -H "Content-Type: application/json" \
  -d '{"query": "{ workflowStates(filter: { team: { key: { eq: \"BLDX\" }}}) { nodes { id name } }}"}'

# Open tickets in the project (Stage 0 suppression list)
curl -s "$PROXY_BASE/proxy/linear" \
  -H "Authorization: Bearer $PROXY_JWT" -H "Content-Type: application/json" \
  -d '{"query": "{ issues(filter: { project: { name: { eq: \"App SDK v3.0\" }}, state: { type: { nin: [\"completed\",\"canceled\"] }}}, first: 100) { nodes { identifier title description } }}"}'

# Create issue
curl -s "$PROXY_BASE/proxy/linear" \
  -H "Authorization: Bearer $PROXY_JWT" -H "Content-Type: application/json" \
  -d '{"query": "mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier url } }}",
       "variables": {"input": {"title": "…", "teamId": "<id>", "projectId": "<id>", "parentId": "<parent>", "description": "…"}}}'

# Update status / add comment — see IssueUpdateInput / CommentCreateInput mutations
```

## Reviewers (human sign-off)

Two humans sign off on design-level work: **Vaibhav Chopra**
(`vaibhav.chopra@atlan.com`) and **Chris** (SDK maintainer — confirm the exact
Linear handle on first run). Resolve their Linear user IDs once per run:

```bash
curl -s "$PROXY_BASE/proxy/linear" \
  -H "Authorization: Bearer $PROXY_JWT" -H "Content-Type: application/json" \
  -d '{"query":"{ users(filter:{ name:{ containsIgnoreCase:\"Vaibhav\" }}){ nodes{ id name email } } }"}'
```

Tag **both**: add them as `subscriberIds` on the ticket (IssueCreate/IssueUpdate
input) AND @-mention both in a comment so Linear pings them. Apply on:
- the run **parent** ticket (one ping per run), and
- every **`needs-design-review`** ticket (DESIGN + ADR).

Do NOT tag them on routine FIX tickets — those go to `@sdk-review`.

## Handoff — single review pass (Stage 6)

```bash
gh pr comment <PR_NUMBER> --repo atlanhq/application-sdk \
  --body "@sdk-review Please review this Autonomous SDK Evolution PR."
```
One pass only — NOT `@sdk-review auto-complete`. A human decides the merge.

## Pre-commit + tests (inside sandbox)

```bash
uv run pre-commit run --files <changed_files>
uv run pytest tests/unit/ -x -q --timeout=60
```

## Prohibited

- No Linear calls outside the proxy.
- No pushing to main. No force-push. No `git add -A` / `git add .`.
- No `@sdk-review auto-complete` (single review pass only).
