# 2b. Wave 2 — cross-model adversarial (via proxy)

### 2b. Wave 2 — GPT-5.3-codex Adversarial (via proxy)

After Wave 1, call GPT to challenge your findings.

**Skip conditions** (no adversarial):
- `review_scope` is tests-only, conformance-only, config-only, docs-only, or minor
- `review_scope` is contract-toolkit and toolkit-review.md produced zero findings
- `review_tier` is "staged" (massive PR — too much context for one GPT call)
- Wave 1 produced zero findings (nothing to challenge)
- Time budget already over 70% consumed — run `bash /tmp/budget.sh` here
  and skip whenever it prints `OVER 70%` or `OVER HARD STOP`. This is the
  single most expensive optional step in the run (a full GPT-5.3-codex
  call over the whole diff plus every Wave 1 finding), so it is the first
  thing an over-budget run must give up.

If not skipped:

```bash
curl -s "$PROXY_BASE/proxy/litellm/chat/completions" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.3-codex",
    "temperature": 0.2,
    "max_tokens": 16000,
    "messages": [
      {"role": "system", "content": "<agents/adversarial.md content>"},
      {"role": "user", "content": "<Wave 1 findings + PR diff + annotations>"}
    ]
  }'
```

GPT challenges every Opus finding. GPT also discovers findings Opus missed.

If GPT unavailable or skipped: keep all Opus findings >= 80%.
Note in review: "Cross-model adversarial: <skipped (reason) | ran | unavailable>."
