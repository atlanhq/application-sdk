# Build & Security Scanning

> **IMPORTANT**: Always run security scans after Dockerfile changes or dependency updates. CI will block HIGH/CRITICAL vulnerabilities.

## Quick Reference

- **Image base**: `cgr.dev/chainguard-private/python` -> golden images -> SDK -> apps
- **Dapr runtime**: baked into the `app-framework-golden` base image via Chainguard Custom Assembly (0 CVEs). The container's daprd version is owned by the Custom Assembly config — the Dockerfile no longer installs it. (The `__dapr_version` pin in `application_sdk/version.py` governs only the local-dev auto-download, a separate path.)
- **Dapr component YAMLs**: shipped inside the `atlan-application-sdk` wheel at `application_sdk/components/` (see `[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml`, mirroring this repo's own `components/` dir). Consumer apps get them for free from their existing `atlan-application-sdk` dependency — no network download needed, and they're always in sync with whatever SDK version is locked in that app's `uv.lock`.
- **Registries**: GHCR (`ghcr.io/atlanhq`), served publicly as `registry.atlan.com` by the registry gateway — see [Base image registries](#base-image-registries)

## Base image registries

`app-runtime-base` is published to GHCR by `.github/workflows/harbor-release.yaml`
(the filename predates Harbor's retirement). It is reachable under two references
that resolve to **the same image**, because they are the same backend:

| Reference | Served by | Audience |
|---|---|---|
| `registry.atlan.com/public/app-runtime-base` | The registry gateway, fronting GHCR | External/partner and tenant distribution. The reference for app Dockerfiles. |
| `ghcr.io/atlanhq/app-runtime-base` | GHCR directly | Atlan's own app CI. |

`registry.atlan.com` used to be Harbor, and the release pushed both registries. Harbor
now sits behind the registry gateway and no longer answers at that host, so there is one
push and no cross-registry parity to maintain. The gateway rejects the old `HARBOR_*`
credentials, so nothing in this repo logs in to `registry.atlan.com`;
`test_base_image_native_builds.py` pins that.

### How a release is built

Three jobs, because the image is multi-arch and **each architecture is built on a runner
native to it** — `ubuntu-latest` for amd64, `ubuntu-24.04-arm` for arm64. A single job
with `--platform linux/amd64,linux/arm64` emulates the non-native half under QEMU at
5-10x native, and it does so without failing, so nothing reports the regression.
`test_base_image_native_builds.py` pins the pairing.

| Job | Does |
|---|---|
| `prepare` | Computes the tag ladder (`harbor_release_tags.py`). |
| `build` (×2, native) | Builds and Trivy-scans one architecture; pushes an arch-suffixed **staging** tag. |
| `merge` | Joins the two staging images into an index and writes it under every ladder tag (`create_multiarch_manifest.py`). |

Each architecture is Trivy-scanned on its own runner. Previously the action scanned a
separate native single-platform build on an x64 runner, so the **arm64 half of a released
base image was never scanned at all**. The report remains advisory (feedback mode), not a
gate.

### If a release publish fails partway

**Recovery: re-run the failed workflow run** (Actions → the failed run → *Re-run jobs*).
Do not cut a new release. Every tag is rewritten from the same two staging images, and
the tags are mutable, so the operation is idempotent. Cutting a new release instead would
leave the skipped version permanently absent.

If only `merge` failed, re-running that job alone is enough — the staging images from the
build legs are still in GHCR under `:sha-<sha>-amd64` / `:sha-<sha>-arm64`.

### Pulling the base from GHCR directly in app CI

App Dockerfiles keep `FROM registry.atlan.com/public/app-runtime-base:3` — that
reference is the public interface, and it stays put. (Conformance I001 accepts
`ghcr.io/atlanhq/app-runtime-base:3` as an equal spelling, so a Dockerfile that names
GHCR directly is not reverted; the redirect then has nothing to do and says so.)
`build-and-publish-app.yaml`'s `use_ghcr_base` input, **default `true`**, drives a
BuildKit named context that fetches the layers from GHCR directly instead of through the
gateway, without changing what is built. An app can opt out by passing
`use_ghcr_base: false`; builds then use the Dockerfile's reference as written.

The preflight (`resolve_base_redirect.py`) resolves only GHCR. It never fails a build
over the base: anything it cannot handle leaves the Dockerfile's reference in charge.

| Situation | Outcome |
|---|---|
| Dockerfile's base reference matches | Redirect applied, **pinned to the immutable GHCR digest** |
| Dockerfile already names `ghcr.io/atlanhq/app-runtime-base` | Redirect not needed — builds from GHCR directly, nothing emitted |
| No `FROM` matches the supported reference (pinned patch tag, digest-pinned base) | Warns, builds from the reference as written |
| Base reference only resolves inside BuildKit (`ARG` with no default) | Warns, builds from the reference as written |
| GHCR unreachable | Warns, builds from the reference as written (through the gateway) |

## CI test images (MinIO mirror)

The Storage Emulator Tests pull MinIO only from our own private GHCR package, `ghcr.io/atlanhq/ci-mirror/minio`, and never from a vendor registry. MinIO's community images disappeared from Docker Hub and then from quay.io in the same month, pinned digests included. The mirror holds a byte-for-byte copy of Chainguard's free `cgr.dev/chainguard/minio`, tagged with the MinIO release it contains.

- **Pin:** `MINIO_IMAGE` in `.github/workflows/sdk-tests-reusable.yaml`, as `tag@digest`. Connector repos that run MinIO should pin the same reference.
- **Refresh:** dispatch **Refresh MinIO CI mirror** (`.github/workflows/mirror-minio-image.yaml`). Leave `source_digest` empty to take Chainguard's current `:latest`, or pass a digest. Use `dry_run` to see what it would do. The run prints the `tag@digest` to pin. Paste it into `MINIO_IMAGE` and into the local-run docstrings in `tests/integration/storage/test_emulator_*.py`, then open a PR.
- **Tags never move.** Chainguard rebuilds the same MinIO release every day, so a refresh that finds the release already mirrored at a different digest is refused. That means no new MinIO release exists yet, and nothing needs doing.
- **Release-age cooldown:** Chainguard only serves `latest`, so a refreshed digest is always fresh. It is a test-only emulator, not a runtime dependency. Note the build date in the PR so a reviewer can accept it.
- **Access:** the package is private on purpose, so we are not publicly redistributing a third-party image. Jobs log in to `ghcr.io` with `GITHUB_TOKEN` and need `packages: read`. A reusable workflow's token is capped by its caller, so the calling job must grant it too. Each repo that pulls the image needs a grant under the package's *Manage Actions access* setting: **Write** for application-sdk, which also runs the refresh, and **Read** for any connector repo. A missing grant fails as `permission_denied`. Connector repos pull it from their `services-script`. The shared `integration` job in `tests-reusable.yaml` logs in to GHCR and grants `packages: read` for them.
- **Local runs:** the local-run commands in `tests/integration/storage/test_emulator_*.py` pull the same private image. Run `docker login ghcr.io` once, with an account that has Read on the package, using a personal access token with `read:packages`. The commands use the full `tag@digest` from `MINIO_IMAGE`, so update them in the same PR as the pin.

If Chainguard withdraws the image, CI keeps working from the mirror. Only refreshes stop.

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
