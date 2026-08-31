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

## Label gates must be event-aware

**Rule:** if a workflow can receive a `labeled` event, every job gated on a
label must check *which* label fired, not only whether the PR carries it:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, labeled]

jobs:
  expensive-thing:
    if: |
      contains(github.event.pull_request.labels.*.name, 'e2e') &&
      (github.event.action != 'labeled' || github.event.label.name == 'e2e')
```

**Why:** `contains(...labels...)` is a **state** check — "does this PR carry
`e2e` right now". On `opened` / `synchronize` / `reopened` that is exactly
right. On `labeled` it is wrong, because GitHub offers no way to filter the
trigger by which label was added: on a PR that already carries `e2e`, adding
*any* other label satisfies the state check and re-runs the job. Bots add and
remove size, area, dependency and review-state labels constantly, so in practice
this fires repeatedly and at random. FND-48 was the e2e case — a 20–40 minute
live-tenant suite, multiplied by the cross-CSP matrix, and queued rather than
replaced because `cancel-in-progress: false` is deliberate there.

The added term is inert on every other event: only a `labeled` payload carries
`github.event.label`, so a real push (`synchronize`) still re-triggers, and
adding — or re-adding — the label still triggers.

**What it does not fix:** the workflow *run* is still created, because the
filter is at job level and GitHub has no trigger-level label filter. The jobs
skip within seconds, so no tenant time or runner minutes are consumed, but a
stream of skipped runs still appears in the Actions list. Removing those means
dropping `labeled` for an explicit signal (`workflow_dispatch`, a `/e2e`
comment command, a re-run button) — more work, and it changes how everyone
triggers the suite today.

**Reusable workflows always count as reachable.** A `workflow_call` workflow
cannot see its callers' trigger lists, and the connector `tests.yaml` this repo
scaffolds ships `labeled`, so a gate inside a reusable must carry the term
unconditionally.

Both halves are enforced by
[`test_label_trigger_gates.py`](../../.github/scripts/tests/test_label_trigger_gates.py):
a repo-wide sweep fails any new label gate that omits the term, and a
behavioural layer lifts each real gate out of the YAML and evaluates it against
synthetic payloads. The second layer exists because presence is not
correctness — `&&` binds tighter than `||` in GitHub expressions, so a term
added one parenthesis out is a no-op that a textual check waves through.
Evaluation uses [`_gha_expr.py`](../../.github/scripts/tests/_gha_expr.py), a
deliberately partial evaluator that raises on anything it does not model rather
than guessing.

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

## Never pass a credential as a Docker `build-arg`

**Rule:** a step that builds an image must not put a token, password, or key in
`build-args:`. Feed it through `secrets:` instead, and have the Dockerfile read
it with `RUN --mount=type=secret`.

```yaml
# WRONG — publishes the credential with the image
build-args: |
  ACCESS_TOKEN_PWD=${{ secrets.ORG_PAT_GITHUB }}

# RIGHT — the value is available during the RUN and nowhere after it
secrets: |
  git_token=${{ secrets.ORG_PAT_GITHUB }}
```

```dockerfile
RUN --mount=type=secret,id=git_token,uid=1000 \
    UV_GIT_TOKEN="$(cat /run/secrets/git_token)" \
    uv sync --frozen --no-dev
