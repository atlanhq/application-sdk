# Build & Security Scanning

> **IMPORTANT**: Always run security scans after Dockerfile changes or dependency updates. CI will block HIGH/CRITICAL vulnerabilities.

## Quick Reference

- **Image base**: `cgr.dev/chainguard-private/python` -> golden images -> SDK -> apps
- **Dapr runtime**: baked into the `app-framework-golden` base image via Chainguard Custom Assembly (0 CVEs). The container's daprd version is owned by the Custom Assembly config — the Dockerfile no longer installs it. (The `__dapr_version` pin in `application_sdk/version.py` governs only the local-dev auto-download, a separate path.)
- **Registries**: Harbor (`registry.atlan.com`) for production, GHCR for CI

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
