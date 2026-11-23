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
| Nothing to load | Reviews with no cards | A fleet directory that is not given, does not exist, holds no principle file, or holds only principle files none of which could be read is a configuration error: exit 2 before any GitHub or model request. A dismissal or a re-check loads no principles and is not affected. |

Call sites that changed with it:

- `review.run(principles=…)` and `agent.review_bundle(…, principles, …)` take a
  `PrincipleSet` where lens takes a `RuleSet` (`rules=`).
- `agent.build_rules_message` is `agent.build_principles_message`.
- `agent.build_context` has no `rules` parameter (lens passes one and never reads it).
- `REVIEW_SYSTEM` has a "Principles" section: what `<principles>` is, how to use the
  fields, and that every finding rests on a principle (its id in the comment's
  `principle` field) or is a concrete bug. Since FND-5313 this is checked in code:
  see "Verdict rules" below.
- `config.raw_hash` is the hash of `config.toml` alone (lens adds `rules.toml`).
- `glob_match` lives in `advisory.py`, its only user here (lens has it in `rules.py`).
- The trace line per bundle lists the principle ids, where lens counts rule cards.
- CLI: `--principles-dir` is new.

### Typing

`.github/scripts` holds a new file to zero pyright findings
(`check_pyright_baseline.py`, docs/standards/ci.md), and a copy is a new file: the
baseline entries lens has do not carry over. So the copy is pyright-clean where lens
is not, and `.github/scripts/pyright_baseline.json` is unchanged. None of this
changes behaviour, with the two exceptions noted:

- `typed.py` is new: `EMPTY`, a read-only typed empty mapping, and `objects()`, the
  JSON objects of an untrusted list (nothing for a value that is not a list, so a
  scalar where the model should send a list cannot raise). `(x.get("user") or {}).get("login")` in lens is
  `(x.get("user") or EMPTY).get("login")` here (`agent.py`, `event.py`, `github.py`,
  `holistic.py`, `llm.py`, `review.py`), and `[c for c in items if isinstance(c, dict)]`
  is `objects(items)` (`holistic._parse_verdict`, `review.verdict_items`,
  `trace.args_summary`).
- Local lists and dicts that lens leaves to inference are annotated (`out: list[str] = []`
  and the like), the private `Client._body`, `_responses_body` and `_complete` have
  parameter types, `holistic.build_input` and `check` take `dict[str, Any]`, and the
  tool lists in `prompts.py` are `list[dict[str, Any]]`.
- Exception: `event.decide` reads the comment body as `str(body or "")`, where lens
  calls `.strip()` on the value as it comes. A body that is not a string is read as
  its text and no longer raises.
- Exception: `GitHub.file_at` narrows the reply with a `cast` after its `isinstance`
  check. Same result for every reply.
- Unused `# noqa` comments that lens keeps are gone: the copy is not in the `RUF100`
  baseline list of `pyproject.toml`, which may not grow.

### Tests

`test_connector_reviewer.py` is `test_lens.py` ported. Of lens's 271 tests at the fork
point, 217 are kept as they are (only the names above, `app/` fixture paths, the
principles call sites and type annotations differ), 22 are adapted, 32 are deleted,
and 26 are new, which makes 265.

Fixtures: paths are in the connector layout (`application_sdk/storage/fetch.py` is
`app/storage/fetch.py`), the `repo` fixture has a fleet principles directory where
lens has cards and `rules.toml`, an autouse fixture sets
`CONNECTOR_REVIEWER_PRINCIPLES_DIR`, `fake_github()` returns the fake typed `Any`, and
`state_of()` / `decode_state()` read a state that must be there.

Deleted, by reason:

- **No workflow yet** (6): `test_workflow_triggers_only_on_invocation`,
  `test_shipped_workflow_never_checks_out_pr_head`,
  `test_lock_timeout_matches_the_workflow_job_timeout`,
  `test_workflow_can_write_the_status`,
  `test_workflow_acts_as_the_fleet_app_with_a_scoped_token`,
  `test_workflow_uploads_the_run_report_even_on_failure`.
- **The approval step is removed** (8):
  `test_approval_is_decided_only_when_every_finding_at_every_level_is_closed`,
  `test_a_ready_head_is_approved_by_the_code_owner_token_only`,
  `test_no_approval_unless_every_condition_holds`,
  `test_withdraw_dismisses_only_lens_approvals_by_the_code_owner`,
  `test_the_step_never_fails_a_finished_review`,
  `test_the_review_writes_the_decision_for_the_last_step`,
  `test_only_the_approval_step_holds_the_code_owner_token`,
  `test_a_failed_review_still_leaves_a_withdraw_decision`.
