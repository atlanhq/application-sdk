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

## Stage 3 — Versioned GHCR image (`build-and-publish-app.yaml`)

**Trigger:** `release: published` event in the app repo.

**What it does:**
- Builds the multi-arch (`linux/amd64` + `linux/arm64`) Docker image.
- Pushes to GHCR with the full version-tag ladder:
  - **Stable** (e.g. `1.2.3`): `:latest`, `:1.2.3`, `:1.2`, `:1`, `:sha-{SHA7}`
  - **Pre-release** (e.g. `1.2.3-rc1`): `:1.2.3-rc1`, `:sha-{SHA7}` — no `:latest` or aliases
- Publishes to the Atlan Global Marketplace (`publish=true`).
- Pushes to Docker Hub for SDR apps (`self_deployed_runtime: true` in `atlan.yaml`).

On every push to `main` (non-release), the same workflow fires with `publish=false`, producing only `:{branch}-{sha7}` + `:{branch}` tags — no version ladder, no marketplace publish.

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
| Push to `main` | `:{branch}-{sha7}` (immutable), `:{branch}` (mutable) |
| Release (stable) | All of the above + `:latest`, `:VERSION`, `:MAJOR.MINOR`, `:MAJOR`, `:sha-{SHA7}` |
| Release (pre-release, e.g. rc) | All push-to-main tags + `:VERSION`, `:sha-{SHA7}` |

Apps opting out of explicit versioning can pin the mutable `:{branch}` tag (e.g. `:main`) in deployment manifests — it always tracks the latest build on that branch without requiring manual SHA updates.

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

The two can only disagree when a deployer stamps something other than the GM
version it deployed — which is the case this change exists to correct. A
deployment that needs the deployer's value to win should stop baking the file
(`ATLAN_BUILD_INFO_PATH` pointed at a path that does not exist), not reorder the
precedence: the file is the only source a single-app SDR customer has.
