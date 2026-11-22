# connector-reviewer is a fork of lens

connector-reviewer (`.github/scripts/connector_reviewer/`) reviews pull requests in
the connector app repos (`atlanhq/atlan-*-app`). It is a separate copy of lens
(`.github/scripts/lens/`), the reviewer of application-sdk itself. The two do not
share code, so a fix to lens does not reach this copy until someone ports it.

This file lists every intentional difference from lens, so that a later lens fix
can be ported knowingly. Update it in the same change as any new difference.

## Fork point

| What | Commit |
|---|---|
| application-sdk `main` when the copy was made | `3dbd51f96589897bb17b1d50a22c580232476d98` |
| Last commit that touched `.github/scripts/lens/` | `d5a10fc5dc3bc22e32ee450d31c09602f87e7412` |
| Last commit that touched `.github/lens/config.toml` | `ebed442eed361a72d3ea25f4e72ebb655b60be14` |
| Last commit that touched `.github/scripts/tests/test_lens.py` | `d58a4f90da6bd3deddcdc6abae111669a02890ce` |

To see what lens changed since the fork:

```bash
git log 3dbd51f96589897bb17b1d50a22c580232476d98..origin/main -- .github/scripts/lens .github/lens .github/scripts/tests/test_lens.py
```

## What was copied

| lens | connector-reviewer |
|---|---|
| `.github/scripts/lens/` | `.github/scripts/connector_reviewer/` |
| `.github/lens/config.toml` | `.github/connector-reviewer/config.toml` |
| `.github/scripts/tests/test_lens.py` | `.github/scripts/tests/test_connector_reviewer.py` |

Not copied: `.github/lens/rules.toml`, `.github/lens/cards/`, `.github/workflows/lens.yml`.
There is no connector-reviewer workflow yet, so nothing triggers this package.

## Differences from lens

### Names

| What | lens | connector-reviewer |
|---|---|---|
| Python package | `lens` (`python -m lens`) | `connector_reviewer` (`python -m connector_reviewer`) |
| Comment command | `/lens`, `/lens force`, `/lens dismiss …`, `/lens recheck …`, `/lens verify-budget` | `/review`, `/review force`, `/review dismiss …`, `/review recheck …`, `/review verify-budget` |
| Commit status context | `lens` | `connector-reviewer` |
| Summary comment marker | `<!-- lens-summary -->` | `<!-- connector-reviewer-summary -->` |
| State marker | `lens-state` | `connector-reviewer-state` |
| Environment variables | `LENS_GATEWAY_KEY`, `LENS_BOT_LOGIN`, `LENS_REPORT_PATH` | `CONNECTOR_REVIEWER_GATEWAY_KEY`, `CONNECTOR_REVIEWER_BOT_LOGIN`, `CONNECTOR_REVIEWER_REPORT_PATH` |
| Prompt cache keys | `lens-review`, `lens-verify`, `lens-reflect`, `lens-approach`, `lens-recheck` | `connector-reviewer-review`, `connector-reviewer-verify`, `connector-reviewer-reflect`, `connector-reviewer-approach`, `connector-reviewer-recheck` |
| User-Agent | `lens/1.0 (+…)` | `connector-reviewer/1.0 (+…)` |
| Run report file named in the summary | `lens-run.json` | `connector-reviewer-run.json` |
| Workflow file the lock looks for (`lock.WORKFLOW_FILE`) | `lens.yml` | `connector-reviewer.yaml` |
| Run name the lock matches (`lock.run_name`) | `lens #<pr>` | `connector-reviewer #<pr>` |
| Log, trace and comment prefix | `lens:` | `connector-reviewer:` |
| Summary line | "lens reads this PR as" | "The reviewer reads this PR as" |

Unchanged: `MODEL_GATEWAY_BASE_URL`, `GITHUB_TOKEN`, the default bot login
(`atlan-app-fleet[bot]`), the model id, `reasoning_effort = "max"`, the prices, the
$1 cap per PR and the round limit.

### Where the config is read from

lens reads `<root>/.github/lens/` from the base-branch checkout of the repo it
reviews, which is application-sdk itself. connector-reviewer reviews other
repositories, so its default config directory is the one that ships beside the
package (`.github/connector-reviewer/` in the application-sdk checkout the package is
run from), not `<root>/.github/connector-reviewer/` of the repo under review.
`--config-dir` still overrides it. An app repo therefore cannot retune the reviewer.

### Removed: what only application-sdk needs