- **The released-surface check is removed** (12):
  `test_a_name_added_and_reshaped_within_the_pr_is_not_a_released_removal`,
  `test_a_released_name_removed_without_deprecation_is_listed`,
  `test_a_released_name_kept_as_a_deprecated_alias_is_not_listed`,
  `test_deleting_or_renaming_away_a_released_module_lists_its_names`,
  `test_base_branch_drift_in_a_module_the_pr_does_not_touch_is_not_listed`,
  `test_a_removal_the_base_branch_made_is_not_the_prs_in_a_module_it_edits`,
  `test_a_released_name_still_on_the_base_branch_removed_by_the_pr_is_listed`,
  `test_a_pr_that_changes_no_package_module_gets_no_block`,
  `test_a_snapshot_failure_falls_back_without_raising`,
  `test_no_release_tag_falls_back_without_raising`,
  `test_run_gives_the_block_to_every_bundle_and_to_verify`,
  `test_run_survives_a_checkout_with_no_release`.
- **The public-API flag is removed** (1):
  `test_the_approach_check_is_told_about_public_api_changes`.
- **Rule cards are replaced by principles** (5):
  `test_first_matching_path_entry_decides_the_cards`,
  `test_every_rules_glob_matches_a_tracked_file_and_wins_for_one`,
  `test_every_path_a_card_cites_exists`,
  `test_every_conformance_rule_id_a_card_cites_exists`,
  `test_dont_flag_lines_cite_only_rules_that_fail_ci`.

The last four of those guarded the shipped rules and cards: against a glob that
matches nothing, and against citing a path or a conformance rule that does not
exist. The fleet principles have their own guard instead:
`test_connector_reviewer_principles.py` loads the shipped files with this package's
loader, requires that none is skipped, holds each to the documented format, and checks
that every conformance rule id it cites exists and runs on app repos.

The change that adds the connector-reviewer workflow must bring the workflow guards
back for its own file: the trigger list, never checking out the PR head, the
`run-name` and no `concurrency:` block (`lock.run_name`), `timeout-minutes` equal to
`lock.JOB_TIMEOUT`, the token permissions, and the run-report upload. It must also
run `python -m connector_reviewer finalize` in an `if: always()` step after the
review step, and give the review step its own `timeout-minutes` below the job's, so
that a timed-out review still reaches that step (see "Verdict rules" below).

## Behaviour that differs from lens

The sections above are the fork itself: the same engine under another name, with a
connector profile. From here on the engine behaves differently, each on purpose.

### Who may ask, trusted Apps, forks and waive (FND-5310)

| | lens | connector-reviewer |
|---|---|---|
| Who may comment a command | `author_association` is OWNER, MEMBER or COLLABORATOR | The commenter's live role on the repository is Write, Maintain or Admin, read from `GET /repos/{repo}/collaborators/{login}/permission` (`GitHub.can_push`, passed to `event.decide` as `can_push`). A member with only Read is refused. A failed lookup is a refusal with a reply: an HTTP error, a network error (DNS, a reset or a timeout, which urlopen raises as `OSError`), a reply cut short or one that is not JSON. `.github/scripts/pr_test_command.py` does the same. |
| Bots | Every bot is refused | A bot is honoured when it is a trusted App: `performed_via_github_app.slug` is on `[access] trusted_apps` in `config.toml` and the author is `<slug>[bot]` (`event.trusted_app`). It has the rights of a person with Write. Every other bot is refused with no reply. |
| A refusal | Silent, except a usage error | `Decision.reply` says whether to answer: yes for a usage error and for a person without the role, no for a bot. |
| A fork | Not checked | `event.is_fork`: the CLI refuses a pull request whose head is in another repository, with a short reply, whatever the command. |
| Waive | No such command | `/review waive <reason>` (`review.waive`): no model call; `PRState.waiver` records who, why and the head sha; a history row; status `success` "waived by @login: reason" on that head; the summary says it was waived and that open findings were not resolved. Any real review clears it (`run`), so `/review force` on the same head puts the verdict back. `verdict_status` is green only for the head the waive names. The pull request's author may waive. |
| Dismiss | The author cannot dismiss a critical or high finding | The same rule, and it holds for a trusted App that authored the pull request. |
| The reviewer triggering itself | Cannot happen: bots are refused | Its App is on the trusted list, so `github.safe_body` guards every comment it posts: none may open with `/review`. |
| A stored reason (dismiss, waive) | As the comment wrote it | Through `event.plain`, which cannot open an HTML comment. (Both copies read the last state block in the comment, and neither summary can spell out a state marker: FND-5323.) |
| Order in the CLI | Decide from the event, then load the config | `event.addressed` first drops a comment that is not `/review` before any config or request; then the config (it holds `trusted_apps`), then `decide`. |

