# atlan-application-sdk-conformance

Dev-only conformance suite, remediation programs, and CLI for the
[Atlan Application SDK](https://pypi.org/project/atlan-application-sdk/).

**Do not add this as a production dependency.** It is intended for developer
machines and CI only.

## Installation

```bash
uv add --dev atlan-application-sdk-conformance
```

## Usage

```bash
# Run the conformance suite
uv run atlan-application-sdk-conformance detect --repo . --series E,L,C --output report.sarif

# Get the path to bundled remediation programs (for SKILL.md / reactor)
uv run atlan-application-sdk-conformance programs-dir

# Regenerate rule docs
uv run atlan-application-sdk-conformance gen-rule-docs
```

## SDK-derived data

`conformance/data/` holds tables generated from SDK (and `pyatlan_v9`) source, baked into the wheel
because the suite runs in consumer repos where that source is not available.
Never hand-edit them: regenerate in the same PR as the SDK change, and each
drift test under `tests/` fails until you do. Every generator takes `--check`.
The drift tests for `deprecated_symbols.json`, `public_errors.json`,
`sdk_type_aliases.json` and `toolkit_baseline.json` (plus the contract-ledger,
dependency-audience and removed-imports guards) find the monorepo root with
`tests/_helpers.py:sdk_root()`, which fails under `CI` rather than skipping.
The `sdk_contract_fields.json` drift test does not use it yet and can still skip.

| Table | Command |
|---|---|
| `deprecated_symbols.json` | `gen-deprecations` |
| `public_errors.json` | `gen-public-errors` |
| `relationship_directions.json` | `gen-relationship-directions` |
| `sdk_contract_fields.json` | `gen-sdk-contract-fields` |
| `sdk_type_aliases.json` | `gen-sdk-type-aliases` |
| `toolkit_baseline.json` | `gen-toolkit-baseline` |

Run each as `uv run atlan-application-sdk-conformance <command>` from
`packages/conformance/`.

## Developing in this repository

`packages/conformance` is a member of the repository's uv workspace, so it has
no `uv.lock` of its own: the root `uv.lock` resolves it together with the SDK,
and its `test` extra installs this checkout's SDK, editable. From
`packages/conformance/`, `uv run --extra test pytest` runs the package's tests
against the tree beside them. A dependency change here relocks with `uv lock`
anywhere in the repository.

See [preflight conformance](conformance/docs/preflight-testing.md) for static checks, opt-in behavior scenarios, and their coverage limits, and the [F-series rule catalog](conformance/docs/rules/preflight.md) for the rules themselves.

## Grading a change instead of the repository (`--since`)

A plain `detect` grades the repository: every blocking finding fails it, old
or new. That is what the scheduled run and the dashboard want. A pull request
wants a different question answered: did *this change* add a blocking finding?

```bash
# Fail only on blocking findings added since the fork from origin/main
uv run atlan-application-sdk-conformance detect --repo . --since origin/main --output report.sarif

# The same, through the environment (how PR CI sets it for every series step)
CONFORMANCE_SINCE=origin/main uv run atlan-application-sdk-conformance detect --repo . --output report.sarif
```

- The exit code is non-zero only for blocking findings the working tree has
  over the merge-base of the ref and `HEAD`. Each is printed as `[INTRODUCED]`.
- Findings the base already carries are still printed and still written to the
  SARIF, which stays the whole-repository result. They do not fail the run.
- Warnings never fail the run, with or without `--since`.
- A file that cannot be parsed still fails the run, exactly as without
  `--since`: it is not compared with the base, because a clean verdict must
  mean every discovered file was scanned.
- The working tree is compared, so an uncommitted change is judged the same as
  a committed one, including a file moved with a plain `mv`.
- `--repo` may be a subdirectory of the checkout; the same subdirectory of the
  base is what it is compared with.
- Findings are counted per rule, file and discriminator rather than matched
  line by line, so moving or rewording an existing violation does not count as
  adding one. A change that fixes one violation and adds another of the same
  rule in the same file nets to zero and passes.
- The base is only scanned when the working tree has blocking findings, and
  then only for the rules that fired, in a temporary `git worktree`.
- If the base cannot be compared (a shallow clone, an unknown ref, no git), the
  run says `falling back to the whole-repository verdict` and does exactly
  that: stricter, never laxer. No `[INTRODUCED]` line is printed then, so a
  failure after that message is about the missing base, not about the change.
- `--exit-zero` still forces exit 0, unless `--gate-introduced` (or
  `CONFORMANCE_GATE_INTRODUCED=true`) is set: then a blocking finding the
  change introduces fails the run anyway. `--exit-zero` keeps excusing
  what the base already carries, warnings, and a base that could not be
  compared.

`conformance-reusable.yaml` sets `CONFORMANCE_SINCE` to the base commit and
`CONFORMANCE_GATE_INTRODUCED=true` on `pull_request` and `merge_group`
events. Push, scheduled and dispatched runs leave both unset and grade the
whole repository.

So a caller's soft mode (`exit-zero: true` in its `conformance.yaml`) now
means one thing: the repository's existing backlog does not fail a run. It
no longer lets a pull request add a blocking finding. A repo that needs the
old behaviour back sets the repository variable `CONFORMANCE_SOFT_PRS=true`.

## Suppressing a finding

Most findings are suppressed inline, next to the violating line:

```python
x = 1  # conformance: ignore[E001] reason
```

A rule that checks for something *missing* repo-wide (no line to attach an inline
comment to) is instead exempted via a `[tool.conformance]` key in the repo's own
`pyproject.toml` — see
[`docs/schema-contract.md` §6.4](conformance/docs/schema-contract.md#64-config-based-tier-exemption-toolconformance)
for the canonical convention and when to use it over inline suppression.

## Files that do not parse

A Python file the runner cannot parse is skipped by every check, so it would
otherwise look clean. The runner reports each one instead: a `[PARSE]` line in
the log, a SARIF `toolExecutionNotifications` entry, `executionSuccessful:
false`, and a failed gate. Fix the syntax error, or `--exclude` the path if it
is deliberately not Python. See
[`docs/schema-contract.md` §8](conformance/docs/schema-contract.md#files-the-suite-could-not-analyse).

## In CI

Consumer repos should reference this package via the reusable workflow in
`atlanhq/application-sdk`:

```yaml
uses: atlanhq/application-sdk/.github/workflows/conformance-reusable.yaml@main
# No inputs required — the published PyPI package is used by default.
```

Most `canonical_reference` values cite one of the three reference apps; a few
SDK-seam rules (for example P057) cite this SDK instead.
`.github/workflows/canonical-reference-check.yaml` checks app claims nightly
against the apps' `main` and SDK claims against this checkout — every cited
path, `path:N` line span and backticked symbol must exist, and anything a
reference names as absent must still be absent:

```bash
python -m conformance.tools.verify_canonical_references --sdk-root ../.. \
  --app atlan-openapi-app=<checkout> --app atlan-mysql-app=<checkout> \
  --app atlan-metabase-app=<checkout>
```

See `conformance/programs/conformance-remediation.prose.md` for the
`/remediate` skill entry contract.
