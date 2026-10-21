# App Release Flow

This document describes the standard three-stage release flow for Atlan first-party app repos. All three stages are opt-in — apps that prefer Nishant's auto-from-commit-id model can omit the release wrappers and rely solely on `build-and-publish.yaml` for push-to-main GHCR builds.

## Overview

```
feat/fix commit merged to main
  → release.yaml (Stage 1: version bump PR)
      → bump-version-main PR merged
          → tag-and-publish.yaml (Stage 2: tag + GH Release)
              → build-and-publish.yaml on 'release: published' (Stage 3: versioned GHCR image)
```

## Stage 1 — Version bump PR (`release-version-bump.yaml`)

**Trigger:** Any non-`bump-version` PR merged to `main`.

**What it does:**
- Reads the current version from `pyproject.toml`.
- Analyses conventional commits since the last non-rc tag (`feat` → minor bump, `fix` → patch, `BREAKING CHANGE` / `!:` → major).
- Updates `pyproject.toml` and regenerates `uv.lock`.
- Prepends a new section to `CHANGELOG.md` with categorised commits. Every conventional
  type is rendered except `ci` (FND-3748): the release notes are the human review gate for
  a semver release, so any change to what ships — `perf`, `refactor`, `chore(deps)` — must
  appear in them. Non-conventional subjects are listed under *Other Changes*.
  *Rollout:* app repos pass `--sections auto`, which renders the full notes only once the
  repo's own workflows carry the bump-PR CI gate (below) and otherwise keeps the old
  Features + Bug Fixes notes byte for byte. Rendering chores moves the bump PR on every
  Renovate merge, so each repo switches over in the same `bootstrap --resync` that gates
  its bump-PR CI — the extra pushes never run ahead of the gate. The SDK repo renders the
  full notes immediately; it gates its own bump PRs from the same merge.
- Opens a PR from `bump-version-main` into `main` with the `release` label — or, when
  that PR is already open, re-syncs its title, body and label to the version this run
  produced (`.github/scripts/upsert_release_pr.py`).

`bump-version-main` is a *fixed* branch that is force-pushed on every merge to `main`, so
while a bump PR sits unmerged its content keeps moving: a `feat:` merged after a patch bump
was computed turns the pending release into a minor one. The PR must therefore be **upserted,
not created-or-skipped**. The version the PR advertises always comes from the run that last
touched the branch, never from the run that happened to open it.

Note the tag itself is read from `pyproject.toml` in Stage 2 and never from the PR title, so
a stale title misleads reviewers rather than mis-tagging a release.

**The branch is only re-pushed when the release would change** (FND-3322,
`.github/scripts/release_bump_debounce.py`). Each force-push is a `synchronize` that re-runs
all PR CI on the bump PR, and most merges change nothing it would ship: Renovate commits are
forced to `chore`, which does not move the version past the first patch bump. Since FND-3748
chores do appear in the rendered notes of a repo whose bump-PR CI is gated, so there a Renovate
merge changes the CHANGELOG section and is pushed — affordable only because that CI is opt-in
(next section). An ungated app repo keeps the old notes, so Renovate merges still don't push. The push is
skipped when the open branch
already carries the computed version (in every version file) and the same CHANGELOG section,
date aside, **and** still merges cleanly onto the new tip. Anything the script cannot establish
resolves to a push. A skipped branch stays on an older base; that is safe because it only edits
version lines, the lock's own-package version and the top of the CHANGELOG, and the merge
combines it with the current target. The release gate must test that combination too, and a
`/gate-check` run is dispatched on the branch tip rather than the PR's merge ref, so
`/gate-check` refuses a bump branch that is behind its base (FND-3839): update the branch, or
let the next bump push it, first.
The PR upsert still runs on a skipped push. Bump runs are also serialised per repo and target
(`concurrency`, queued not cancelled), so a burst of merges coalesces into the newest run.

**CI on a bump PR is opt-in via a `/gate-check` comment** (FND-3748, FND-3839). Nothing heavy
runs on a `bump-version-*` PR until a reviewer comments `/gate-check`, which means "run this
lane's full release gate":

| Lane | `/gate-check` runs |
|---|---|
| App repo `bump-version-main` | every CI workflow, including tenant e2e |
| SDK `bump-version-main` | SDK Gate (SDK area), PR Checks including connector e2e / SDR / storage, CodeQL |
| SDK `bump-version-conformance` / `-contract-toolkit` | SDK Gate for that area only — never tenant e2e |