### Exempt pull requests (FND-5311)

lens reviews every pull request it is asked about. Here `exempt.classify`, a pure
function of facts GitHub reports, names four classes that are not reviewed:

| Class | Holds when |
|---|---|
| dependency update | the author is in `dependency_bots`, the branch starts with one of `dependency_branches`, and every commit's author is in `dependency_bots` |
| conformance resync | the author is in `resync_bots`, every commit is by one of them, and every changed file matches `bootstrap_managed` |
| version bump | the branch starts with one of `version_bump_branches`. The summary reminds that it still needs `/gate-check` |
| docs only | every changed file ends in `.md` or is under `docs/`, and none is under `.github/connector-reviewer/` or `.claude/`, or named `CLAUDE.md` or `AGENTS.md` at any depth |

- The lists are the `[exempt]` table of `config.toml` (`exempt.ExemptConfig`).
- `review.run` asks before admission, when the request is not forced and the pull
  request has never been reviewed (`state.round == 0`). An exempt pull request gets
  no model call, a summary that says `Exempt: <class>` and why, and status `success`
  with `exempt: <class>`. `PRState.exempt` records it for one head.
- `/review force` reviews it anyway, and after that it is never exempt again: a
  plain `/review` must not paper over what a forced review found.
- "Every commit" needs the commit list (`GitHub.pr_commit_authors`). If it cannot be
  read, or is shorter than the pull request's own count (GitHub lists at most 250),
  the authors are unknown and the bot classes do not hold.
- A test reads bootstrap's destination registries out of
  `packages/conformance/conformance/bootstrap/render.py` as text and fails when one
  is not covered by `bootstrap_managed`.
- `run` fetches each diff once (`diff()` memoises `gh.diff`), since the exempt check
  and the review both need the pull request's whole diff.

### Verdict rules (FND-5313)

The status is the verdict, and it is never green unless every part of the change
was read and every finding is closed.

| Situation | Status | Summary |
|---|---|---|
| Nothing open | success | Ready to merge |
| Exempt, or waived | success | Exempt: <class>, or waived by @login: reason |
| An open critical or high finding | failure | Changes requested |
| Only medium or low open, a nit included | failure | Not ready to merge |
| Incomplete: a bundle failed, a file is left pending, source was not read, the round cap or the budget stopped a new head, or the run itself ended without a verdict | error | Review incomplete |

