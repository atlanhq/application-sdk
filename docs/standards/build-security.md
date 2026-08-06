# Build & Security Scanning

> **IMPORTANT**: Always run security scans after Dockerfile changes or dependency updates. CI will block HIGH/CRITICAL vulnerabilities.

## Quick Reference

- **Image base**: `cgr.dev/chainguard-private/python` -> golden images -> SDK -> apps
- **Dapr runtime**: baked into the `app-framework-golden` base image via Chainguard Custom Assembly (0 CVEs). The container's daprd version is owned by the Custom Assembly config — the Dockerfile no longer installs it. (The `__dapr_version` pin in `application_sdk/version.py` governs only the local-dev auto-download, a separate path.)
- **Dapr component YAMLs**: shipped inside the `atlan-application-sdk` wheel at `application_sdk/components/` (see `[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml`, mirroring this repo's own `components/` dir). Consumer apps get them for free from their existing `atlan-application-sdk` dependency — no network download needed, and they're always in sync with whatever SDK version is locked in that app's `uv.lock`.
- **Registries**: Harbor (`registry.atlan.com`) for production, GHCR for CI — see [Base image registries](#base-image-registries)

## Base image registries

`app-runtime-base` is published to two registries from a single `docker buildx --push`
in `.github/workflows/harbor-release.yaml`, so both carry an **identical manifest digest**
for a given tag:

| Registry | Ref | Audience |
|---|---|---|
| Harbor | `registry.atlan.com/public/app-runtime-base` | External/partner and tenant distribution. The reference for app Dockerfiles. |
| GHCR | `ghcr.io/atlanhq/app-runtime-base` | Atlan's own app CI only. |

The GHCR mirror is a cost measure, not a second source of truth. Harbor is S3-backed with
blob redirects on, so a base-image pull 307s to a presigned S3 URL and bills as
`DataTransfer-Out`; GHCR pulls from GitHub-hosted runners are free. Because both refs
resolve to the same digest, an app can be pointed at either without changing what it
builds on.

### If a release publish fails partway

The push is not transactional across registries — buildkit pushes each name in sequence.
A GHCR-side failure (PAT rotation, GHCR incident) can therefore fail the job *after* the
Harbor tags have landed, leaving the floating aliases (`:latest`, `:MAJOR`, `:MAJOR.MINOR`)
advanced on Harbor but not on GHCR.

**Recovery: re-run the failed workflow run** (Actions → the failed run → *Re-run jobs*).
Do not cut a new release. Every tag is re-pushed to both registries from one build, so the
re-run restores parity; the tags are mutable, so the operation is idempotent. Cutting a new
release instead would leave the skipped version permanently absent from GHCR.

## Consuming Dapr components in an app repo

App repos should **not** curl these files from `raw.githubusercontent.com` or the GitHub contents API pinned to a hardcoded SDK tag (that pattern hits GitHub's unauthenticated rate limit under CI concurrency and silently drifts from the app's actual `atlan-application-sdk` version). Instead, copy them out of the installed package, e.g. as the app's `download-components` poe task:

```toml
[tool.poe.tasks]
download-components.shell = """
python -c "
import application_sdk, pathlib, shutil
src = pathlib.Path(application_sdk.__file__).parent / 'components'
shutil.copytree(src, 'components', dirs_exist_ok=True)
"
"""
```

This requires `atlan-application-sdk` to already be installed into the venv before the task runs (true for both local dev and the Docker build, where `uv sync` happens before `poe download-components`).

This is enforced fleet-wide by the conformance suite's **D009 `RemoteDaprComponentFetch`** rule (BLOCK-tier, autofixable) — see `packages/conformance/conformance/docs/rules/dependency.md#d009`. Run the `remediate` skill/loop with `--series D` against an app repo to detect and fix this pattern automatically.

## Build & Scan Commands

```bash
# Build image locally
docker build -t application-sdk:local .

# Scan image
trivy image application-sdk:local
grype application-sdk:local

# Scan dependencies
trivy fs uv.lock
grype dir:.
```
