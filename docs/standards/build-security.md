# Build & Security Scanning

> **IMPORTANT**: Always run security scans after Dockerfile changes or dependency updates. CI will block HIGH/CRITICAL vulnerabilities.

## Quick Reference

- **Image base**: `cgr.dev/chainguard-private/python` -> golden images -> SDK -> apps
- **Dapr runtime**: baked into the `app-framework-golden` base image via Chainguard Custom Assembly (0 CVEs). The container's daprd version is owned by the Custom Assembly config — the Dockerfile no longer installs it. (The `__dapr_version` pin in `application_sdk/version.py` governs only the local-dev auto-download, a separate path.)
- **Dapr component YAMLs**: shipped inside the `atlan-application-sdk` wheel at `application_sdk/components/` (see `[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml`, mirroring this repo's own `components/` dir). Consumer apps get them for free from their existing `atlan-application-sdk` dependency — no network download needed, and they're always in sync with whatever SDK version is locked in that app's `uv.lock`.
- **Registries**: Harbor (`registry.atlan.com`) for production, GHCR for CI — see [Base image registries](#base-image-registries)

## Base image registries

`app-runtime-base` is published to two registries by
`.github/workflows/harbor-release.yaml`, and both carry an **identical manifest digest**
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

### How a release is built

Three jobs, because the image is multi-arch and **each architecture is built on a runner
native to it** — `ubuntu-latest` for amd64, `ubuntu-24.04-arm` for arm64. A single job
with `--platform linux/amd64,linux/arm64` emulates the non-native half under QEMU at
5-10x native, and it does so without failing, so nothing reports the regression.
`test_base_image_native_builds.py` pins the pairing.

| Job | Does |
|---|---|
| `prepare` | Computes the tag ladder (`harbor_release_tags.py`). |
| `build` (×2, native) | Builds and Trivy-scans one architecture; pushes an arch-suffixed **staging** tag to GHCR only. Harbor's project is the public catalog and never sees a half-image, so the build jobs hold no Harbor credential. |
| `merge` | Joins the two staging images into an index and writes it under every ladder tag on **both** registries (`create_multiarch_manifest.py`). |

Digest parity holds across that split: an index's bytes are a function of its child
digests, so the same two children written to two registries produce the same index digest.
`resolve_base_redirect.py` fails closed on any skew.

Each architecture is now Trivy-scanned on its own runner. Previously the action scanned a
separate native single-platform build on an x64 runner, so the **arm64 half of a released
base image was never scanned at all**. The report remains advisory (feedback mode), not a
gate.

### If a release publish fails partway

The push is not transactional across registries — `merge` writes one registry's ladder
after the other. A GHCR-side failure (PAT rotation, GHCR incident) can therefore fail the
job *after* the Harbor tags have landed, leaving the floating aliases (`:latest`,
`:MAJOR`, `:MAJOR.MINOR`) advanced on Harbor but not on GHCR.

**Recovery: re-run the failed workflow run** (Actions → the failed run → *Re-run jobs*).
Do not cut a new release. Every tag is rewritten on both registries from the same two
staging images, so the re-run restores parity; the tags are mutable, so the operation is
idempotent. Cutting a new release instead would leave the skipped version permanently
absent from GHCR.

If only `merge` failed, re-running that job alone is enough — the staging images from the
build legs are still in GHCR under `:sha-<sha>-amd64` / `:sha-<sha>-arm64`.

App builds do not silently ride out that window. When an app opts into
`use_ghcr_base` (see below), `build-and-publish-app.yaml` resolves the base tag on
**both** registries before building and fails closed on skew, with this recovery in
the error annotation — so a stale GHCR alias surfaces as a failed app build rather
than a green build on the wrong base.

### Redirecting app CI to the GHCR mirror

App Dockerfiles keep `FROM registry.atlan.com/public/app-runtime-base:3` — that
reference is the public interface, and it stays put. Callers of
`build-and-publish-app.yaml` opt in with `use_ghcr_base: true`, and a BuildKit named
context rewrites *where the layers come from* without changing what is built.

Each app self-selects, on its own schedule — the input's default stays `false` until the
fleet has soaked. The opt-in is safe to keep in an app's `build-and-publish.yaml` even
though `bootstrap` fully manages that file: the value is read back off the file on every
re-run (or set with `atlan-application-sdk-conformance bootstrap --use-ghcr-base true`),
so a re-sync preserves it instead of reverting the app to Harbor, and conformance C002
does not report an opted-in app as drifted. To go back to Harbor, delete the line or pass
`--use-ghcr-base false`.

The opt-in runs `.github/scripts/resolve_base_redirect.py` first, which fails the
build rather than let the redirect fail quietly:

| Situation | Outcome |
|---|---|
| Dockerfile's base reference matches, digests agree | Redirect applied, **pinned to the immutable digest** |
| No `FROM` matches the supported reference | **Build fails** — the opt-in would be a silent no-op |
| Harbor and GHCR serve different digests for the tag | **Build fails** — see the recovery above |
| Base reference only resolves inside BuildKit (`ARG` with no default) | Warns, builds from Harbor |
| GHCR unreachable | Warns, builds from Harbor |
| Harbor unreachable | **Build fails** — parity cannot be verified without the redirect's source |

A registry that cannot be reached is *unknown*, not skew. GHCR-unreachable degrades to the
pre-redirect Harbor pull instead of blocking an app release. Harbor-unreachable is the one
exception: with the redirect's source down there is no baseline to verify the GHCR tag
against, so an unverified redirect is indistinguishable from a stale one and the build fails
closed. Re-run once Harbor recovers — unsetting `use_ghcr_base` does not route around a
Harbor outage, it just moves the failure from the preflight to the base-image pull.

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