```

**Why:** a build arg is not a build-time-only variable. BuildKit records the
`ARG` name *and its value* in the image config, so `docker history <image>`
prints it back to anyone who can pull the image — no repo access, no Actions
log access, no runner access needed. The credential is published as part of the
artifact, and it stays published in every tag ever built that way; deleting the
workflow line does not retract the images. `::add-mask::` does not help here for
the same reason it does not help with artifacts: it rewrites the log stream, not
bytes written elsewhere.

A `--mount=type=secret` is exposed as a tmpfs file for the duration of one `RUN`
and is not part of that layer, the image config, or the history. It is also the
only form that survives layer caching correctly: a build arg changes the cache
key, so rotating the credential invalidates every layer after the `ARG`.

**Migrating a consumer:** `build-args` and `secrets` are not interchangeable at
the Dockerfile end, so the two sides have to move together. Land the Dockerfile
change first — a Dockerfile that mounts `git_token` ignores an
`ACCESS_TOKEN_PWD` build arg it no longer reads, so it builds green under both
the old and the new workflow — then drop the build arg from the workflow. Doing
it the other way round leaves the build with an empty credential, and `uv sync`
against a private git dependency fails on the next build rather than at merge.

**Scope note:** this covers credentials only. Non-secret build inputs — a
version string, a target arch, a base-image reference — are what `build-args`
are for, and recording those in image history is a feature.

## Renovate post-upgrade commands

Two run today, both installed as bare PATH commands by `.github/workflows/renovate.yaml`
and both declared in `renovate-config/default.json`:

| command | lane | what it does |
|---|---|---|
| `renovate-pkl-sync` | `app-contract-toolkit` | re-resolves the Pkl lock and regenerates contract artifacts |
| `renovate-uv-lock-bounded` | `lockFileMaintenance` | re-resolves `uv.lock` under the org §5 release-age bound, then strips uv's `[options]` block |

Three rules apply to any command added here.

**No `${VARS}`.** Renovate does not shell-expand post-upgrade commands, so a
variable is passed through literally. Everything the command needs must be a
literal argument.

**Every command needs a matching regex in `allowedCommands`** in
`renovate-config/self-hosted.js` — an admin-only option, which is why the fleet
runs a self-hosted runner at all. A command the allowlist does not match is
**skipped with a log line and nothing else**, so the drift is invisible exactly
when it matters: in FND-367 a bound that was not yet allowlisted meant the lock
refreshed unbounded and took a package published three minutes earlier, with
nothing red anywhere. `.github/scripts/check_renovate_allowed_commands.py`
asserts the preset↔allowlist pairing in CI for that reason. Keep allowlist
entries free of character classes and backslashes: a `]` truncates the array the
guard parses, and `"\d"` in a JS string literal is just `"d"`.

**Anything that writes a lockfile must leave it valid for every consumer.** uv
records its resolver settings into `uv.lock` under `[options]`, and every
`uv sync --locked` compares its own settings against that block — so a bound
applied at lock time and left recorded makes the lock unusable in the app
Dockerfiles. That is what reddened `scan / Build Image` fleet-wide in #3212. The
bounded driver strips the block; if you add another lock-writing command, check
what it records.

### A refused lock refresh is red on purpose, and nothing re-evaluates it

`withhold()` in `renovate_uv_lock_bounded.py` refuses by writing the baseline
versions plus a bare `[options]` table — the one lever the driver holds over a
*required* check, since `uv sync --locked` then rejects the lock and
`scan / Build Image` holds the branch. Two consequences follow, and both bite:

**Nothing expired it, so the driver stamps why it refused.** The branch is not
behind its base, so Renovate reuses it without re-running `postUpgradeTasks`, and
the marker committed into `uv.lock` carries no clock. Observed 2026-08-24: five
fleet lock PRs sat frozen for two to four days after the window that blocked them
had opened. `withhold()` now writes the reason on the tripwire's own line
(FND-909), which is what lets a machine tell the two kinds apart:

```
exclude-newer-span = "P3D"  # refusal: window-empty
```

Only `window-empty` heals on its own — the bound admitted nothing on that pass,
which the next slice of window routinely fixes. A broken interpreter, an
unsatisfiable floor, a floor that was admitted and still failed, and a yanked-pin
rollback are standing faults that waiting never clears.

**A reaper clears the self-healing half.** `renovate_reap_refused_locks.py` runs
immediately before Renovate in each repo's matrix job, deletes a stamped
`window-empty` branch on sight, and Renovate rebuilds it in the same pass —
`recreateClosed: true` in the shared preset is what makes that safe. Standing
faults keep their tripwire and stay red for a human; recycling a real wedge every
four hours would hide it behind a lane that looks busy. An *unstamped* tripwire
is also left alone: "no reason given" must never read as self-healing.

**The fleet dashboard names both, and one of them alarms.**
`conformance.renovate.classify` splits the shape out of `checks_failing` once
three things hold — the PR is lock-maintenance, its diff is a `uv.lock` and
nothing else, and that lock carries a lone `exclude-newer-span` (uv's own
`[options]` always records the absolute `exclude-newer` beside it, so a lone span
is the driver's signature). The stamp then decides which finding it is:

| Finding | When | Who acts |
| -- | -- | -- |
| `bounded_lock_refusal_standing` | stamped with a reason outside the self-healing set, immediately | a human |
| `bounded_lock_refusal_expired` | stamped self-healing and older than `REAPER_GRACE` (two fleet passes), **or** unstamped and older than the window it names | nobody — the reaper should already have taken it |

`renovate_refusal_alarm.py` runs last in `renovate-dashboard.yaml` and fails the
run on `bounded_lock_refusal_expired`, because reaching that state means the
reaper did not run — and the reaper never fails its own job, so the outage is
otherwise silent. Standing faults are printed and never fatal: a wedge a human is
legitimately still working through must not red a six-hourly job, or the alarm
stops being read.

The self-healing vocabulary lives twice — in the driver that writes stamps and in
the classifier that reads them — because the driver runs as a bare `python3` on
the fleet runner and cannot import the conformance package. A test in
`.github/scripts/tests/` pins the two equal; without it, adding a reason to the
writer alone would silently make the alarm stop firing.

## The release-age bound on application-sdk's own lock refresh

`postUpgradeTasks` reaches every repo in the fleet except the one that publishes
the wheel they all install. `allowedCommands` is admin-only, so post-upgrade
commands exist only on the self-hosted runner, and application-sdk's dependency
PRs come from Mend. `renovate-lock-cooldown.yaml` closes that gap (FND-376) by
running the same driver from the other side: triggered by the push Mend makes to
`renovate/lock-file-maintenance`, correcting the lock in place before the PR
merges.

Three things about that position differ from the fleet's, and each is a way to
get it silently wrong:

**The baseline is the base branch, not `HEAD`.** Under `postUpgradeTasks` the
refresh is still uncommitted, so `HEAD` is the pre-refresh lock — which is why
that is the driver's default. Here Renovate has already committed it, so `HEAD`
*is* the unbounded lock, and using it would derive every retention ceiling from
the releases the bound exists to exclude. The bound would apply, report success,
and change nothing. Hence `--baseline-ref origin/<default-branch>`, which also
makes "never roll back what main ships" structural: the driver's rollback gate
then compares against main by construction.

**Two uv projects, two exempt sets.** `packages/conformance` is a separate uv
project, and unlike the root it resolves `atlan-application-sdk` from PyPI — so
its exempt set carries the SDK *and* pyatlan. Exempting the SDK alone does not
fail; a bounded resolve that cannot see a fresh pyatlan silently backtracks to an
older SDK instead.

**One commit, not one per lock.** Every push to the branch re-fires the PR's
entire required-check suite, so the bound rewrites both uv locks and the npm lock
in a single commit.

The workflow pushes with a minted App token, not `GITHUB_TOKEN`: a `GITHUB_TOKEN`
push does not re-trigger the PR's required checks, which would leave the PR green
against a commit that is no longer its head.

The job runs only for a push made by one of the Renovate identities the approval
gate also accepts as PR authors (`RENOVATE_AUTHORS`), pinned to that list by a
drift test rather than copied. Treat it as defence in depth and not as a control:
it is evaluated from the workflow file at the pushed ref, so it constrains what
runs by accident, not what runs by intent. Restricting who may push a branch is
branch configuration and lives outside the repo.

### Any bot commit on a Renovate branch loses `renovate/artifacts`

Commit statuses are per-SHA and Renovate stamps only the commits it authors, so
the moment an in-repo workflow adds a commit the head moves off the stamped SHA.
Condition (f) of the approval gate reads an absent `renovate/artifacts` as
not-green — correctly, since a post-upgrade command that was skipped and one that
ran clean are otherwise indistinguishable — so approval is withheld and stays
withheld. This is not new and not hypothetical: on #3216 atlan-ci posted three
approvals, each dismissed by the next push, and the PR merged on a human's
approval after the since-removed requirements sync added the last commit.

`carry_artifact_status.py` closes it for this lane by republishing the state it
**reads from our commit's parent**, and only when that state is `success`. A
`failure`, a `pending`, an absent context, or an unreadable one all publish
nothing and leave the gate withholding. It waits briefly for an absent context
before giving up, because Renovate pushes the branch before it sets its statuses
and "not yet" would otherwise read as "never".

If you add another workflow that commits to a Renovate branch, it inherits this
problem. Carry the status or accept that the PR needs a human.

### Repo-local `postUpgradeTasks` override

`renovate.json` clears the preset's `lockFileMaintenance.postUpgradeTasks`
commands for this repo. Mend cannot run post-upgrade commands — `allowedCommands`
is admin-only — so it publishes `renovate/artifacts: failure` on every lock
branch, which the gate then withholds on. Measured: the lane read `success` on
#3216 and `failure` on the first branch Renovate rebased after #3227 merged.
Clearing the commands removes a signal that can only ever be false here; the
bound itself is not lost, because the workflow above applies it instead.

### The npm lock is bounded differently: all-or-nothing on the whole file

The lane rewrites a fourth file, `packages/conformance/conformance/package-lock.json`
— dev-only devDependencies for the remediation programs, never bundled in the
published wheel. It gets its own driver, `renovate_npm_lock_bounded` (FND-380),
because **npm can express none of the per-package retention ceilings the uv bound
depends on, and has no forward-only mode at all.** Measured on npm 11.19.0 against
this project:

| command | versus `main` |
| --- | --- |
| `npm install --package-lock-only` (lock present) | byte-identical, no-op |
| `npm install --package-lock-only --before=X` (lock present) | **no-op** |
| `npm install --package-lock-only --before=X` (no lock) | 4 rolled **back** |
| `npm update --package-lock-only --before=X` (lock present) | the same 4 rolled **back** |

Two of those rows are traps. The second is the silent one: leaving the committed
lock in place and adding `--before` exits clean and changes nothing, so a bound
written that way reports success, bounds nothing, and is indistinguishable from a
lane with nothing to do. **The lock must be deleted before the resolve for the
date bound to reach the tree at all.** The third and fourth are the mass-rollback
failure FND-359 corrected before merge — the four were `fast-uri`, `hono`, `jose`
and `negotiator`, all adopted by `main` before this bound existed.

So the mechanism gates the file as a unit:

1. re-resolve from `package.json` alone with `--before` set to the window,
2. compare against the lock `main` ships, on the newest version each package
   *name* is pinned at — by name and not by install path, so a package moving
   between a hoisted and a nested position is not read as one entry vanishing and
   another appearing,
3. any name whose newest version went down, restore `main`'s lock **verbatim**;
   otherwise take the bounded resolve.

It cannot roll anything back, structurally rather than as a policy: the only two
things it can ever write are a resolve that regressed nothing and the exact bytes
it compared against. **A decline is exit 0, not an error.** Nothing in CI installs
this lock — there is no `npm ci` anywhere in the repo — so unlike the uv side
there is no required check a red could hold the branch on, and the state a decline
leaves is the safe one. Non-zero is reserved for the cases with no safe outcome at
all: npm unavailable, an unparseable lock, or a `package.json` that has moved away
from the baseline the fallback was resolved against.

The cost is the coupling, and it is real: one package `main` adopted inside the
window holds the entire npm lock until it ages out. It is not permanent — under
this bound `main` only ever takes versions that were already `--window` old, so
the pre-bound adoptions age past the window and the file starts moving again.

Do not reuse `packaging.Version` for the comparison, tempting as it is: PEP 440
reads `1.0.0-1` as `1.0.0.post1` and ranks it **above** `1.0.0`, so a rollback
from a release to one of its own prereleases would read as an upgrade, and it
rejects `7.0.0-next.5` outright — an ordinary npm channel — which the uv driver
treats as "cannot compare" and therefore as a regression, wedging the file into
declining every run.

### What `checks/dep-cooldown` still reports, and why that is not a bug here

The bound runs at `P3D` on both halves, matching the fleet's policy. The
`checks/dep-cooldown` check run enforces **7 days** — measured 2026-08-24 on
\#3365, which it failed on a 3-day-old release. So an adoption aged between 3 and 7
days is bounded correctly by policy and still reported by the check. Measured
exposure on one sampled refresh: 2 of the 6 versions the P3D bound adopted sat in
that band.

Closing that is the checker's threshold to change, not a second window here
(FND-761).
Note where the check comes from: the `atlan-security` App, **not** a workflow in
this repo. `dep-cooldown.yml` was removed in FND-373 because a public repo cannot
call the private reusable it wired up, so it had never produced a check run at
all — and the App offers no `lockfile-globs` equivalent to scope, only a `security`
label bypass. The App's check run is a separate thing and was never affected.

## Reusing scripts from a reusable workflow

A `uses:` reusable workflow does **not** bring its own repo's files into the
caller's checkout. To run an SDK script from a reusable that other repos call,
sparse-checkout it into a side path and invoke from there — the
`.sdk-scripts` pattern used by `release-version-bump.yaml` and
`generated-freshness.yaml`:

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

### Do not try to derive the ref from `github.job_workflow_sha`

`ref: main` above is deliberate and is the default answer. Every consumer pins
these reusables at `@main`, and several checkouts of this repo pin `main` for a
stronger reason — they fetch *content* that must reflect `main` whatever ref the
caller is on (`trivy-container.yaml`'s base allowlist,
`vuln-reconcile-on-release.yml`'s scan baseline,
`contract-toolkit-publish.yml`'s published content). Those carry their reasoning
inline; leave them as they are.

The tempting "improvement" is to derive the ref so the script always matches the
workflow that resolved it. `${{ github.job_workflow_sha }}` is documented in the
`github` context as exactly that — the commit of the reusable workflow file the
caller resolved. **It renders empty.** It is an OIDC token claim, not a usable
expression value.

The failure is silent and the wrong way round: `actions/checkout` treats an
empty `ref` as *not supplied* and takes the default branch, so the step goes
**green having fetched a different commit's script than the workflow the caller
pinned** — the wrong version of every code path, with nothing red to show for
it. FND-372 hit this; it only surfaced because the script did not exist on
`main` yet, so the *next* step died on a missing file.

Generalising: never let a `with:` value that must not be blank come from an
expression that can evaluate to empty. Prefer a shape where "unset" cannot be
expressed over a guard that checks for it after the fact — a declared input with
a default, or a literal.

**When verifying any change to a workflow like this, read the checkout step's
logged `ref:` rather than concluding from a green tick.** A step that fetched the
wrong ref still passes; it is the next step that fails, and only if what it
wanted happens to be absent from the fallback.

## `concurrency:` is not a lock, and not a queue

**Rule:** never use a `concurrency:` group to protect a shared external resource
(a tenant, a fixed environment, a singleton deployment). Use it only to
supersede or de-duplicate *runs of the same thing*.

Two independent limits make it unfit as a lock:

* **It is per-job.** The group is released when the job ends. Anything the job
  set up for later jobs to use — an install, a seeded database — is unprotected
  from the moment that job finishes.
* **It holds ONE pending run.** `cancel-in-progress: false` reads like a queue
  but is a waiting room with a single chair. A third arrival does not wait: it
  *evicts* the run that was waiting, which is reported as `cancelled` having
  never been given a runner, so there is no log blob at all (`gh api
  .../jobs/<id>/logs` → 404). A few-second job lifetime with no logs is the
  signature.

That combination caused FND-218: batching more than two PRs into the merge queue
self-inflicted roughly a two-in-three ejection rate, and because the connector
callback mirrored `cancelled` as `failure`, it read on the dispatching PR as
"your change broke the connector".

**Do not key a group on `github.ref` for anything a merge queue or a cross-repo
dispatch can reach.** On a cross-repo `workflow_dispatch`, `github.ref` is the
*callee's* ref — always `refs/heads/main` — never the dispatching commit. So a
ref-keyed group collapses every concurrent dispatch into one group, and the
one-pending-slot rule then evicts all but two. Key on `github.run_id` off the PR
path:

```yaml
concurrency:
  group: thing-${{ startsWith(github.ref, 'refs/pull/') && github.ref || github.run_id }}
  cancel-in-progress: ${{ startsWith(github.ref, 'refs/pull/') }}