| lens has | In connector-reviewer |
|---|---|
| The code-owner approval: `approve.py`, the `approve` sub-command, the decision file and `LENS_APPROVAL_PATH`, and `GitHub.reviews` / `approve` / `dismiss_review` / `newest_status` | Removed. The verdict is the `connector-reviewer` commit status and the summary comment only. A lens fix to the approval step has nothing to port to. |
| The released-surface check: `surface.py`, its import of `.github/scripts/check_symbol_removals.py`, the `<released_surface_removals>` block in the review and verify prompts, the `released` argument of `_verify`, `_verify_batch` and `_check_new_against_base`, and trace group "3b · released surface" | Removed. Nothing reads `vX.Y.Z` tags. The prompts judge compatibility "against the base branch", where lens says "against the base branch and the last release". Trace group "3c · added by this PR" is "3b" here. |
| The public-API flag: `context._public`, `context.public_api`, the "⚠ PUBLIC API behaviour change" line in `<context>`, the `<public_api>` block and its paragraph in the approach check, and the "A changed symbol marked PUBLIC API …" bullet in `REVIEW_SYSTEM` | Removed: a connector app has no public Python API. `context.build` returns the context block alone (lens returns a tuple with the public-API lines). One sentence of that bullet is kept on its own: a behaviour change the description declares as intended is the author's decision. |
| The recheck facts `private: …` / `public: module …` and `listed in docs/agents/sdk-capabilities.md`, with `review.CAPABILITIES_DOC` and `review._private` | Removed. `recheck_evidence` takes no `capabilities` argument, and `RECHECK_SYSTEM` no longer mentions privacy or a capability manifest. The remaining facts are the merge-base fact, the callers and a workflow's checkout-target visibility. |
| `docs/agents/sdk-capabilities.md` in `select.DEFAULT_EXCLUDE` | Removed. |
| The trivy/grype scan advisory in `advisory.py` (`SCAN_GLOBS`, citing `docs/standards/build-security.md`) | Removed. `advisory.py` is kept as an empty extension point: an `Advisory` (globs and a note) added to `advisory.ADVISORIES` is matched against the PR's changed paths, and `notes()` takes the tuple as an optional argument. `PRState.advisories` and the "**Advisory** (does not block)" summary line are unchanged. |

Wording that followed from the removals: the compatibility flag of `code_comment`
(`tools.COMMENT_ITEM`) and `RECHECK_SYSTEM` say "a deployed contract" where lens says
"a public or released contract".

### Connector profile

| Where | lens | connector-reviewer |
|---|---|---|
| `prompts.REVIEW_SYSTEM`, first lines | "a senior code reviewer for the Atlan Application SDK" | `prompts.CONNECTOR_PROFILE` (the target is a connector app built on the Application SDK v3, and what a bad merge costs), then the scope sentence (review for regression risk, cost at customer scale, production failure and silent data loss, credential leaks, test integrity; not style, naming, generic best practice or what a CI gate enforces), then a "Repository layout" block. Everything from "Scope" down is lens's text, with the edits in the next three rows. |
| `REVIEW_SYSTEM`, "Do not report what CI already enforces" | formatting, import order, …, naming style | The same list plus "conformance rules". |
| `REVIEW_SYSTEM`, severity `high` | "broken public contract" | "broken contract" |
| `REVIEW_SYSTEM`, severity `low (nit)` | "clearer name, simpler expression, a missing edge-case test" | "a small, concrete improvement within the review scope above — a simpler expression that removes a hazard, a missing edge-case test". Naming is out of scope here, so the example that asked for it is gone. |
| `holistic.SYSTEM` (approach check) | No description of the target | `CONNECTOR_PROFILE` after its first line. "a public contract" in the concern list is "a contract (the Pkl contract, a workflow's or task's inputs and outputs, persisted state)". |
| `bundle._module_key` | `application_sdk/x/y.py` and `tests/unit/x/test_y.py` cluster as `x` | `app/x/y.py` and `tests/{unit,integration,e2e}/[app/]x/test_y.py` cluster as `x`; everything under `contract/` is one cluster `contract`; a root `Dockerfile*` and `pyproject.toml` are one cluster `build`. |
| `review.RISKY_PREFIXES`, `review._risk` | A flat tuple of SDK prefixes: a bundle is risky or it is not | Ordered tiers, riskiest first: `app/` (not `app/generated/`), `contract/`, `Dockerfile` and `pyproject.toml`, `.github/`, `tests/`, then everything else. `review.risk_rank(path)` gives a path's tier and a bundle ranks as its riskiest file. |
| `select.DEFAULT_EXCLUDE` | — | Adds `**/*.jpeg`, `**/*.gif`, `**/*.ico`, `**/*.webp`. |
| `config.toml` `[scope] exclude` | `.mothership/**`, `conformance/**/fixtures/**`, `tests/**/fixtures/**`, `tests/**/golden/**` | `app/generated/**`, `contract/PklProject.deps.json`, `.mothership/**`, `tests/**/fixtures/**`, `tests/**/golden/**`, `tests/**/__snapshots__/**` |
| `search_code` tool description | example glob `application_sdk/**` | example glob `app/**` |

