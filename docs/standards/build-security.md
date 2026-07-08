# Build & Security Scanning

> **IMPORTANT**: Always run security scans after Dockerfile changes or dependency updates. CI will block HIGH/CRITICAL vulnerabilities.

## Quick Reference

- **Image base**: `cgr.dev/chainguard-private/python` -> golden images -> SDK -> apps
- **Dapr runtime**: baked into the `app-framework-golden` base image via Chainguard Custom Assembly (0 CVEs). The container's daprd version is owned by the Custom Assembly config — the Dockerfile no longer installs it. (The `__dapr_version` pin in `application_sdk/version.py` governs only the local-dev auto-download, a separate path.)
- **Dapr component YAMLs**: shipped inside the `atlan-application-sdk` wheel at `application_sdk/components/` (see `[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml`, mirroring this repo's own `components/` dir). Consumer apps get them for free from their existing `atlan-application-sdk` dependency — no network download needed, and they're always in sync with whatever SDK version is locked in that app's `uv.lock`.
- **Registries**: Harbor (`registry.atlan.com`) for production, GHCR for CI

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
