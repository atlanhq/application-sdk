# Conformance Remediation Tools

## Standard tools

- `Read`, `Glob`, `Grep`, `Bash`, `Edit`, `Write` — full access
- `Task` — **use this.** Sub-agents run on the cheap model; the per-file edit
  drafting and the refutation pass in Stage 4 belong there, not in your own
  context. A run that never delegates leaves the cheap lane idle and puts the
  whole `M`-finding volume through the expensive one.

## Environment

Injected by mothership's `_base` snapshot; not configurable from this repo.

| Env var | Source | Used for |
|---|---|---|
| `GITHUB_TOKEN` | mothership GitHub App installation | `gh` CLI + `git push` |
| `PROXY_BASE` | mothership credential proxy | base URL for the LiteLLM + Linear proxies |
| `PROXY_JWT` | mothership credential proxy | bearer for those proxies |

The run prompt additionally carries `REPO`, `RULE_ID`, `SERIES`, `TIER`,
`DELIVERY`, `BASE_REF`, `HEAD_SHA`, `PR_NUMBER`, `SUITE_VERSION`,
`APPLY_UNVERIFIABLE`, `RUN_ID`.

## Session files

Written by the orchestrator at dispatch — read, never edit:

| Path | Contents |
|---|---|
| `/workspace/.mothership/session/REMEDIATION.md` | this lane's playbook, fetched from `application-sdk@main` |
| `/workspace/.mothership/session/PRIOR_DECISIONS.json` | rulings from earlier attempts on this `(repo, rule)`, keyed by finding fingerprint |
| `~/.claude/skills/remediate/SKILL.md` | the remediation skill |

`PRIOR_DECISIONS.json` shape:

```json
[
  {
    "fingerprint": "…",
    "rule_id": "L004",
    "question": "…",
    "chosen": "…",
    "rationale": "…",
    "evidence": ["application_sdk/foo.py:42"],
    "decided_at": "2026-08-18T02:14:00Z"
  }
]
```

Honouring these is what makes retries deterministic. Re-deciding the same
ambiguity differently churns the diff across attempts and destroys reviewer trust
in the lane.

## The conformance suite

Always **pinned** to `SUITE_VERSION` — never `@latest`. The fleet spans `0.13.0`
to `0.20.1`, so an unpinned run "fixes" findings a differently-versioned CI leg
will re-raise.

```bash
# Every series except D
uvx "atlan-application-sdk-conformance==${SUITE_VERSION}" detect \
  --repo . --series "$SERIES" --output /tmp/remediation/before.sarif

# D-series needs the app's own env (D003 resolves import names from installed
# package metadata; a bare uvx run degrades it)
uv sync --all-extras
uv run --with "atlan-application-sdk-conformance==${SUITE_VERSION}" \
  atlan-application-sdk-conformance detect --repo . --series D --output …

# The area prescriptions and the loop contracts
PROGRAMS=$(uvx "atlan-application-sdk-conformance==${SUITE_VERSION}" programs-dir)
```

### Reading the result — the only number that counts

```bash
jq '.runs[0].properties["atlan/summary"]' /tmp/remediation/before.sarif
# → {"failing": 92, "warning": 0, "suppressing": 0}
```

Three different things mean "clean" and only one is usable here:

| Signal | Trustworthy? |
|---|---|
| process exit code | **No** — `--exit-zero` makes it 0 regardless |
| `invocation.exitCode` in the SARIF | the real result, but whole-run |
| **`atlan/summary.failing`** | **use this**, filtered to your rule |

Most app repos render `exit-zero: true`, so a green Conformance check in CI says
nothing about whether findings exist.

Rule-level filtering is a **post-filter on `result.rule_id`**. Do not try
`--series L004` — `--series` matches a series *letter*, so that activates zero
checks and yields an empty report you would misread as "clean".

```bash
jq --arg r "$RULE_ID" \
  '[.runs[0].results[] | select(.ruleId == $r)] | length' \
  /tmp/remediation/before.sarif
```

## Git + GitHub

Mothership has already cloned `REPO` into `/workspace/<name>` on `BASE_REF`.

```bash
echo "$GITHUB_TOKEN" | gh auth login --with-token
cd /workspace/$(basename "$REPO")
```

**Rules, in order of how much damage breaking them does:**

1. **Never `--force` / `--force-with-lease`.** Not once, not on your own branch.
2. **Never push to `main`.** `one_pr_per_rule` pushes `conformance/<rule>`;
   `push_to_pr_branch` pushes the PR's head ref. Nothing else.
3. **Never merge.** A human reviews every PR this lane opens.
4. **Never `git add -A`.** Stage the exact paths you edited — the sandbox leaves
   artefacts, and a stray file in the diff fails the shape gate.
5. **Never edit `tests/`, `.github/`, or `conformance/`** *in the app repo*. These
   are the gates you are judged against. `conformance_pr_shape_gate.py` enforces it
   as a required check; the gate is the boundary, this list is its description.

## Fixing the rule instead of the app (Stage 6)

When the evidence says the rule is wrong, you fix the rule — in a **second clone**,
never in the app's working tree:

```bash
cd /workspace
gh repo clone atlanhq/application-sdk sdk -- --depth=50
cd sdk
git switch -c "conformance/rule-fix/<rule-lowercase>" origin/main
# narrow the detector + add the fires/silent regression pair
cd packages/conformance
uv sync --all-extras --all-groups
uv run pytest -q
uv run atlan-application-sdk-conformance gen-rule-docs --check
```

Two clones, two branches, no crossing. SDK changes never land in the app repo and
app changes never land in the SDK.

Then open the PR and hand it to the resolver:

```bash
gh pr create --repo atlanhq/application-sdk --base main \
  --label conformance-rule-fix --title "…" --body-file /tmp/remediation/rule_fix_body.md
gh pr comment <number> --repo atlanhq/application-sdk --body "@sdk-resolve"
```

`@sdk-resolve` drives the PR through review→fix→push until it is merge-ready and
then **stops** — a human merges. That boundary is deliberate: a conformance rule
change is fleet-wide, so the loop may perfect a rule fix but never land one.

The regression test is not optional. `packages/conformance/tests/test_<series>.py`
opens by explaining why: a buggy check "false-positives across hundreds of apps and
triggers spurious remediations". A narrowing without a `silent` case pinning the
shape is a change nobody can defend later.

## Reporting

Both blocks in Stage 7 are machine-parsed by the orchestrator, which persists the
decision log and completes the GitHub check run. Emit them verbatim, including the
`=== END … ===` markers, and emit `DECISIONS` even when it is empty — an absent
block is indistinguishable from a crashed run.