```

On a real PR ref the shared group is the point (supersede the previous commit);
everywhere else the run must be independent.

**For a shared resource, take a lease instead.** The `(app, cloud)` tenant lease
in [`e2e-tenant-lease`](../../.github/actions/e2e-tenant-lease/) is the worked
example (FND-250). It needs no lock server:

* One ref per resource, with a **fixed** name:
  `refs/e2e-tenant-lease/<app>/<cloud>/holder`. `POST /git/refs` on a name that
  already exists returns 422, and that is an atomic test-and-set evaluated by
  GitHub — of N simultaneous callers exactly one gets 201. That 422 *is* the
  lock, not an error path bolted onto one.
* The ref points at a **blob** holding the holder's identity (run id, attempt,
  acquisition time). A ref can target a blob, which is what lets a single atomic
  creation both take the lease and record who took it. Waiters read it to tell a
  live holder from a dead one.
* Liveness is the holder's run status, not a heartbeat. A lease whose run is
  `completed` is reaped by whoever notices. This is the part `if: always()`
  cannot do: GitHub *cancels* queued jobs rather than running them, so a
  cancelled run's release job never starts.
* Release must **check ownership first**. A fixed name is a name you can delete
  someone else's lease with.

**Release cannot be made fully atomic, and that is an accepted trade-off, not an
oversight.** The check-then-delete above is inherently racy: the refs API offers
*no* conditional delete, so between the final ownership read and the `DELETE` a
replacement holder can acquire the ref, and the delete would take the new
holder's lease. The implementation re-reads the target immediately before the
delete, which narrows the window to one round-trip but cannot close it. The
deliberate decision is to **rely on the TTL as the load-bearing bound** rather
than chase a stronger primitive: the only interleaving that loses is a TTL
breaking a lease whose run is *still live*, and sizing the TTL above the longest
legitimate hold makes that window unreachable in practice. The alternative —
deleting only via a storage/API primitive that supports delete-if-current-target
— does not exist on `git/refs`, so proactive release stays best-effort and the
next acquirer's reaping of completed holders is the actual safety net.

**Do not build exclusion out of an ordering rule.** The first version of this
lease was an ordered queue: every run created a ticket ref named after itself,
and the lease belonged to whichever live ticket sorted lowest by
`(run_id, run_attempt)`. It failed on its first concurrent run — both contenders
acquired and both installed onto the same tenant. Two reasons, and both
generalise:

* `run_id` increases with run *creation*, which is **not** the order runs reach a
  given job. In the observed failure the lower-id run got there 15 seconds later.
* A total order gives FIFO *fairness*, not *exclusion*. A run that checks only
  the tickets ordered ahead of it never notices that a run behind it already
  holds the lease.

Ordering-based exclusion is only sound if every contender's ticket exists before
any contender decides, and nothing enforces that. Use an atomic primitive and
derive nothing from ids. The cost is fairness — acquisition becomes a scramble,
so bound starvation with the wait budget below rather than with a queue position.

**But when one run needs SEVERAL of these locks, it must take them in a fixed
order — and that is a different rule, not a contradiction of the one above.**
The lease job used to be a per-cloud matrix that acquired every cloud's lease in
parallel and held each for the whole run while blocking on the rest. That is
textbook hold-and-wait, and it deadlocked the first time two runs queued behind
one holder (FND-646): the holder released all three leases at once, the two
waiters raced for the freed set and **split** it — one took aws + azure, the
other gcp — and each then blocked on what the other held for the whole 90-minute
wait budget. Any time two or more runs are queued behind a holder, a parallel
matrix makes that split the *expected* outcome, not a rare interleaving.

The fix is resource ordering: one job takes every lock it needs, one at a time,
in an order that is a **pure function of the resource names** (`sorted()`). Two
runs can then never hold locks the other needs out of order, so the worst case
degrades from mutual blocking to plain serialisation. Note what is and is not
being ordered — the CAS still grants every lease, and the order governs only the
sequence in which *one* run takes several of them. That is why this does not
re-open the rejected-ordering trap above.

Consequences worth spelling out:

* The wait budget becomes a **total** across the set, not per resource, or N
  locks can outlast the job timeout by N times over and the runner's bare
  "cancelled after Nm" replaces the actionable error.
* Acquisition has to be **all-or-nothing**: a run that cannot get the whole set
  hands back what it took, immediately, rather than waiting for its cleanup job.
* Derive the order from the names only. Run id, arrival order, or the caller's
  list order all make two contenders disagree about which lock comes first,
  which is the entire guarantee.
* Release can stay parallel. It cannot block, so there is no wait to order.

Three further consequences to design for:

* A waiting run occupies a runner, so give the wait a budget and fail loudly past
  it, **naming the holder**. A contention outcome must never be phrased as a test
  failure.
* Ref writes need `contents: write`, so put acquire/release in their own small
  jobs and leave the jobs that execute test code read-only.
* **A fail-open is only a fail-open if every consumer downstream agrees.** This
  lease used to warn and return `disabled` when it could not write refs, on the
  reasoning that failing an ungrantable lease would turn a safety improvement
  into a fleet-wide red. It did not proceed: the install job verifies its own
  tenant's lease before installing, needs only `contents: read` to do it, finds
  nothing, and reds — so the run went red anyway, two jobs later, with an error
  saying "re-run this job" when re-running could not help (FND-702). The posture
  was not being chosen; it was being *reversed* by a consumer, at the cost of the
  one message that could have explained it. Before writing a fail-open, walk
  every consumer of the thing you are failing open on and check that it tolerates
  the degraded value; if any one of them does not, the fail-open is fiction, and
  the honest version is to fail where the cause is still in hand.

### Declare the permissions a reusable workflow needs, in the caller

A called workflow's `permissions` can only **equal or narrow** its caller's, so a
job in the reusable declaring `contents: write` is a ceiling, never a grant. A
caller with no block at all satisfies it purely from the repository's
`default_workflow_permissions` — which means tightening that setting, an ordinary
hardening step, silently strips the grant from every adopted repo at once, with
nothing in the resulting failure pointing at the cause.

So the canonical scaffolded `tests.yaml` declares the set explicitly. Two things
make that edit easy to get wrong:

* **A `permissions:` block is exhaustive, not additive.** Every scope it omits
  becomes `none`. A well-meaning `permissions: {contents: read}` on the caller is
  strictly *worse* than declaring nothing, because it clamps a job whose
  repository default would have carried it.
* **Derive the set from the reusable, in a test.** `test_tests_yaml_permissions`
  reads every `permissions:` block in `tests-reusable.yaml`, takes the strongest
  level each scope is used at, and asserts the scaffolded caller declares exactly
  that — so adding a scope to a job of the reusable fails there rather than
  silently arriving as `none` in every connector.