The opt-in and its verdict are the `gate-check` **commit status** on the head commit, so the
opt-in is one-shot by construction: a new bump commit has no status. Nothing has to clear it
before a push. (`.github/scripts/bump_pr_opt_in.py clear` still runs in the app-repo
`release-version-bump.yaml`, for repos whose shims predate FND-3839 and still read the label.)
Ordinary PRs request e2e with `/e2e`. See [ci.md](ci.md#test-legs-are-requested-by-pr-comment-never-by-label)
for the command mechanics.

*App repos* skip **every job inside the reusable** on a bump PR's own `pull_request` run. Each
bootstrap shim (`tests.yaml`, `checks.yml`, `conformance.yaml`, `vulnerability-scan.yml`) passes
`skip-on-bump-pr: true` to its reusable, and every job there starts with
`!(inputs.skip-on-bump-pr && github.event_name == 'pull_request' && startsWith(github.head_ref, 'bump-version'))`
(FND-3947). A skipped job gets no runner and is not billed, but it still files its
`<caller> / <job>` check run as `skipped`, and `skipped` passes a required check. So the PR's
required checks (`tests / Tests Gate`, `pre-commit / Pre-commit`, `suite / Conformance Gate`,
`scan / Build Image`, `scan / Security Gate`) resolve on every bump commit, and **Release Gate is
the only blocker**: it stays red until the head commit's `gate-check` verdict is `success`.

The shims used to skip their **caller** job instead (FND-3748). That filed only a bare `tests` /
`pre-commit` / … context, never the required `<caller> / <job>` name, and GitHub does not count a
`workflow_dispatch` run toward a PR's required checks. A bump PR therefore sat at "Expected"
even after a passing `/gate-check`, and could only merge by admin bypass.

`/gate-check` dispatches each of those workflows on the bump branch (`workflow_dispatch`, which
the skip term does not match), so the dispatch runs everything. Its checks land on the head commit
alongside the PR run's skipped ones; the dispatch result reaches the merge only through the
`gate-check` verdict and Release Gate. `vulnerability-scan.yml` builds its release candidate only
on that dispatch. None of the shims subscribes to `labeled`.

`skip-on-bump-pr` defaults to `false`, and only a caller whose Release Gate reads the
`gate-check` *status* may set it. A shim from before FND-3839 runs its reusable on a bump PR's
`labeled` event, and its Release Gate passes on the `gate-check` *label*; a skipped
`tests / Tests Gate` there would let an untested release merge. The SDK's own `sdk-gate.yaml`
calls `conformance-reusable.yaml` on its bump PRs and leaves the input off too (see below).

*The SDK repo* cannot use caller skips: SDK Gate and Connector Tests Gate are inline required
jobs, and a skipped inline job files `skipped`, which reads as a pass. So both gates run and
**fail fast** on an un-opted bump PR, and every job they compose skips. Only the Admin role
may comment `/gate-check` here (app repos accept Write, Maintain or Admin). It posts a
`gate-check` status on the head and re-runs SDK Gate, PR Checks and CodeQL there; each reads
the status live (`bump_pr_opt_in.py check --sha`), since a re-run replays the original event
payload.

**Release Gate passes a release PR only on a `success` `gate-check` status on its head**
(`.github/scripts/release_gate.py`, vendored by bootstrap). In an app repo `/gate-check` posts
it as `pending` with the list of workflows it dispatched; `gate_check_verdict.py`, run by
`test-commands.yaml` on each of those runs' `workflow_run` completion, sets it to `success` once
all of them passed (or `failure`, naming the ones that did not) and re-runs Release Gate, whose
re-run replaces its earlier red. `pending` reads as "still running", `failure` as "failed", and
no status as "comment `/gate-check`". A push moves the head to a commit with no status, so the
gate fails until `/gate-check` is asked again. A release PR is recognised by its `bump-version*`
branch as well as the `release` label, because the label is added after the PR opens and the
`opened` payload can lack it. Labels never pass the gate any more, and Release Gate no longer
subscribes to `labeled`/`unlabeled`.

**Two guards decide whether a run acts at all** (`.github/scripts/release_guard.py`, called
from `release.py` before any file is touched; each sets `skip=true`, which every mutating
step is gated on). Both read the target branch fresh from the remote rather than trusting
the checkout, and both fail open when the check itself cannot be made:

- *Did the PR land on the target branch?* GitHub delivers a stacked PR's merge into its
  **parent feature branch** as a `pull_request: closed` event on the stack's root, so
  `branches: [main]` matches and `actions/checkout` fetches the feature branch tip *as*
  `origin/main`. The workflow passes `github.event.pull_request.merge_commit_sha` through
  `PR_MERGE_COMMIT_SHA`, and the guard asks git whether that commit is an ancestor of the real
  branch tip. Without this, one such merge rebuilt `bump-version-main` on fifteen unmerged
  feature commits and the bump PR went CONFLICTING (application-sdk#3794).
- *Has this version already shipped?* The checkout can be a frozen merge ref that predates a
  release merged seconds earlier, so a sibling PR's run recomputes the version that just
  published. The guard compares the computed version with the one on the target branch and
  skips when the branch is already at or past it (application-sdk#3570).

**Caller wiring** (`.github/workflows/release.yaml`):
```yaml
on:
  pull_request:
    types: [closed]
    branches: [main]
jobs:
  bump:
    if: |
      github.event.pull_request.merged == true &&
      !startsWith(github.event.pull_request.head.ref, 'bump-version')
    uses: atlanhq/application-sdk/.github/workflows/release-version-bump.yaml@main
    secrets: inherit
```

## Stage 2 — Tag and GitHub Release (`tag-and-release.yaml`)

**Trigger:** A PR with the `release` label merged to `main` (i.e., the bump-version PR from Stage 1).

**What it does:**
- Reads the new version from `pyproject.toml`.
- Extracts the matching section from `CHANGELOG.md` as release notes.
- Creates and pushes an annotated git tag `v<VERSION>` (idempotent — skips if the tag already exists on the correct commit; errors if it points at a different commit).
- Creates a GitHub Release (pre-release flag set for versions containing a `-` suffix, e.g. `1.2.3-rc1`, `1.2.3-alpha.1`).

The GitHub Release `published` event fires Stage 3.

**Caller wiring** (`.github/workflows/tag-and-publish.yaml`):
```yaml
on:
  pull_request:
    types: [closed]
    branches: [main]
jobs:
  release:
    if: |
      github.event.pull_request.merged == true &&
      contains(github.event.pull_request.labels.*.name, 'release')
    permissions:
      contents: write
    uses: atlanhq/application-sdk/.github/workflows/tag-and-release.yaml@main
    secrets: inherit
```

## Bump PR — release candidate and vulnerability scan (FND-3328)

The bump PR is the one PR the vulnerability scan gates in a release-flow repo
(ordinary PRs and queue entries skip it; see `ci.md`). Its
`vulnerability-scan.yml` runs two jobs:

1. `candidate` — `build-and-publish-app.yaml` with `candidate: true` on the
   PR's merge commit (`ref: github.sha`). Same prepare → build → merge as a
   release, pushed as `:candidate-<tree>` (`<tree>` = git tree SHA of that
   commit, per-arch `:candidate-<tree>-amd64|arm64`). It bakes the release's
   identity: `app_version` is `v<pyproject version>`, the tag Stage 2 cuts. No
   Docker Hub copy, scan, deploy or publish.
2. `scan` — `build-and-scan.yaml` on `candidate-<tree>@<digest>`, blocking. On
   a pass the Security Gate copies that digest to `:scanned-<tree>`. It only
   marks the calling repository's own package: an `image` naming any other
   package is refused, since the org token it writes with could reach any.

If the candidate build fails, `scan` falls back to the scan's own single-arch
build, so the required checks still mean something; the release then rebuilds.

## Stage 3 — Versioned GHCR image (`build-and-publish-app.yaml`)

**Trigger:** `release: published` event in the app repo.

**What it does:**
- Looks up `:scanned-<tree>` for the tree of the tagged commit (and only when
  the release tag is `v<pyproject version>`). **Found:** skips the build and
  promotes that manifest, byte for byte, to every tag below; the shipped digest
  is the scanned digest. The release's scan is then report-only. **Not found**
  (base moved before the merge, scan failed, lookup error): falls through to
  the build below, and that release's scan **blocks** the publish.
- Builds the multi-arch (`linux/amd64` + `linux/arm64`) Docker image.
- Pushes to GHCR with the full version-tag ladder:
  - **Stable** (e.g. `1.2.3`): `:latest`, `:1.2.3`, `:1.2`, `:1`, `:sha-{SHA7}`
  - **Pre-release** (e.g. `1.2.3-rc1`): `:1.2.3-rc1`, `:sha-{SHA7}` — no `:latest` or aliases
- Publishes to the Atlan Global Marketplace (`publish=true`).
- Pushes to Docker Hub for SDR apps (`self_deployed_runtime: true` in `atlan.yaml`).

On every push to `main` (non-release), the same workflow fires with `publish=false`. What it does depends on the app (FND-3327):

- **SDR deploy-on-merge apps** (`self_deployed_runtime: true`): builds and pushes `:{branch}-{sha7}` + `:{branch}`, scans the image (blocking), copies it to Docker Hub, and dispatches the deploy. No version ladder, no marketplace publish.
- **Every other app**: only the `Build decision` job runs (it reads `atlan.yaml`); certify, leak-scan, build and scan all skip. No image is built — the release builds the one that ships.

A caller that passes `publish: true` on push (the legacy `github.event.inputs.publish != 'false'` wiring) is unaffected and builds on every push as before.

**Caller wiring** (`.github/workflows/build-and-publish.yaml`):
```yaml
on:
  push:
    branches: [main]
    paths: ['**', '!**.md', '!images/**']
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      publish:  { type: boolean, default: false }
      ref:      { type: string, required: false }
      channel:  { type: string, default: "all" }
      tenants:  { type: string, default: "" }
jobs:
  build-and-publish:
    uses: atlanhq/application-sdk/.github/workflows/build-and-publish-app.yaml@main
    with:
      ref:         ${{ inputs.ref || github.ref }}
      publish:     ${{ github.event_name == 'release' || inputs.publish == true }}
      channel:     ${{ inputs.channel || 'all' }}
      tenants:     ${{ inputs.tenants || '' }}
      release_tag: ${{ github.event_name == 'release' && github.event.release.tag_name || '' }}
    secrets: inherit
```

## Image tag reference

| Context | GHCR tags pushed |
|---|---|
| Push to `main` (SDR deploy-on-merge apps only) | `:{branch}-{sha7}` (immutable), `:{branch}` (mutable) |
| Push to `main` (every other app) | none — no image is built |
| Release (stable) | `:main-{sha7}`, `:main` + `:latest`, `:VERSION`, `:MAJOR.MINOR`, `:MAJOR`, `:sha-{SHA7}` |
| Release (pre-release, e.g. rc) | `:main-{sha7}`, `:main` + `:VERSION`, `:sha-{SHA7}` |
| Bump PR (release candidate) | `:candidate-{tree}` (+ `-amd64` / `-arm64`); `:scanned-{tree}` once its scan passes |

A release build still pushes `:main-{sha7}` and `:main` (the branch slug is forced to `main` for a release tag). So for a non-SDR app the mutable `:main` tag now tracks the **latest release**, not the latest merge, and a `main-{sha7}` tag exists only for commits a release was cut from. Pin a version tag (`:VERSION`, `:sha-{SHA7}`) rather than `:main` where the exact build matters.

## Image identity

Every image built by `build-and-publish-app.yaml` carries its own identity so a
running worker can say exactly which build it is, however it was deployed.

The build job writes `app/atlan_build.json` into the build context before the
image build, and the template Dockerfile's `COPY app/ app/` bakes it in — no
Dockerfile change per app:

```json
{
  "app_version": "0.3.0",
  "commit_sha": "<full git sha>",
  "build_id": "main-abc1234",
  "image": "ghcr.io/atlanhq/atlan-foo-app:0.3.0",
  "built_at": "2026-09-10T12:00:00+00:00"
}
```

A promoted release image (FND-3328) was built on the bump PR, so its
`commit_sha`, `build_id` and `image` name the PR's merge commit and
`candidate-<tree>`: a commit whose tree equals the released commit's, not the
released commit itself. `app_version` still matches the release exactly.

`app_version` is the exact string the publish job sends to Global Marketplace as
`version` (the release tag for semver apps, the 7-char SHA for CD apps), so a
worker's self-report matches `versions.version` byte for byte. The publish job
also sends `commit_sha`, which GM stores on the version — the only handle that
resolves a semver image to its release, since `:0.3.0` carries no SHA.

The SDK reads the file at startup (`application_sdk.constants.load_build_info`)
and reports `app_version` and `commit_sha` on `worker_start` and on every
`token_refresh`. The baked file wins over `ATLAN_APPLICATION_VERSION`: the env
var describes what a deployer thinks it deployed, the file describes the image.
Single-app SDR customers set no version env vars and only bump the tag when
they upgrade, so the file is the one identity that survives their upgrades.
Apps with a non-template Dockerfile that does not copy `app/` can point the SDK
at the file with `ATLAN_BUILD_INFO_PATH`.

### `build_id`, and its relationship to `ATLAN_BUILD_ID`

`build_id` is the immutable image tag (`{branch}-{sha7}`), and it is the same
identity FND-1684 already established for the e2e path as the `ATLAN_BUILD_ID`
image ENV — see [`connector-ci-e2e.md`](connector-ci-e2e.md). That ENV is stamped
by the `build-app-image` action, which builds only the e2e image and is never
called by `build-and-publish-app.yaml`, so a **released** image carried no build
identity at all and answered the build-identity route with `""`.

There is one reader for both carriers,
`application_sdk.app.build_identity.build_identity()`, which prefers the ENV:

| Built by | Carrier | Reported by |
|---|---|---|
| `build-app-image` action (e2e) | `ATLAN_BUILD_ID` ENV | `build_identity()`, unchanged |
| `build-and-publish-app.yaml` (release) | `build_id` in `app/atlan_build.json` | `build_identity()`, new |

The ENV wins because an e2e build derives that exact string and compares against
it. A second env var and a second reader would make "which build is this?" a
question with two answers that can disagree.

### What existing consumers see change

The baked file wins over the env var for **new images only** — an image built
before CI started baking it has no file and behaves exactly as it does today.
On a new image, every reader of `APPLICATION_VERSION` now sees the value CI
baked rather than the one the deployer stamped:

| Reader | Field | Effect |
|---|---|---|
| `worker_start` / `token_refresh` events | `app_version`, `commit_sha` | The point of the change. |
| OTel `target_info` gauge (`observability/utils.py`) | `app.version` | Now the GM `version` string by construction, rather than whatever the deployer stamped. |
| Preflight results store (`preflight_persist`) | `app_version` | Same. Its "as its catalog card carries it" contract holds more tightly, not less: `gm_version` **is** the string publish sends as `version`. |
| `App started` / `App completed` log messages (`app/base.py`) | `app=`, `commit=` | A run's own exported logs name the app release that produced them, with `sdk=` alongside. Carried in the message rather than as attributes because that is the only field the run-logs path preserves end to end. See [Monitoring → Build identity in the App lifecycle messages](../concepts/monitoring.md#build-identity-in-the-app-lifecycle-messages). |

The two can only disagree when a deployer stamps something other than the GM
version it deployed — which is the case this change exists to correct. A
deployment that needs the deployer's value to win should stop baking the file
(`ATLAN_BUILD_INFO_PATH` pointed at a path that does not exist), not reorder the
precedence: the file is the only source a single-app SDR customer has.

## Release train scheduler

The central job that releases each listed semver app at its slot (FND-4036,
design FND-3529). It does the two human steps of Stage 1: it comments
`/gate-check` on the bump PR and lets the merge queue merge it.

| | |
|---|---|
| Workflow | `.github/workflows/release-train.yml` |
| Script | `.github/scripts/release_train.py` (tests: `.github/scripts/tests/test_release_train.py`) |
| Config and schedule | `data/release_train.yaml` in the private `atlanhq/connector-pulse` repo — days, slots, caps, and `schedule`: the one list of apps on the train and when each releases. Fetched from connector-pulse `main` every run with the fleet App token. Reviewed like code. |
| Identity | The `atlan-app-fleet` App (`FLEET_APP_ID` / `FLEET_APP_PRIVATE_KEY`). The workflow asserts the minted slug. |

### The schedule list

`schedule` in connector-pulse `data/release_train.yaml` maps each `owner/repo`
to one to four releases a week, each `{day, slot}` with at most one a day:

```yaml
schedule:
  atlanhq/atlan-openapi-app: [{day: mon, slot: "12:00"}]
  atlanhq/atlan-mysql-app: [{day: mon, slot: "12:00"}, {day: thu, slot: "15:00"}]
```

- Being listed is what puts an app on the train. An unlisted app is never read
  or touched, and an empty list releases nothing: the job never falls back to
  discovering the fleet.
- To add, move or remove an app, open a PR on connector-pulse that edits the
  list. connector-pulse's test of the file applies the same rules as this job,
  so a bad day, an unquoted or unknown slot, a second release on one day or an
  unknown key fails that PR, not the 08:00 run.
- Every run re-reads the list, so a merged edit takes effect at the next run.
  An invalid or unreadable list fails the run before any write.
- The list lives in a private repo so that this public repo never names the
  apps on it. The Pulse Release Train tab reads the same file.

### Public logs

Run logs and job summaries of a public repository are public, so they carry
counts only: apps listed, apps per state, writes per kind, and errors per kind
(an error shows as its HTTP status). They never name a repo, and never carry a
reason, a PR title, comment text, a login or a raw API error. An invalid list
is reported as invalid without quoting it; connector-pulse's test names the
problem. Per-app detail goes to each bump PR's status comment, visible only to
people who can read the app repo, and to Pulse. `--verbose` prints the per-app
table and full errors, for local runs only; the workflow never passes it.

### Phases

All times are IST, a fixed UTC+05:30. Releases happen Monday to Thursday only;
the config rejects `fri`, `sat` and `sun`.

| Phase | Cron (UTC) | IST | Does |
|---|---|---|---|
| `sync` | `30 2 * * 1-5` | 08:00 Mon–Fri | Re-reads the list, checks eligibility. Sets the bump PR title suffix and status comment. Disarms auto-merge the job armed on an earlier day that did not merge. On a release day, posts a new comment that mentions the app's CODEOWNERS: `Releasing today at HH:MM IST. To skip: /release-skip <reason>`. |
| `slot` | `30 3,6,9,12,15 * * 1-4` | 09/12/15/18/21 Mon–Thu | For each app due by now, runs pre-flight, then comments `/gate-check` (the bare command, nothing else), sets the plain title and arms auto-merge. Also re-gates apps armed this week whose head moved. |
| `close` | `30 17 * * 4` | 23:00 Thu | Disarms everything still armed, expires this week's skips, and writes the weekly summary to the job summary. |
| `roll` | none (dispatch only) | any time | **Fleet-wide roll** for a critical fix. Releases every listed app, or the dispatch's `repos` subset, now: `/gate-check` and auto-merge, exactly as a slot does. |

The current slot comes from the wall clock, not the cron. A scheduled run that
starts late still works on the slot it falls in. Apps due in an earlier slot
that a missed run never reached are released by the next run.

**Fleet-wide roll.** For a fix that cannot wait for each app's slot (a CVE,
a revoked CA), dispatch the workflow with phase `roll`, a `reason` such as the
advisory id, and `dry_run: false` (live also needs `RELEASE_TRAIN_LIVE`). It
ignores each app's day and slot, the caps and pending `/release-skip`s, and
runs on any day. It does not bypass anything that decides whether a release is
safe: eligibility, pre-flight (a branch behind `main` is deferred to the next
slot), `/gate-check` and the merge queue all still apply. An app already
gate-requested today or in flight is not gated twice; only a moved head is
re-gated. The reason is recorded as a `rolled` event in each status comment and
kept off the public log. Dispatch inputs on this public repo are visible, so
the reason must never name a customer or tenant. Run it dry first: the summary
counts what it would release.

**Caps.** At most `per_slot_cap` apps start per slot. Overflow moves to the
next slot that day with room. At most `per_day_cap` apps start per day. An app
that does not fit waits for its next scheduled day.

**Pre-flight.** A due app is released only if it passes all of these:

- It is eligible (see below).
- Its slot is not skipped.
- Its bump PR exists and is ahead of `main`.
- Its bump branch is not behind `main`. This is read from the compare API's
  `behind_by`, never `mergeStateStatus`.
- The GM canary check passes. This is a stub returning `unknown` until FND-4044.

A branch that is behind is deferred to the next slot, because `/gate-check`
refuses it. The job dispatches the app's `release.yaml` first if that workflow
declares `workflow_dispatch`. The bootstrap template does not today, so the
branch waits for the next merge to `main` to refresh it. The title shows why.

**Auto-merge** is armed with `enablePullRequestAutoMerge`, pinned to the head
commit that passed pre-flight (`expectedHeadOid`). The merge method follows the
repository's settings: squash, then merge, then rebase. A merge-queue branch
merges through the queue. The job only ever disarms auto-merge that its own App
enabled, never a human's.

**Re-gate.** A push to an armed bump PR moves its head to a commit with no
`gate-check` verdict, so Release Gate holds it. The next slot comments
`/gate-check` again. This happens at most `max_regate_per_pr` times a week;
past that the PR is `Blocked`.

### Eligibility

An app rides the train only if all of these hold. Otherwise its title says
`Not on the train: <next action>`.

- `release_model` in `atlan.yaml` on `main` is `semver` (or its alias
  `versioned`). A missing value means `cd`.
- `.github/scripts/release_gate.py` on `main` reads the `gate-check` status.
  If it does not, run `bootstrap --resync`.
- The `main` ruleset requires both `Release Gate` and `tests / Tests Gate`.

Keep system and partner apps off the list; nothing else excludes them.

### What the bump PR shows

The title keeps `Bump version to X` and adds one suffix. The job edits the
title only when it changes. Each edit is an `edited` event, which
`commits.yaml` subscribes to; that workflow exempts bump PRs at job level, so
an edit bills nothing.

| Suffix | Meaning |
|---|---|
| `· Next release Mon 26 Oct 12:00 IST` | Planned. |
| `· Skipped, next release …` | The next slot is skipped. |
| `· Blocked: <reason>` | Pre-flight or the gate failed; the reason says which. |
| `· Not on the train: <next action>` | Ineligible. |
| *(none)* | `/gate-check` requested and auto-merge armed. |

### Skips

Skips are PR comments, never labels:

- `/release-skip <reason>` skips the app's next slot. The reason is required.
- `/release-resume` cancels a pending skip.

The job reads the PR's comments itself. It honours only commenters with the
Write, Maintain or Admin role, read from the collaborator-permission API in the
same way as `pr_test_command.py`. `author_association` is not enough, and bots
are always ignored. An honoured command gets a 👍. A command must start the
comment, so a comment that only mentions it, such as the release-day notice,
is not a command. A skip posted after Thursday's last slot applies to next
Monday. An app skipped two weeks running is flagged in the weekly summary.

### State: the release record

There is no store outside GitHub. Each bump PR carries one status comment,
found by `<!-- release-train:status -->` and edited in place. Only a comment
posted by the job's own App counts, so anyone else posting the marker changes
nothing. The comment has a human section (schedule, next slot, state, gate,
commands) and a hidden JSON record:

```
<!-- release-train:record v1
{"week": "2026-W44", "repo": "atlanhq/atlan-example-app",
 "schedule": [{"day": "mon", "slot": "12:00"}],
 "state": "armed", "reason": null,
 "events": [{"at": "2026-10-26T06:31:04Z", "slot": "2026-10-26T12:00+05:30",
             "action": "gate_requested", "sha": "<sha>", "by": "release-train"}],
 "regates": 0, "gm": {"stage": "unknown"},
 "seen_commands": [], "skip_weeks": []}
-->
```

`state` is one of `planned`, `gate_requested`, `armed`, `merged`, `blocked`,
`skipped` or `not_on_train`. The event actions are:

- `gate_requested`, `armed`, `disarmed`, `regated`, `titled`;
- `skipped`, `resumed`, `expired`;
- `deferred`, `notified`, `merged`.

Every event is keyed by `(week, slot, action, sha)`, plus the comment id for a
command. Re-running a phase does nothing new, and a missed run catches up. In
the JSON, `<`, `>` and `&` are escaped, so no value can close the HTML comment.
When a new week starts, the record keeps skips that target the new week or
later, plus the skip history.

### Safety

- **Dry run by default.** The script makes no write unless `--mode live`. A dry
  run counts the writes it would make, by kind, in the job summary.
- **Live** needs `vars.RELEASE_TRAIN_LIVE == 'true'` and either a scheduled
  run or a dispatch with `dry_run: false`.
- **Kill switch.** If `vars.RELEASE_TRAIN_ENABLED` is not `'true'`, the job
  runs report-only.
- The workflow computes the mode in a single expression
  (`test_workflow_mode_expression` pins it); the script does the branching.
- A dispatch's `repos` can narrow the schedule list but never widen it.
- Runs are serialised (`concurrency: release-train`, never cancelled).

**Dependencies.** `pr_test_command.py` must honour the fleet App's bare
`/gate-check` (FND-4037), and `upsert_release_pr.py` must keep the title suffix
across bump pushes (FND-4038). Until both ship, run the job dry.
