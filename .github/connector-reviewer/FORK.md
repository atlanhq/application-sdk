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
| A refusal | Silent, except a usage error (the CLI answers a reason in `event.USAGE_ERRORS`) | `Decision.reply` says whether to answer: yes for a usage error and for a person without the role, no for a bot. Both answer an unknown word on the command's line with a usage reply (see "Fixed in both copies (FND-5314)"). |
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
| Past the bundle cap | Bundles sorted by their riskiest file and the rest set aside, "bundle cap (lowest-risk code, after re-packing)" | `review.plan_bundles` (FND-5316). A test file joins the cluster of the source it tests (`bundle._module_key`), so on a large pull request test tokens filled the bundles that held app code, and more bundles held app code than the cap allowed: app code could be cut while tests were read. Still over the cap after re-packing, the code the app ships (`risk_rank` below `tests/`: app, contract, build files, CI) and the rest (tests, then other files) are packed apart, shipped first, so the cap cuts tests and other files before any app code. Under the cap nothing changes. Each skip names what was not read (`review._cap_skips`): `bundle cap: not reviewed, past the N-bundle cap`, or for a file reviewed in parts `bundle cap: lines A-B, … not reviewed, the rest was`. The incomplete reason lists up to 20 unread files (was 5), the summary's list counts any past 50, and both say once what the cap reviews first (`CAP_NOTE`). A state written with the old reason still starts `bundle cap`, so it is still unread. On the replayed pull request every app file was already reviewed and part of one test file was still cut, so that review stays incomplete: whether capped tests should keep a review incomplete is not decided here. |
| What the status reads | `res.incomplete` of the run | `review.incomplete_reasons`: the run's reasons plus the state's (`pending_files`, `unread`). A dismissal or a re-check builds a fresh result, so in lens dismissing the last finding could turn an incomplete review green; here it stays `error`. The run report and the CLI's reaction read the same list. |
| The round cap or a spent budget stops `/review` on a new head | No status on that head | `error` on that head (`_no_verdict`), "not reviewed — round cap …" or "review incomplete — PR budget … spent; `/review waive` …". A head already reviewed keeps its verdict. |
| A crash, a cancel or a timeout | The `pending` status stays | `finalize.py`, new: `python -m connector_reviewer finalize` reads the latest `connector-reviewer` status (`GitHub.status_of`, read-only, paginated up to `STATUS_PAGES` pages of 100) of the pull request's current head and of the head this run set `pending` on, and turns each into `error` only when it is `pending` and links to this run (`GITHUB_RUN_ID`). It rewrites a progress note this run left behind into the failure note. Everything else is left alone, so it is safe after any outcome. The review's own crash handler calls it too, before it replaces the progress note. A push during the run moves the current head, so `review.run` calls `on_pending(head)` the moment it sets `pending`, and the CLI records that head on its progress note as a hidden marker (`finalize.progress_with_head`); finalize reads it only from a note the reviewer's App posted. Known limit: when the progress note could not be posted, only the current head is checked. |
| A finding that names no principle | Kept | `agent.cite`, after placement and before the fact-check: `Finding.rule_id` (from the comment's new optional `principle` field, or an applicable id written in its title or content) stands only when it names a principle that applies to the finding's own file (a bundle can mix `app/` code and its tests, and FP01 does not apply to the test). A finding left without one is dropped unless its category is `bug` or `security` (`CONCRETE_CATEGORIES`). `BundleResult.uncited` and the run report count them. The tool schema has no per-bundle enum, so the cached prefix stays the same for every bundle. |
| A finding in a test file | Capped at low unless `security` | Also not capped when it rests on a principle (`rule_id`): that principle's Severity line grades it (FP15 says high for a deleted test that covered changed code). |
| The severity of a finding that rests on a principle | The model's, from the general scale | Set from the cited principle's Severity line (FND-5316). A replayed finding met a principle's "High when …" case and came out medium, so it did not block. The comment has two optional fields, `severity_case` (`high` or `medium`: the case of the line the scenario meets) and `severity_basis` (the words it meets), and `REVIEW_SYSTEM` says a principle's Severity line, not the general scale, grades such a finding. `agent.grade_by_principle`, after `cite` and the fact-check and before `calibrate`, raises a medium or low finding to high when the case is `high`, the basis is not empty, the comment's `principle` is the one the finding still cites, and that principle's Severity line has a High case. It also lowers (second replay): `severity_case` takes `low` as well, and `agent.principle_case` checks the match. A finding that meets only the Medium case, or a Low case the line names, is capped there, critical included. A Low case on a line that names none counts as medium, the line's lowest case. A critical finding that meets the High case keeps critical. `bound_by_outcome` and then `calibrate` run after it, so their floor and caps apply on top. A finding raised to high is a new blocker, so the skeptic checks it. Verify is not where this lands: it runs only from round 2 and judges fixed, moot or open, never severity. |
| The approach check | First, before the line review; the bundles see `<pr_understanding>` | Alongside the line review, in a thread of its own (at most `concurrency` + 1 calls in flight), on a ledger of its own carved out of the round's (`APPROACH_SHARE`, 15%), so it can never spend a bundle's share. On the round it runs the bundles do not see its reading of the PR; later rounds do, from the state. Its requests are listed after the line review's in the run report. Still advisory: it never changes the status. |
| A slow bundle | Bounded by turns, context and its budget share | Also by wall time: `[agent] bundle_time_limit_s` (default 120; 0 turns it off), measured from the bundle's start, plan turn included. Past it the next turn is the final one. `BundleResult.forced` names why the final turn was forced and `seconds` how long the bundle took; both are in the run report. Bundles still run four at a time. |

Tests adapted for this: `test_the_review_prompt_tells_the_model_how_to_use_and_name_principles`
(citing is now required), `test_loop_places_comments_from_quoted_code_and_reflector_can_only_remove`
(its fact-checked finding now names a principle, or it would be dropped before the
fact-check), `test_approach_check_runs_once_per_pr_and_is_advisory` (round 1's line
review no longer sees `<pr_understanding>`; round 2's does) and
`test_every_request_is_recorded_with_counts_only_and_reported` (the approach request
comes last).

### What a second replay changed (FND-5316)

A replay of 24 past pull requests showed these defects. lens has the same code for
the id and the mechanical box, and still has both defects; they are not ported.

| | lens | connector-reviewer |
|---|---|---|
| Two different findings on one quote | The id is `sha1(category, path, quote)`, with no prose in it. Two findings in one category that quote the same line get the same id. `agent.review_bundle` grades the second from the first one's comment (`raw_of` is keyed by id), and `merge_new` drops it as a restatement, so it vanishes from the summary and the status. | `findings.unique_ids` runs on each bundle's placed findings before `raw_of` is built, and in `review.run` on the round's findings before `_check_new_against_base` and `merge_new`, since two bundles can meet on one file. Groups are formed by the plain fingerprint, not the current id, so the round's pass re-derives the ids a bundle's pass gave and still sees a repeat across bundles (lens F-11333f). In a group, the finding first by principle (none sorts first), then title, keeps the plain fingerprint, so an open finding on a live pull request keeps matching its stored record. Severity and the order the model writes findings in change from round to round, so neither decides anything (lens F-871acd). Each other finding gets `Finding.fingerprint(salt)`. The salt is its principle when no other finding of the group has that principle, otherwise its principle and title, and a counter as a last resort. A later round that reports the same findings gets the same ids. A literal repeat (same quote, principle and title) is one finding reported twice, and is kept once. A plain-fingerprint id depends on neither principle nor title. A salted id depends on whatever its salt holds: if those fields change in a later round, it gets a new id, and the old one stays open until verify closes it. A pull request already hit by the old collision may see one id change once. |
| The summary's box "mechanical changes, proven behaviour-neutral in code, not sent to the model" | `triage` notes a reason per hunk (`Triage.note`), and the box counts every file in `Triage.mechanical`. A file whose import hunk was set aside and whose real change was reviewed is counted as never sent, findings on it included. The run report's triage counts and the approach check's `<mechanical_changes>` ("not line-reviewed", from `summary_lines`) count the same way. | `Triage.withheld` narrows `mechanical` to files with no hunk left to review. The box, `summary_lines` (so the approach check), the run report's counts and the scope trace read it. The box now names the withheld files, at most `review.MECHANICAL_SHOWN` (20) per reason, then "and N more". The trace lists a file with a set-aside hunk and a reviewed rest on a line of its own. `Triage.mechanical` still records every hunk. |
| How a finding says why it fails | `content` names the input or state under which it fails, in prose. Nothing checks that the mechanism it describes is the one the code has. In the replay, four partial catches and two false alarms pointed at the right change and told a familiar failure story the code did not support. | A comment must carry `causal_chain` (required in `COMMENT_ITEM`): `trigger`, one to six `steps` (`path`, `code` quoted verbatim, `effect`), and `outcome`. `agent.traced`, right after placement and before `cite` and the fact-check, checks each step with `agent.trace_chain`. A step's code must have at least `STEP_QUOTE_MIN` (12) non-space characters, and `agent.locate_step` must find it in the file it names: the head text, else the lines the pull request removes ("`path (removed)`"), or for an `sdk:` path the pinned SDK source. The location recorded is where the quote was found. A finding with no chain, no trigger, no outcome, no step, or a step not found is dropped in code. `BundleResult.untraced` holds each with why, and there is a trace line and a run-report column (`untraced_dropped`, the reasons only). A kept finding has the checked chain on `Finding.chain` (not stored in the state, like `scenario`), rendered into `scenario`, so the summary's "When it fails" shows trigger, steps with locations, and result. `REVIEW_SYSTEM`'s "Evidence" section asks for the chain and says to trace the mechanism, not assume it. It gives no list of stories to avoid. The `sdk:` form is named only in the `<sdk_source>` block, so a review without the SDK source sends the same system prompt as before on that point. |
| Severity from the traced outcome | Only the general scale and `calibrate`'s caps. In the replay a missing test was graded high and a crash on any valid configured value was graded medium. | The chain has two more required fields: `harm` (one of `findings.HARMS`) and `normal_path` (the trigger occurs in ordinary runs: the default config, a valid value of an input the connector accepts, a source shape real sources produce). `trace_chain` keeps them on `Finding.chain`. `agent.bound_by_outcome`, after `grade_by_principle` and before `calibrate`, reads only those fields and never the title. A finding whose only harm is a test gap (`harm` `test_gap`, or no harm and category `test`) is capped at medium unless `principle_case` is `high`. A `crash`, `data_loss` or `silent_wrong_output` (`OUTCOME_FLOOR`) with `normal_path` true is raised to at least high. That floor wins over a principle's Medium case, because the chain is traced through quoted code and the case is the model's reading of a sentence. `calibrate`'s caps still win over both. The order is: principle raise or lower, then outcome cap and floor, then `calibrate`. `REVIEW_SYSTEM`'s Severity section states both rules. Its "input form … at most medium" rule now excepts a valid value of an input the connector accepts, which is a normal path. |

### What a third replay changed (FND-5316)

A third replay of the same pull requests, with the rules above in place, showed
these. lens has none of this code.

| | Before | Now |
|---|---|---|
| Where the causal chain ends | At the last quoted step. Two real defects came out medium because the chain stopped one hop short of what the downstream consumer does: the principle's High case was "publish rejects …", and the chain ended at "the payload holds X", so the High case never applied and the outcome floor did not fire. | `REVIEW_SYSTEM` ("Evidence", "Principles") and the `outcome` field of `COMMENT_ITEM` ask for the observable outcome: what the consumer of the output (the publish step, the runtime, Atlan) does with it. A consumer whose code is not in the repository is the `outcome`, not a step (a step must quote code `trace_chain` can find), and a principle that states its behaviour is the authority for that hop. `severity_case`, `harm` and the severity are judged at that outcome. `findings.HARMS` has a new harm, `rejection` (a consumer refuses the output), and it is in `OUTCOME_FLOOR`: a traced rejection on a normal path is at least high, like a crash. |
| The skeptic and a consumer's behaviour | It checked that "the last step produces the outcome", which it cannot do for a consumer in another repository. | `skeptic.SYSTEM` says that when `<cited_principle>` states what the consumer does, the principle is the authority for that last hop: it confirms the last step hands the consumer what the principle describes, and does not contest only because the consumer's code cannot be read. |
| A chain that holds on an edge-case trigger | Upheld when every step held. A valid edge case (an input no real run produces) was raised to high by the floor, upheld step by step, and blocked a clean pull request. | The skeptic judges the trigger too: `skeptic_verdict` has a required `trigger_normal` (and `trigger_why`). `_parse_verdict` upholds only when every step holds and `trigger_normal` is true. False is contested ("the trigger is not a normal input: …"); left out is contested ("the trigger was not confirmed as a normal input"). Either way the finding drops to medium and stops blocking. For a security finding, input an attacker or a misconfigured source can send counts as normal (prompt, not code). `build_input` now shows the reviewer's own `<harm>` and `<normal_path>` claims inside `<causal_chain>`, so the skeptic can check them. |

| A skeptic that answers with no verdict | `malformed` was grouped with a failed call and a spent budget as "no check made", and kept the severity. A finding the skeptic returned no verdict on stayed high and blocked. | `malformed` is in `skeptic.CHANGES`: the finding drops to medium, contested, with `NO_VERDICT` as the note. The rule that only a fully confirmed chain upholds already said this; the old grouping cited "a provider error must not unblock a real defect", and a model that ran and gave no verdict is not a provider error. `error` (a failed call), `budget` and `skipped` still keep the severity: no check was made. The outcome is still recorded as `malformed`, so the run report and the job log tell it apart from a contested verdict, and both mark it "→ medium". |

Tests adapted: `upheld()` (the default skeptic reply, `UPHELD`) now sets
`trigger_normal: true`; `test_the_prompt_and_schema_carry_the_harm_fields` checks
the Severity sentence with "rejection by a consumer".
`test_a_skeptic_that_makes_no_check_keeps_the_severity` is split: its failed-call
case is `test_a_skeptic_whose_call_fails_keeps_the_severity`, and its two
no-verdict cases are `test_a_skeptic_that_answers_with_no_verdict_contests_the_finding`;
`test_skeptic_stops_after_the_forced_turn_once_lookups_are_spent` now expects medium.

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

### Skeptic pass on new blocking findings (FND-5332)

lens publishes every finding the line review keeps after its fact-check. Here a new
critical or high finding first faces one independent model call that tries to refute it
(`skeptic.py`). A false blocker costs an author a blocked pull request, and the Release
Gate will require this verdict.

| | |
|---|---|
| Which findings | New this round (`review.run`, right after `merge_new`, so a finding carried over from an earlier round, a known id, is never re-checked), open, critical or high, and made by the model (`origin == "llm"`). Medium and low are never checked. In a later round it runs after `_check_new_against_base`. |
| What the skeptic gets | The finding (where, severity, category, title, body, quoted code), its causal chain as `<causal_chain>` (the trigger, each numbered step with where its quote was found, the quote and its effect, and the outcome; FND-5316), the principle it cites rendered as the line review saw it, the file's diff (200 lines at most), 40 lines of head code around it, and the read-only lookups `find_symbol`, `search_code`, `read_file`, `read_diff`. No `code_comment`, no `task_done`. A finding with no chain (raised before chains) gets `<when_it_fails>` from its `scenario` instead. |
| Its instruction | Check each step of the chain against the code (FND-5316). Until then it tried to refute the finding and defaulted to upheld when uncertain, so in the replay it upheld findings whose story the code did not support with "I cannot refute it". It answers with `skeptic_verdict`: `upheld`, `contested` or `refuted`, a reason, `steps` (`{step, holds, why}` per step), and for a refutation up to three `{path, line, quote}`. The prompt says not being able to refute a finding is not a reason to uphold it. |
| Upheld | Only when `steps` confirms every step 1..n of the chain with `holds: true` (`skeptic._parse_verdict`, in code), and `trigger_normal` is true (third replay, below). An upheld verdict that leaves a step out or marks one as not holding is contested, with `step N not confirmed: …` as the reason and the unconfirmed steps in the error. |
| Contested | A `contested` verdict, an upheld verdict short of the above, or a refutation that falls short (no reason, or no quote found). The finding drops to medium and is marked contested, as a refuted one is. |
| Refuted | Accepted only with a reason and at least one quote of at least 12 non-space characters (`skeptic.QUOTE_MIN`: `return` occurs in every file) that `diff.locate_in_text` finds in the head text of the file it names (`Workspace.text`, so a path outside the repository is no evidence). Before FND-5316 a refutation that fell short kept the finding blocking; it is now contested. The location recorded is where the quote was found, not the line the model claims. The finding drops to medium, and `Finding.contested` holds the reason (one line, no tag, no `\|`, at most 300 characters). It is not deleted: the summary shows "⚖️ contested — <reason>" after its title in the severity table and in its details, with a line that says it was raised as blocking. The status is then "not ready" (an open medium), never "changes requested" for it, and not green until it is fixed or dismissed, unless the head is waived (`/review waive <reason>`: `verdict_status` returns `success` for a waived head before it reads open findings, so the contested finding stays open under a green status); the pull request's author may now dismiss it, which `dismiss` refuses an author only for a critical or high finding. Critical and security findings can be lowered too, unlike `reflect`, which never removes them. |
| No verdict | `malformed` (an unknown verdict, or no verdict after the last turn) is contested, with the note "the skeptic gave no verdict, so the chain is not confirmed": the check ran and confirmed nothing (third replay, below). |
| No check made | Nothing changes. The outcome is recorded: `error` (a failed call), `budget` (the call could not be reserved) or `skipped` (past `max_findings`). Kept on purpose (FND-5316): no check was made, and a provider error must not unblock a real defect. A skeptic failure never makes the review incomplete. |
| Bounds | `[skeptic]` in config.toml: `enabled`, `max_lookups` (lookups per check, counted per tool call, not per turn: one response can ask for several, and only those within the remaining allowance run; each one past it is answered "Lookup limit reached" and the next turn is the forced verdict, after which the check stops; so at most `max_lookups` lookups and `max_lookups` + 1 calls), `max_tokens` (per call) and `max_findings` (checks per round). Each value must have its TOML type, and an unknown key is refused (`config._skeptic`, `Config.load_errors`), so `enable = false` cannot leave it on unnoticed. Checks run `[scope] concurrency` at a time, after the line review. |
| Budget | A ledger of its own carved out of the round's, `review.SKEPTIC_SHARE` (10%), the way the approach check has `APPROACH_SHARE`: when the skeptic is on and the round has bundles, the line review gets 10% less. Only spend is booked into the PR's ledger (`by_stage["skeptic"]`), so an unused share stays in the PR's cap. A call that cannot be reserved is not sent and the finding keeps its severity. |
| Log and report | One job-log line per outcome (`skeptic F-… high: refuted → medium, contested (1 lookup(s), 9.0s): …`; a contested check reads `contested` where this reads `refuted`), a count line, `timings_ms["skeptic"]`, `RunResult.skeptic`, and in the run report `skeptic.counts` (every outcome, zeros included) and `skeptic.checks` (id, severity, outcome, reason, evidence locations, lookups, error, seconds; never the quoted code). Its requests are listed after the approach check's. |
| State | `Finding.contested` is a new field with a default, so a state written before it decodes (`PRState.decode` keeps only known fields and fills the rest). |

Cost, estimated from the shipped prices (not yet measured on a live pull request): one
check reads about 5,000 to 10,000 prompt tokens per call and writes up to `max_tokens`,
for about $0.005 a call and at most four calls, so about $0.01 to $0.02 per blocking
finding. Checks run `[scope] concurrency` at a time, so the added wall time after the line
review is one check (one to four sequential calls) per wave of `concurrency` checks: with
the shipped `concurrency = 4` and `max_findings = 6`, up to four new blocking findings take
one check's time, and five or six queue into a second wave and take about two. A round with
no new blocking finding makes no call.

Tests: `Script` routes a request that offers `skeptic_verdict` to its own replies
(`skeptic=`, then upheld), so existing tests script the line review as before. Three
existing tests now count the skeptic call of their new high finding:
`test_first_round_puts_every_finding_in_the_summary_and_opens_no_thread`,
`test_every_request_is_recorded_with_counts_only_and_reported` and
`test_the_approach_check_runs_alongside_the_line_review`.

Tests adapted for the causal chain (FND-5316): the default upheld reply (`UPHELD`,
from `upheld()`) confirms steps 1 to 6. `COMMENT` carries a two-step chain, and the
other scripted comments (`OFF_DIFF`, `TUPLE_CONTRACT`, `REGRESSION_CLAIM`,
`COMMENT_NEXT`, `_dispose_finding`, the deleted-file and `_bracket` comments) carry
a one-step chain through their own quote (`chained`). The refutations that fell
short moved from `test_a_skeptic_that_cannot_refute_keeps_the_severity` and
`test_a_refutation_without_a_reason_keeps_the_severity` into
`test_a_refutation_without_proof_is_contested_not_upheld`; the rest is
`test_a_skeptic_that_makes_no_check_keeps_the_severity`. The principle-grading test
`test_a_principle_case_never_lowers_and_raises_only_on_its_own_match` is now
`test_a_principle_case_grades_both_ways_and_only_on_its_own_match`: its `high-stays`
and `critical-stays` cases now expect medium, and it gained a Low case on a line
that names none. `test_a_review_publishes_a_principles_high_case_as_blocking` checks
the `severity_case` enum with `low`.

### The SDK source at the version the app pins (FND-5331)

lens reviews application-sdk itself, so the code it reads is the SDK. This fork
reviews a connector app, and many connector bugs are a misuse of the SDK
(outputs, publish nodes, typed errors, storage). It can read the application-sdk
source at the exact version the app pins, read-only, beside the app. lens has no
equivalent.

- **Which version.** `sdk_source.py` reads the pull request head's `uv.lock`
  (FND-5316) with `tomllib`. It was the trusted base checkout's, which is the
  caller's default branch as it is today: a replay of a historical pull request
  that pinned an older SDK resolved today's SDK instead, and the review reasoned
  from the wrong source. The head's lock is read through the REST API as data
  (`sdk_source.head_lock`, `GitHub.raw_file_at`: the raw media type, because the
  JSON contents reply carries no content past 1 MB and an app's lock gets near
  that), never checked out. When the head has no lock or it cannot be read
  there is no ref and the reason is said; there is no fallback to the default
  branch's lock. The ref is therefore the pull request author's choice, bounded
  by the checks below to a release tag or a locked commit of
  atlanhq/application-sdk, read as text and never run.
  `atlan-application-sdk-api` (packages/api) ships from this repository at the
  SDK's own tag, so a lock that has only it resolves the same way
  (`SdkPin.package` names which). A registry pin of
  `X.Y.Z` or `X.Y.ZrcN` is the release tag `vX.Y.Z` (the scheme of every
  application-sdk release tag). A git pin of `github.com/atlanhq/application-sdk`
  is the locked commit uv writes after `#`. Anything else is no ref, with a
  reason: the package absent, two different pins, an editable or path source, a
  git pin of another repository, a version with no tag (a dev or post release),
  or a lock that does not parse. Both ref shapes are checked against a strict
  pattern before they are written as a step output. A git pin's version is only
  shown, never checked out, and is left out of the label unless it is plain
  version characters: a TOML string can hold a newline (a second line in
  `$GITHUB_OUTPUT`) or a quote (out of the prompt's attribute). The prompt
  escapes the label as well.
- **Other Atlan SDK packages (FND-5316).** An app can pin an Atlan SDK package
  that does not ship from this repository: on the replay above, a `server/`
  package pinned `atlan-server-sdk` by a direct git reference to its own
  private repository, in `server/pyproject.toml` and in no lock. The workflow's
  tokens cannot read that repository, so `sdk:` cannot cover it.
  `sdk_source.other_pins` finds every `atlan-…sdk…` package other than the SDK,
  its api package and the conformance tool, in the head's lock and in each
  `pyproject.toml` path the workspace knows (at most 20), every one read at the
  head under review (`review.sdk_pins_block`: the pull request's own text for a
  file it changed, `GitHub.raw_file_at` at the head otherwise), never from the
  checkout, which is the default branch. When the head's lock cannot be read,
  nothing comes from a lock (lens F-d40fdc). `pins_block` puts an
  `<other_sdk_packages>` block in the line review's context, with or without
  the SDK source: `sdk:` covers only application-sdk (which version), the
  package and how it is pinned, and not to judge its calls from application-sdk
  source. What is shown of a pin is plain text by pattern, and escaped.
- **Workflow.** `python -m connector_reviewer sdk-ref --repo … --event-name …
  --event-path …` runs after the App token is minted (reading the pull request
  needs `pull-requests` scope, which the job's own token lacks), takes the pull
  request from the event, writes `ref`, `label` and the `head` it read to
  `$GITHUB_OUTPUT` and always exits 0. `--root <dir>` alone still reads a local
  checkout's lock. A third checkout, of
  `atlanhq/application-sdk` at that ref, runs only when `ref` is not empty
  (`if:` on the step output). It is sparse (`/application_sdk/`), into `sdk-src/`,
  with `persist-credentials: false` and the job's read-only token. Both steps
  have `continue-on-error: true`: a ref that does not exist fails only the
  checkout. The Review step always gets `CONNECTOR_REVIEWER_SDK_SOURCE`
  (`--sdk-source`), the checkout's path, whether or not it exists.
- **One head (lens F-f65a20, F-9e1332).** The pull request can move between the
  resolve step and the review, so the review does not trust the step's ref.
  `review.run(sdk_dir=…, sdk_head=…)` reads the lock again at the exact head
  this round reviews (`sdk_source.head_lock_at`), resolves its pin, and
  `SdkSource.open` uses the checkout only when it is at the commit that pin
  names (`sdk_source.checkout_at`: `git rev-parse` of `HEAD` and of the ref,
  an annotated tag peeled, in the checkout alone, with `GIT_CEILING_DIRECTORIES`
  so it never reads an enclosing repository). A directory that merely holds
  `application_sdk/` is no longer enough. When a push moved the pin, the review
  runs without the SDK source and its note names the two heads
  (`CONNECTOR_REVIEWER_SDK_HEAD`, the step's `head`) and says `/review force`
  reviews with the right one. When the push left the pin alone, the checkout
  is still that head's source and is used.
- **Tools.** One prefix on the existing tools, not new tools: a `file_path` of
  `read_file`, a `path_glob` of `search_code`, or a `name` of `find_symbol` that
  starts `sdk:` goes to the SDK source (`tools.SdkSource`), e.g.
  `sdk:application_sdk/outputs/__init__.py`. Every hit and definition carries the
  prefix, so it can be handed back to `read_file`. Why a prefix: the tool
  schemas, part of the cached prompt prefix, do not change; the approach check,
  which takes a subset of the same schemas and the same `run_tool`, needs no
  change of its own to use the prefix later (today it is not told about it, see
  Prompt); and the model already knows the three tools. Without the prefix every tool is the app only,
  as before. `Workspace.text` and `Workspace.files` stay the app only too: the
  context and facts code walks them, and must not count SDK callers or slow
  down.
- **Safety.** The same path guard as the app (`_safe_rel`: no absolute path, no
  `..`, no secret-bearing path), then an allowlist: only `application_sdk/` is
  reachable, so a stray file beside it is not. A symlink is refused, and a file
  must resolve inside `application_sdk/`, so a symlinked directory cannot lead
  out. The walk does not follow links. Files over 1 MB are not read.
- **Cost.** Nothing is walked or indexed until the model asks. The file list is
  built on the first `sdk:` search or `find_symbol`; the index (`index_source`
  over the SDK's Python files, about 1 s for 460 files at v3.58) on the first
  `find_symbol("sdk:…")`, once, under a lock, since bundles share one Workspace.
  It stops at 20 s and says the index is partial. A search over the SDK took
  about 0.06 s. Its tests are not checked out, so `find_symbol` lists no tests
  for an SDK symbol.
- **Prompt.** `REVIEW_SYSTEM` is unchanged. When the source is there, the line
  review's context message has an `<sdk_source version="…">` block (from
  `SdkSource.prompt_block`, rendered in `agent.build_context`): the version, the
  three calls, and when to use them (to check how an SDK API the diff calls
  behaves before claiming the app misuses it; the SDK is reference, never under
  review). The approach check, verify and re-check are not told.
- **Fallback.** Without the source, the prompt and the tools are as before, and a
  `sdk:` call answers that the source is not available. When it was asked for
  (the variable or the flag, as in CI) and is not usable, `review.run` adds a
  note, "SDK source not available: <reason>", to the summary, and the job log says
  the same. A local run without either changes nothing and adds no note.
- **Open.** A live review on a connector pull request that reads SDK source, and
  its cost and time against today's reviews (the ticket's last two items).

## Module by module

Where a lens fix lands in this copy. "Names" means only the renames in the table
above; "typing" means only the annotations described under Typing.

| Module | Against lens at the fork point |
|---|---|
| `baseline.py`, `diff.py`, `index.py` | Identical, byte for byte. A lens patch applies as it is. |
| `lock.py` | Names. |
| `report.py` | Names; bundle time, forced final turn and dropped uncited findings; the incomplete list from `incomplete_reasons`; the skeptic counts and checks (FND-5332); triage counts only withheld files, the `untraced_dropped` reasons, and a contested check marked "→ medium" (FND-5316). |
| `findings.py` | Names; `PRState.waiver`, `exempt`, `unread`, `facts`; `Finding.contested`; `unique_ids` and the `salt` of `Finding.fingerprint`, `Finding.chain` (not stored), and `HARMS`, `OUTCOME_FLOOR` and `TEST_GAP` (FND-5316). |
| `triage.py` | Typing (one annotation); `Triage.withheld`, and `summary_lines` reads it (FND-5316). |
| `tools.py` | Two description strings (`compatibility`, `search_code`), the comment's optional `principle` field, and `task_done`'s optional `regression` and `history`; the comment's optional `severity_case` and `severity_basis` (FND-5316); `SdkSource`, `Workspace.sdk` and the `sdk:` prefix in `read_file`, `search_code` and `find_symbol`, with the path checks factored into `_safe_rel` and `_read_inside` (FND-5331); `Workspace.sdk_pins` and `SdkSource.package` (FND-5316); the comment's required `causal_chain` (with `harm` and `normal_path`), `low` in `severity_case`, and a line in `SdkSource.prompt_block` that a step may cite an `sdk:` path (FND-5316). |
| `trace.py` | Names, typing. |
| `select.py` | `DEFAULT_EXCLUDE`, and `excluded` (the pattern match, public for `review.changed_tests`). |
| `config.py` | Docstring, `raw_hash`, typing; the `[access]`, `[exempt]`, `[facts]` and `[skeptic]` tables. |
| `bundle.py` | `_module_key` and the module docstring. |
| `llm.py` | Names (the gateway key variable, the User-Agent, log text), typing. |
| `github.py` | Names, typing, four methods removed with the approval step; `can_push`, `safe_body`, `status_of`, `pr_commit_messages`, `recent_commits`, `commit_patches` and `raw_file_at` (FND-5316) added; `set_status` records each verdict in the job's temp directory (`record_verdict`, `recorded_verdicts`), which `finalize` trusts over a lagging combined status (FND-5316). |
| `event.py` | Rewritten around who may ask (FND-5310); the command grammar is lens's plus `waive`. The unknown-word check is the same in both, under each copy's own names: `REVIEW_USAGE` and `REVIEW_OPTIONS` here, `LENS_USAGE` and `LENS_OPTIONS` in lens. Two differences (FND-5314): here the subcommand (`dismiss`, `recheck`, `waive`) is read from the first line only, so a note on a later line that starts with one of those words still reviews (ids and reason may continue below a first line that names the subcommand), where lens reads the comment's second word wherever it is; and `force` counts in either order with `verify-budget`, where lens forces only when `force` comes first. |
| `holistic.py` | The prompt's opening and one concern; no `<public_api>` block; typing. |
| `context.py` | No `_public` / `public_api`; `build` returns a string. |
| `advisory.py` | Rewritten as the empty extension point; holds `glob_match`. |
| `agent.py` | Names; principles where lens has rules; no released-surface block; typing; `cite`, the test-file cap, the per-bundle time limit (FND-5313); the facts blocks and the `task_done` answers (FND-5312); the `<sdk_source>` block (FND-5331) and the `<other_sdk_packages>` block (FND-5316); `grade_by_principle` (FND-5316); `unique_ids` before grading, and `locate_step`, `trace_chain`, `render_chain`, `traced` and `BundleResult.untraced`, and `principle_case` and `bound_by_outcome` (FND-5316). |
| `prompts.py` | The review prompt's opening, "Repository layout", "Principles" (with the Severity-line grading of FND-5316) and "Facts" sections; the causal-chain bullets of "Evidence", and the two-way principle grading and the outcome floor and cap in "Principles" and "Severity" (FND-5316); the removals; typing. |
| `review.py` | Names; the removals; `RISKY_PREFIXES` as tiers; principles where lens has rules; typing; exempt and waive; the verdict rules and the approach check alongside the line review (FND-5313); `compute_facts` and the Regression and History lines (FND-5312), with `_whole_files` joining a file reviewed in parts back into one diff before its facts are computed; the skeptic pass, `SKEPTIC_SHARE` and the contested mark (FND-5332); `run`'s `sdk` and `sdk_note` (FND-5331); `run`'s `sdk_dir` and `sdk_head`, opening the SDK source against the reviewed head's lock, and `sdk_pins_block`, and `plan_bundles` packing the shipped code apart from tests past the cap, with `_cap_skips` naming the lines left unread (FND-5316); `unique_ids` across bundles before `merge_new`, and the mechanical box and scope trace from `Triage.withheld`, and the contested note's wording (FND-5316). |
| `__main__.py` | Names; no `approve` sub-command; the config directory default; `--principles-dir`; the `finalize` sub-command, and the crash handler calls it; the `sdk-ref` sub-command and `--sdk-source` (FND-5331), `sdk-ref` reading the pull request head's lock and writing that head, and `--sdk-head` (FND-5316). |
| `__init__.py` | Docstring. |
| `principles.py`, `typed.py`, `exempt.py`, `finalize.py`, `facts.py`, `skeptic.py`, `sdk_source.py` | New here. |
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

## Fixed in both copies (FND-5314)

Neither of these is a difference: each copy has the same behaviour, under its own
names.

- A finding on code the PR removes. Its quote is absent at the head by design, so
  verify and `recheck` both show where it was removed, prefixed with
  `review.REMOVED_NOTE` ("its absence is not a fix"), or say the PR deletes the whole
  file (`review._removed_site`). In `recheck_evidence` the merge-base fact says
  the code is on the merge base and this PR removes it, and it is never "absent".
  The symbol around the removal site frames `<code_now>` only: it gives no
  merge-base, caller, privacy or capability fact (the last two in lens only). So a
  compatibility claim about the removal cannot resolve with no model call because
  that neighbour is new. In `recheck_evidence`, a head match that sits on a line
  the PR added, of a quote the PR also removes (`review._new_copy_of_removed`), is
  a new copy rather than the quote surviving, so the removal stays the subject.
- An unknown word on the command's line (`/review frobnicate`, `/lens forse`) gets a
  usage reply and starts nothing. Options come from the first line only, so a note
  on the lines below a bare `/review` or `/lens` still reviews.

## Not changed, and worth knowing before the next change

Nothing listed at the moment. The three items that were here (the test-file
severity cap, the verdict rule for a nit, and the unused `Finding.rule_id`) are
decided under "Verdict rules (FND-5313)".
