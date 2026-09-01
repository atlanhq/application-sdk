# Phase 0 step 11 — Smart agent routing (deriving review_scope)

11. **Smart agent routing** — classify the PR by which area it touches, and
    dispatch only the matching specialist(s):
    ```bash
    gh pr view "$PR_NUMBER" --repo "$REPO" --json files --jq '.files[].path' > /tmp/PR_FILES.txt
    TOTAL_FILES=$(wc -l < /tmp/PR_FILES.txt)
    CT_FILES=$(grep -cE '^contract-toolkit/' /tmp/PR_FILES.txt || true)
    SDK_FILES=$(grep -cE '^application_sdk/' /tmp/PR_FILES.txt || true)
    CONF_FILES=$(grep -cE '^(packages/conformance/|remediation/)' /tmp/PR_FILES.txt || true)
    TEST_FILES=$(grep -cE '^(tests/|contract-toolkit/tests/)' /tmp/PR_FILES.txt || true)
    DOC_FILES=$(grep -cE '^(docs/|contract-toolkit/docs/|.*README\.md$)' /tmp/PR_FILES.txt || true)
    CONFIG_FILES=$(grep -cE '^(pyproject\.toml|uv\.lock|\.pre-commit|\.github/|helm/)' /tmp/PR_FILES.txt || true)
    # Agent-prompt / operational-meta files with NO Temporal/Dapr code surface:
    # the mothership review+resolve playbooks, Claude Code config, and
    # AGENTS/CLAUDE instruction files. Excluded from SOURCE_FILES below so a
    # prompt-only PR is never routed into the full 3-agent SDK panel (the SDK
    # correctness/quality/structure agents have no rules for reviewing prompts).
    META_FILES=$(grep -cE '^(\.mothership/|\.claude/)|(^|/)(AGENTS|CLAUDE)\.md$' /tmp/PR_FILES.txt || true)
    # SOURCE_FILES = files in NONE of the non-source buckets (conformance, tests,
    # docs, config, meta). Computed by exclusion (grep -vc) rather than
    # TOTAL - sum(buckets): a file matching two buckets (e.g. docs/CLAUDE.md is
    # both DOC and META) would be subtracted twice by the arithmetic and could
    # zero out a real source count, silently skipping code review. Mirrors the
    # bucket patterns above (contract-toolkit is intentionally NOT excluded — CT
    # PRs are routed by the first two rules before SOURCE_FILES is consulted).
    SOURCE_FILES=$(grep -vcE '^(packages/conformance/|remediation/|tests/|contract-toolkit/tests/|docs/|contract-toolkit/docs/|pyproject\.toml|uv\.lock|\.pre-commit|\.github/|helm/|\.mothership/|\.claude/)|.*README\.md$|(^|/)(AGENTS|CLAUDE)\.md$' /tmp/PR_FILES.txt || true)
    CHANGED_LINES=$(grep -cE '^[+-]' /tmp/DIFF.patch 2>/dev/null || echo 0)
    # Security-sensitive paths NEVER take the fast path (a 3-line auth/secret
    # change is exactly where a subtle blocker hides).
    SECURITY_PATHS=$(grep -cE '(credential|secret|auth|token|_dapr|_temporal)' /tmp/PR_FILES.txt || true)
    ```
   This file-list based classification includes deleted files; do not classify
   only from `+++ b/` diff headers. Apply in order; **first match wins**:
   - If `CT_FILES > 0 && SDK_FILES == 0 && CONF_FILES == 0 && CONFIG_FILES == 0` → `review_scope=contract-toolkit`
     (toolkit-review.md only; mandatory private consumer validation based on affected surface)
   - If `CT_FILES > 0 && (SDK_FILES > 0 || CONFIG_FILES > 0)` → `review_scope=mixed-sdk-toolkit`
     (standard SDK review agents + toolkit-review.md)
   - If `META_FILES > 0 && SOURCE_FILES == 0 && CONFIG_FILES == 0 && CONF_FILES == 0
     && CT_FILES == 0 && TEST_FILES == 0 && DOC_FILES == 0` → `review_scope=docs-only`
     (agent-prompt / operational-meta files only — `.mothership/**`, `.claude/**`,
     `AGENTS.md`, `CLAUDE.md` — carry no Temporal/Dapr code surface, so the SDK
     domain agents have nothing to review. Take the docs-only skip path: submit
     APPROVE, no Phase 2. Because META is excluded from `SOURCE_FILES`, a PR that
     ALSO touches code routes on the real code — `config-only`, `full`, etc. — so
     the meta files never drag prompt/markdown into the SDK correctness agents,
     and code is never skipped just because prompts changed alongside it.)
   - If `SOURCE_FILES == 0 && CONF_FILES > 0 && CT_FILES == 0` → `review_scope=conformance-only`
     (`conformance.md` agent only — the conformance-suite specialist for
     `packages/conformance/**` + `remediation/**`: SARIF detector correctness,
     rule-catalog consistency, rule scope (sdk/app/both), the two CI gates, and
     the paired remediation program in the SAME PR. NOT the SDK CORRECTNESS
     agent — conformance code is AST/rule logic, not Temporal/Dapr runtime.
     If `CONFIG_FILES > 0` too, also dispatch `ci-config.md` on the config slice.)
   - If `SOURCE_FILES == 0 && TEST_FILES > 0` → `review_scope=tests-only`
     (QUALITY agent only — focused on test patterns, coverage, assertions)
   - If `SOURCE_FILES == 0 && DOC_FILES > 0 && TEST_FILES == 0` → `review_scope=docs-only`
     (Skip Phase 2 — submit APPROVE with "Docs/meta-only PR, no code review needed")
   - If `SOURCE_FILES == 0 && CONFIG_FILES > 0` → `review_scope=config-only`
     (`ci-config.md` agent only — the CI/workflow/deps/infra specialist, NOT
     the SDK CORRECTNESS agent. Reviews GHA injection/permissions/pinning,
     shell robustness, dependency/supply-chain, and `helm/**`.)
   - If `SOURCE_FILES <= 2 && TEST_FILES >= SOURCE_FILES * 3` → `review_scope=tests-focused`
     (QUALITY agent + lightweight CORRECTNESS — mostly tests with a few source changes)
   - If `SOURCE_FILES >= 1 && TOTAL_FILES <= 2 && CHANGED_LINES < 50 && CONF_FILES == 0
     && CT_FILES == 0 && CONFIG_FILES == 0 && SECURITY_PATHS == 0` → `review_scope=minor`
     (**fast path** for a tiny source change: CORRECTNESS agent only — it always
     keeps guardrail coverage G1-G5 — skipping the heavier QUALITY/STRUCTURE
     waves and the adversarial Wave 2, on the Small time budget. NEVER matches
     credential/secret/auth or `_dapr`/`_temporal` seam paths; those fall through
     to `full`.)
   - Otherwise → `review_scope=full` (correctness + quality + structure)
