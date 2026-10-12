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
- Prepends a new section to `CHANGELOG.md` with categorised commits.
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
forced to `chore`, which neither moves the version past the first patch bump nor appears in
the rendered notes (only Features and Bug Fixes do). The push is skipped when the open branch
already carries the computed version (in every version file) and the same CHANGELOG section,
date aside, **and** still merges cleanly onto the new tip. Anything the script cannot establish
resolves to a push. A skipped branch stays on an older base; that is safe because it only edits
version lines, the lock's own-package version and the top of the CHANGELOG, and the merge — and
the `e2e`-label test run, which tests the PR's merge ref — combine it with the current target.
The PR upsert still runs on a skipped push. Bump runs are also serialised per repo and target
(`concurrency`, queued not cancelled), so a burst of merges coalesces into the newest run.

Release Gate deliberately still runs on every `labeled`/`unlabeled` event. It cannot be skipped
for unrelated labels: a skipped job files a `skipped` check, a required check reads that as a
pass, and the newest run wins — so an unrelated label would clear a real "missing `e2e`" failure.
The job is a two-minute `ubuntu-slim` check.

Release Gate passes a release PR on the `e2e` label **or** on a successful `e2e` commit
status on the PR head (`.github/scripts/release_gate.py`, vendored by bootstrap). The status
is needed because the label is consumed: the Tests Gate removes it once the run finishes and
records the verdict as that status first (FND-3411). The removal uses `github.token`, so it
starts no new Release Gate run. A later run — an unrelated label, a human removing `e2e` —
then reads the status and stays green. A push moves the head to a commit with no status, so
the gate fails until the label is added again. A repo still on the pre-FND-3411 template keeps
its green verdict from the `labeled` run, but any later label event turns it red; re-run
`bootstrap --resync` to pick up the status check.

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
