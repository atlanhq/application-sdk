# CI authoring standard

Guidance for writing and changing GitHub Actions workflows in this repo.

## No conditional logic in inlined shell

**Rule:** a workflow `run:` block must not contain conditional logic — `if`/`else`,
loops (`for`/`while`), `case`, or non-trivial command chaining whose behaviour
branches on state. The moment a step needs to *decide* something, that decision
moves into a script.

**Where the logic goes:** a script under `.github/scripts/`, written in Python
(the established convention — see the existing drivers there), with a pytest
beside it in `.github/scripts/tests/`. The workflow step then just invokes it:

```yaml
- name: Do the thing
  run: python3 .github/scripts/do_the_thing.py --flag "${{ inputs.flag }}"
```

Inlined `run:` blocks are fine for **straight-line** commands — install a tool,
`curl` a binary, `git push`, run a linter. The bar is specifically *branching*.

**Why:** logic inlined in YAML cannot be unit-tested, so it regresses silently
as the automation evolves. A script can be exercised against fixtures —
success paths, failure/fallback paths, edge cases — so a refactor that breaks a
branch fails a test instead of breaking production CI. It is also reviewable in
isolation and reusable across workflows.

### Testability seam

Side-effecting commands (`pkl`, `git`, `ruff`, network calls) should go through
a single thin wrapper function so tests can stub the external tool and let the
rest run for real. See `.github/scripts/renovate_pkl_sync.py` and its test
(`.github/scripts/tests/test_renovate_pkl_sync.py`) for the pattern: `pkl`/`uvx`
are stubbed, `git` runs against a throwaway repo in `tmp_path`, and every
branch (opt-in gate, missing input, eval-failure fallback, no-change
short-circuit) has a case.

## Running the script tests

The `CI script tests` workflow (`.github/workflows/scripts-tests.yaml`) runs
`pytest .github/scripts/tests` on PRs that touch `.github/scripts/**`:

```bash
uv run --extra workflows --with pytest python -m pytest .github/scripts/tests -q
```

If you add a new script, add its test in the same PR — the workflow will pick
it up automatically. To make this gate *blocking*, add it to branch protection
behind an always-concluding gate (see `sdk-gate.yaml`), since a path-filtered
required check otherwise sits pending forever when its paths are untouched.

## Reusing scripts from a reusable workflow

A `uses:` reusable workflow does **not** bring its own repo's files into the
caller's checkout. To run an SDK script from a reusable that other repos call,
sparse-checkout it into a side path and invoke from there — the
`.sdk-scripts` pattern used by `release-version-bump.yaml` and
`renovate-pkl-sync.yaml`:

```yaml
- uses: actions/checkout@<sha>   # consumer repo (the working tree the script acts on)
- uses: actions/checkout@<sha>   # SDK scripts, into a side path
  with:
    repository: atlanhq/application-sdk
    ref: main
    sparse-checkout: .github/scripts
    sparse-checkout-cone-mode: false
    path: .sdk-scripts
- run: python3 .sdk-scripts/.github/scripts/<driver>.py ...
```

The driver runs from the consumer's working directory, so it acts on the
consumer's files; `.sdk-scripts` only holds SDK code and must never be staged.