| | lens | connector-reviewer |
|---|---|---|
| A nit | Keeps the status red | The same, now by decision: the design's verdict table puts "only medium or low open" at failure, "not ready". |
| A changed file skipped for size (`over 1500 changed lines`) or past the bundle cap | Listed under "not reviewed"; the status can still go green | Unread source (`review.unread_skips`, by the reasons `UNREAD_REASONS`). `PRState.unread` (path -> why) holds them; while it is not empty the status is `error`. Each unread file rides along, whole, with every later review, so a later round re-decides and an incremental round never reviews only the slice the new commits touched. A retry cannot help, so they are not `pending_files`, and the summary says to split the change or waive, not to retry. Excluded, binary, deletion-only and secret-bearing files are skipped on purpose and are not unread, with one exception: a test file deleted outright that weakens an expectation, in a round with no line review at all (`review.WEAKENED_UNREVIEWED`). No prompt could carry its facts and it has no line to comment on, so it is unread until a person waives it, never a silent green. |
| What the status reads | `res.incomplete` of the run | `review.incomplete_reasons`: the run's reasons plus the state's (`pending_files`, `unread`). A dismissal or a re-check builds a fresh result, so in lens dismissing the last finding could turn an incomplete review green; here it stays `error`. The run report and the CLI's reaction read the same list. |
| The round cap or a spent budget stops `/review` on a new head | No status on that head | `error` on that head (`_no_verdict`), "not reviewed — round cap …" or "review incomplete — PR budget … spent; `/review waive` …". A head already reviewed keeps its verdict. |
| A crash, a cancel or a timeout | The `pending` status stays | `finalize.py`, new: `python -m connector_reviewer finalize` reads the latest `connector-reviewer` status (`GitHub.status_of`, read-only, paginated up to `STATUS_PAGES` pages of 100) of the pull request's current head and of the head this run set `pending` on, and turns each into `error` only when it is `pending` and links to this run (`GITHUB_RUN_ID`). It rewrites a progress note this run left behind into the failure note. Everything else is left alone, so it is safe after any outcome. The review's own crash handler calls it too, before it replaces the progress note. A push during the run moves the current head, so `review.run` calls `on_pending(head)` the moment it sets `pending`, and the CLI records that head on its progress note as a hidden marker (`finalize.progress_with_head`); finalize reads it only from a note the reviewer's App posted. Known limit: when the progress note could not be posted, only the current head is checked. |
| A finding that names no principle | Kept | `agent.cite`, after placement and before the fact-check: `Finding.rule_id` (from the comment's new optional `principle` field, or an applicable id written in its title or content) stands only when it names a principle that applies to the finding's own file (a bundle can mix `app/` code and its tests, and FP01 does not apply to the test). A finding left without one is dropped unless its category is `bug` or `security` (`CONCRETE_CATEGORIES`). `BundleResult.uncited` and the run report count them. The tool schema has no per-bundle enum, so the cached prefix stays the same for every bundle. |
| A finding in a test file | Capped at low unless `security` | Also not capped when it rests on a principle (`rule_id`): that principle's Severity line grades it (FP15 says high for a deleted test that covered changed code). |
| The approach check | First, before the line review; the bundles see `<pr_understanding>` | Alongside the line review, in a thread of its own (at most `concurrency` + 1 calls in flight), on a ledger of its own carved out of the round's (`APPROACH_SHARE`, 15%), so it can never spend a bundle's share. On the round it runs the bundles do not see its reading of the PR; later rounds do, from the state. Its requests are listed after the line review's in the run report. Still advisory: it never changes the status. |
| A slow bundle | Bounded by turns, context and its budget share | Also by wall time: `[agent] bundle_time_limit_s` (default 120; 0 turns it off), measured from the bundle's start, plan turn included. Past it the next turn is the final one. `BundleResult.forced` names why the final turn was forced and `seconds` how long the bundle took; both are in the run report. Bundles still run four at a time. |

Tests adapted for this: `test_the_review_prompt_tells_the_model_how_to_use_and_name_principles`
(citing is now required), `test_loop_places_comments_from_quoted_code_and_reflector_can_only_remove`
(its fact-checked finding now names a principle, or it would be dropped before the
fact-check), `test_approach_check_runs_once_per_pr_and_is_advisory` (round 1's line
review no longer sees `<pr_understanding>`; round 2's does) and
`test_every_request_is_recorded_with_counts_only_and_reported` (the approach request
comes last).

### Regression and history facts (FND-5312)

lens hands the line review the callers, tests and changed functions it finds in
`context.build`. This fork adds two sets of facts, computed in code in `facts.py`
and placed in every bundle's prompt after `<context>`. They add no model stage.

| Facts | What is computed | Where it comes from |
|---|---|---|
| Regression (`<regression_facts>`) | For each changed function of a non-test Python file in the bundle: its callers outside the tests (by name, from the index) and the test files that name it, by tier (`unit`, `integration`, `e2e`, `other`, from the path). For the whole pull request: every existing test expectation weakened or removed: an assertion gone, an expectation changed (same left-hand side, new right-hand side), a new skip or xfail, a deleted test whose assertions went with it. A moved, reformatted or renamed test, an added test, and an assertion in source code do not count. A removed assertion is taken as moved only when the same line is added in the same test (by name, in any file; the name comes from the def lines and the hunk header, which for a method deep in a test class names the class) or in a test the pull request adds: the same line added to another test that already existed does not hide it. An expectation changed is a new assertion with the same left-hand side in the same test. | The index and the diff. No request. |
| History (`<history_facts>`) | Lines the pull request restores that a recent base-branch commit removed; lines it removes that one added (at least `history_min_lines` non-trivial lines each, and not lines that commit merely moved); a revert ("This reverts commit …" in a commit message of the pull request, or a title `Revert "…"`); a function changed `churn_changes` times in `history_days` days. The count includes this pull request: the default 3 fires when two recent commits touched a function the pull request changes. A function is "touched" when a hunk's header or a def or class line inside it names it, so the count is a floor. | `GitHub.recent_commits` (commits on the base sha that touched the path, since `history_days`), `GitHub.commit_patches` (each commit's patches, read once per sha) and `GitHub.pr_commit_messages` (up to three pages of 100: GitHub lists at most 250 commits of a pull request; when the pull request has more, the history line says how many messages the revert check read; read inside the same wall-time cap, and a failed read, network errors included, is a note): new, read-only. At most 3 + `history_max_files` + `history_max_commits` requests, four at a time. The reads run on daemon threads (`facts._bounded`): no new request starts after `history_seconds`, and reading history returns then even with a request in flight, which is abandoned and its answer dropped. The client's 60 s timeout bounds each socket read, not a whole reply, so it is not what bounds the wall time. A failed read means fewer facts and a trace note, never an incomplete review. |

- The history block is there only when a fact fired for the bundle's files (or the
  pull request is a revert), and only then are the earlier commits shown: short sha,
  date, title and the lines. A commit title is untrusted text, so `<` and `>` are
  made inert, and code lines cannot close the block. So is every path in either
  block (`facts._path`): a pull request names its own files, and a name holding
  `</regression_facts>` or a newline cannot close the block or start a line.
