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

See [preflight conformance](conformance/docs/preflight-testing.md) for static checks, opt-in behavior scenarios, and their coverage limits, and the [F-series rule catalog](conformance/docs/rules/preflight.md) for the rules themselves.

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

See `conformance/programs/conformance-remediation.prose.md` for the
`/remediate` skill entry contract.