### Principles instead of rule cards

lens matches rule cards to files by glob: `rules.py`, `.github/lens/rules.toml` (an
ordered list of globs, first match wins) and `.github/lens/cards/*.md`. This fork has
none of the three. It has `principles.py`:

| | lens (`rules.py`) | connector-reviewer (`principles.py`) |
|---|---|---|
| Unit | A card: free Markdown | A principle: front matter (`id`, optional `applies-to`), a `# Title`, and five fields (`Principle`, `Why`, `Ask` with at most three questions, `Don't flag`, `Severity`) |
| Matched to a file by | The first glob in `rules.toml` that matches | Area, fixed in code (`area_of`): `app`, `generated` (`app/generated/**`, which is never `app`), `tests`, `contract`, `ci` (`.github/**`). No globs. A file outside all five gets only the principles with no `applies-to`. |
| Sources | The config directory | Fleet principles (`FP01`…) from `--principles-dir` or `CONNECTOR_REVIEWER_PRINCIPLES_DIR`. They live in application-sdk at `.github/connector-reviewer/principles/`, and CI points the reviewer at that directory of its application-sdk checkout; there is no default. Repo principles (`RP01`…) from `<root>/.github/connector-reviewer/principles/` of the repo under review. When the two are the same directory it is read once, as the fleet's. Only files named `^[a-z]{2}\d{2}-.+\.md$` are read. |
| Prompt block | `<review_rules>` holding `<rules card="…" for="…">` | `<principles>` holding `<principle id="…" title="…" for="…">` with the five fields; fleet first, then repo, each in id order; a repo principle carries `source="this repository"` and follows a line that says so |
| A bad file | Not checked | Skipped, with a note in the summary (`PrincipleSet.notes` into `RunResult.notes`). The id must be `FP`/`RP` plus two digits for its source, match the file name, and be unique; an unknown front-matter key or area is an error; a file over 8,000 characters or a symlink is not read. A field may wrap over several lines, and blank lines between fields are optional. |
| Nothing to load | Reviews with no cards | A fleet directory that is not given, does not exist or holds no principle file is a configuration error: exit 2 before any GitHub or model request. A dismissal or a re-check loads no principles and is not affected. |

Call sites that changed with it:

- `review.run(principles=…)` and `agent.review_bundle(…, principles, …)` take a
  `PrincipleSet` where lens takes a `RuleSet` (`rules=`).
- `agent.build_rules_message` is `agent.build_principles_message`.
- `agent.build_context` has no `rules` parameter (lens passes one and never reads it).
- `REVIEW_SYSTEM` has a "Principles" section: what `<principles>` is, how to use the
  fields, and that a finding resting on a principle names its id. Citing one is not
  required and nothing in code checks it.
- `config.raw_hash` is the hash of `config.toml` alone (lens adds `rules.toml`).
- `glob_match` lives in `advisory.py`, its only user here (lens has it in `rules.py`).
- The trace line per bundle lists the principle ids, where lens counts rule cards.
- CLI: `--principles-dir` is new.

### Tests

Removed from the copy because they assert on `.github/workflows/lens.yml` or on the
shipped lens cards, neither of which this fork has:

- `test_workflow_triggers_only_on_invocation`
- `test_shipped_workflow_never_checks_out_pr_head`
- `test_lock_timeout_matches_the_workflow_job_timeout`
- `test_workflow_can_write_the_status`
- `test_workflow_acts_as_the_fleet_app_with_a_scoped_token`
- `test_workflow_uploads_the_run_report_even_on_failure`
- `test_only_the_approval_step_holds_the_code_owner_token`
- `test_every_rules_glob_matches_a_tracked_file_and_wins_for_one`
- `test_every_path_a_card_cites_exists`
- `test_every_conformance_rule_id_a_card_cites_exists`
- `test_dont_flag_lines_cite_only_rules_that_fail_ci`

The change that adds the connector-reviewer workflow must bring the workflow guards
back for its own file: the trigger list, never checking out the PR head, the
`run-name` and no `concurrency:` block (`lock.run_name`), `timeout-minutes` equal to
`lock.JOB_TIMEOUT`, the token permissions, and the run-report upload.