- `REVIEW_SYSTEM` has a "Facts" section: what the blocks are, that a fact is not a
  finding, and the severity guidance (medium at most; high only for a concrete
  broken caller, a deleted or skipped test covering changed code, or a recent fix
  undone without a reason). `task_done` takes two optional answers, `regression` and
  `history`, kept on `BundleResult.answers`.
- The summary has a **Regression** line and a **History** line under the approach
  check: the facts' counts (`Facts.regression_line`, `history_line`), then what the
  line review answered (at most two bundles). `PRState.facts` keeps the lines, so a
  summary re-rendered by a dismissal keeps them. The run report carries them, with
  the history request count, and `timings_ms["facts"]`.
- `[facts]` in config.toml turns each half off and sets the caps (`facts.FactsConfig`).
  Each value must have its field's TOML type: a quoted `"false"` or a boolean where a
  number belongs is refused by `config.validate` (`Config.load_errors`), so the run
  stops with a config error rather than read `bool("false")` as true.
- Cost, measured offline: the regression facts take about 1.3 s on application-sdk
  itself (about 3,000 files, 650 test files), most of it listing the files, which
  the tools then reuse. A connector app is a fraction of that. The history half is
  network time, bounded as above; it has not been measured against GitHub here.
- The weakened expectations are read from the round's test files before file
  selection (`review.changed_tests`): selection skips a deleted test file ("deletion
  only") and a test change that only removes lines ("no added lines"), and those are
  the cases FP15 is about. Secret-bearing and excluded files are still left out.
  Such a test file that still has a line to comment on (context around a removed
  assertion) is moved from skipped to reviewed (`review._review_weakened_tests`), so
  a pull request that only removes assertions gets a line review with its facts. One
  deleted outright, with no line review in the round, is unread (see "Verdict rules").
- Known limits: the history facts are computed for the bundle files only, so a
  source file whose change only removes lines (no bundle) cannot raise "removes
  lines a recent commit added". The pull request's own commits are read for revert
  messages only: back and forth between its own commits is not detected. A removed
  assertion whose identical line lands in a brand-new test is taken as moved. Callers
  are counted by bare name, like `context.call_sites`.
- Not done: the gate-file check of FND-5312 (compare a changed bootstrap-managed
  gate file with what bootstrap would render). It needs the conformance package's
  renderer, which this stdlib-only engine does not have.

Tests: `FakeGitHub` has empty history reads by default
(`pr_commit_messages`, `recent_commits`, `commit_patches`), so every other test runs
as before.

## Module by module

Where a lens fix lands in this copy. "Names" means only the renames in the table
above; "typing" means only the annotations described under Typing.

| Module | Against lens at the fork point |
|---|---|
| `baseline.py`, `diff.py`, `index.py` | Identical, byte for byte. A lens patch applies as it is. |
| `lock.py` | Names. |
| `report.py` | Names; bundle time, forced final turn and dropped uncited findings; the incomplete list from `incomplete_reasons`. |
| `findings.py` | Names; `PRState.waiver`, `exempt`, `unread`, `facts`. |
| `triage.py` | Typing (one annotation). |
| `tools.py` | Two description strings (`compatibility`, `search_code`), the comment's optional `principle` field, and `task_done`'s optional `regression` and `history`. |
| `trace.py` | Names, typing. |
| `select.py` | `DEFAULT_EXCLUDE`, and `excluded` (the pattern match, public for `review.changed_tests`). |
| `config.py` | Docstring, `raw_hash`, typing; the `[access]`, `[exempt]` and `[facts]` tables. |
| `bundle.py` | `_module_key` and the module docstring. |
| `llm.py` | Names (the gateway key variable, the User-Agent, log text), typing. |
| `github.py` | Names, typing, four methods removed with the approval step; `can_push`, `safe_body`, `status_of`, `pr_commit_messages`, `recent_commits` and `commit_patches` added. |
| `event.py` | Rewritten around who may ask (FND-5310); the command grammar is lens's plus `waive`. |
| `holistic.py` | The prompt's opening and one concern; no `<public_api>` block; typing. |
| `context.py` | No `_public` / `public_api`; `build` returns a string. |
| `advisory.py` | Rewritten as the empty extension point; holds `glob_match`. |
| `agent.py` | Names; principles where lens has rules; no released-surface block; typing; `cite`, the test-file cap, the per-bundle time limit (FND-5313); the facts blocks and the `task_done` answers (FND-5312). |
| `prompts.py` | The review prompt's opening, "Repository layout", "Principles" and "Facts" sections; the removals; typing. |
| `review.py` | Names; the removals; `RISKY_PREFIXES` as tiers; principles where lens has rules; typing; exempt and waive; the verdict rules and the approach check alongside the line review (FND-5313); `compute_facts` and the Regression and History lines (FND-5312), with `_whole_files` joining a file reviewed in parts back into one diff before its facts are computed. |
| `__main__.py` | Names; no `approve` sub-command; the config directory default; `--principles-dir`; the `finalize` sub-command, and the crash handler calls it. |
| `__init__.py` | Docstring. |
| `principles.py`, `typed.py`, `exempt.py`, `finalize.py`, `facts.py` | New here. |
| `approve.py`, `surface.py`, `rules.py` | Not here. |

## Fixed in both copies (FND-5323)

lens reviewed this fork and raised defects in code that came over unchanged. None
of these is a difference any more: each was fixed in lens and in this copy alike,
and a later lens fix to the same code ports as usual.

- The fork's own fixes, ported to lens: `redact` masks the gateway key itself
  (`register_secret`) and the CLI registers it first thing; the preflight's 401 and
  "unreachable" reasons and the run report's errors are redacted; a change that may
  hold secrets (`tools.secret_diff`, either side of a rename) is skipped at
  selection and refused by `read_diff`; `PRState.decode` reads the last state block
  and the summary cannot spell out a state marker; `validate` refuses a NaN or
  infinite price or cap and a negative cache price; `place` only accepts a path
  from its own bundle.
- The 17 defects found in both: the reservation counts a prompt's upper bound and a
  possibly billed failed attempt is booked before a retry (`llm.py`); a file over
  the bundle token cap is split at hunk boundaries (`bundle.py`); a state too big
  for one comment is cut until it fits (`findings.py`, `review._fit_state`); the
  approach check caps lookups per call (`holistic.py`); a long function shows a
  window around every change, and a named path keeps its leading dot
  (`context.py`); quoted Git paths are decoded (`diff.py`); tool-argument salvage
  reads braces inside strings as text (`tools.py`); scoped callers use the nearest
  enclosing function, the call's receiver and inherited methods, and relative test
  imports are resolved (`index.py`); triage never drops a keyword or literal flip,
  a Python re-indent, an import line with a statement after it, or the same edit
  under different context (`triage.py`).

Every finding of that review was judged a real defect; none was set aside.

## Not changed, and worth knowing before the next change

Nothing listed at the moment. The three items that were here (the test-file
severity cap, the verdict rule for a nit, and the unused `Finding.rule_id`) are
decided under "Verdict rules (FND-5313)".
