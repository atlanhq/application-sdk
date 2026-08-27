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
