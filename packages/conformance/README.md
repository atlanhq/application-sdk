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

## In CI

Consumer repos should reference this package via the reusable workflow in
`atlanhq/application-sdk`:

```yaml
uses: atlanhq/application-sdk/.github/workflows/conformance-reusable.yaml@main
with:
  sdk-ref: main
```

See `conformance/programs/conformance-remediation.prose.md` for the
`/remediate` skill entry contract.
