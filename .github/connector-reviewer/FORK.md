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
