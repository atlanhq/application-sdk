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
`pytest .github/scripts/tests` on every PR:

```bash
uv run --extra workflows --with pytest python -m pytest .github/scripts/tests -q
```

If you add a new script, add its test in the same PR — the workflow will pick
it up automatically.

The trigger is deliberately **not** path-filtered. Part of this suite consists of
cross-file guards that read YAML outside `.github/scripts/` — the mask-before-write
call-site check below is one — and any `paths:` list would have to enumerate every
file they inspect, so it would silently stop firing on exactly the edits those
guards exist to catch. Don't add one; a sub-minute suite is cheaper than that
failure mode. Because it now concludes on every PR, it can be added to branch
protection directly, without the always-concluding-gate wrapper that
path-filtered required checks need (see `sdk-gate.yaml` for that pattern).

## Mask secrets before writing them to `$GITHUB_ENV`

**Rule:** if a step derives secret values from something else — unpacking a
composed blob, decoding a bundle, reading a credentials file — it must register
each derived value with `::add-mask::` *before* the value reaches `$GITHUB_ENV`,
`$GITHUB_OUTPUT`, or anything a later step prints.

**Why:** the runner's log masker replaces occurrences of each *registered
string*. It does not match substrings of one. Registering a composed blob
(`toJSON(...)` of several secrets, say) therefore redacts the blob and nothing
inside it, so the first later step that renders its `env:` group prints each
extracted value in cleartext. `secrets: inherit` does not help: inheritance
controls which secrets a job can read, not which strings the masker knows.

**The stdout constraint:** `::add-mask::` is a workflow command the runner reads
from the step's **stdout**. A script whose stdout is redirected into
`$GITHUB_ENV` cannot emit mask commands on that stream — they would be written
into the env file as garbage. Do not fall back to stderr; workflow commands on
stderr are not a documented guarantee. Split the two outputs into two modes and
call the masking one first, so a failure aborts the step before anything is
written:

```yaml
- name: Export caller-supplied credentials
  run: |
    set -euo pipefail
    python3 .github/scripts/<driver>.py --json "$PAYLOAD" --mask-only
    python3 .github/scripts/<driver>.py --json "$PAYLOAD" >> "$GITHUB_ENV"
```

Set `-e` explicitly rather than relying on the runner's default shell: a
workflow- or job-level `defaults: run: shell:` added later drops the implicit
`-e`, and the step would then fail *open* — the mask pass errors, the env write
still runs, and the values land unregistered. Composite actions declaring
`shell: bash` are immune (they ignore workflow defaults), but it costs one line
to make the property local to the step.

Mask every scalar, not the ones that look sensitive — the caller decides what
goes into the payload, so a driver that guesses will guess wrong. Skip empty and
whitespace-only values (the runner refuses those with a warning). Multi-line
values need each line registered as well as the whole value: the masker is
handed log output a line at a time, so a registration spanning newlines matches
nothing. Never put a value in an error message.

**What masking does not cover:** `::add-mask::` rewrites the **log stream** the
runner uploads. It does not touch bytes on disk. A step that `tee`s output into a
file — `results/`, a JUnit XML, a container log — writes the value unredacted,
and `upload-artifact` then ships it to anyone with repo read access for the
retention window. Masking is not a substitute for not writing the secret:
anything destined for an artifact has to be scrubbed at the source that produces
it, or kept out of that file in the first place.

`export_extra_env.py` and `test_export_extra_env.py` are the worked example,
including a static check that every call site masks before it writes.

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
