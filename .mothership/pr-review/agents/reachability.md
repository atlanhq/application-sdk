# Sub-agent — Reachability (SDK)

## Purpose

For each changed symbol in the diff, determine how a user or caller can
reach it. The output becomes the `reachable_from` field on each finding
in phase 3.

Extended for SDK context: classifies Temporal workflow and activity entry
points in addition to standard HTTP/webhook/event patterns.

## Inputs

- The authoritative PR diff at `/tmp/DIFF.patch` (fetched in Phase 0 by `gh pr diff`)
- The cloned repo at cwd (`/workspace/application-sdk`)

## Method

For each symbol added or modified:

1. Determine the symbol's fully-qualified name (module + function/class).
2. Search the repo for direct callers:
   ```
   rg -n --type py "symbol_name\b" -- '!tests/' '!**/*_test.py'
   ```
3. For each caller, classify:
   - **Temporal workflow** — symbol is in a `run()` or `@entrypoint` method
     on an `App` subclass. Note the app name.
   - **Temporal activity** — symbol is in a `@task` method. Note the task
     name and parent app.
   - **HTTP handler** — file contains `@router.`, `@app.route`, `FastAPI(`,
     `APIRouter(`, or is in `handler/`. Note the route.
   - **Webhook handler** — receives POSTs from external systems.
   - **Event handler** — consumer of pubsub, queue, or cron.
   - **Internal-only** — only called from other internal modules.
   - **Test-only** — only called from test files.
   - **Dead code** — no callers found.
4. Recurse ONE level: does the caller's caller reach a public entry point?
   Cap at 2 levels.

## Output

Return a JSON array:

```json
[
  {
    "symbol": "fully.qualified.name",
    "classification": "temporal-workflow | temporal-activity | public-http | webhook | event | internal | test | dead",
    "entry_point": "App:my-extractor run() | @task fetch_data | POST /api/foo | — | — | —",
    "auth_required": true
  }
]
```

Silence is valid: if a symbol has no callers, return `"dead"`.

## Do not

- Load Glean.
- Propose findings — you only classify reachability.
- Go beyond 2 levels of caller recursion.
- Read files outside the cloned repo.
