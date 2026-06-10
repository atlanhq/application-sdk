# ADR-0016: Supply-Chain Pinning Policy

## Status
**Accepted**

## Context

The SDK is both a library and a build platform: its reusable workflows, composite actions, base image, and Renovate preset are consumed by every first-party app repo. A compromise anywhere in that chain — a repointed action tag, a mutated registry tag, an unpinned runtime package, or a poisoned dependency release — propagates to the whole fleet.

The concrete gaps this policy closes:

- **Mutable action references.** Third-party GitHub Actions referenced by tag (`@v4`) can be silently repointed by the action maintainer. Several of our workflows run with `ORG_PAT_GITHUB` or publish credentials in scope, making a repointed tag a remote-code-execution vector in the publish path.
- **Mutable image tags.** `FROM cgr.dev/atlan.com/app-framework-golden:3.13` resolves to whatever the registry serves for that tag at build time. Two builds of the same commit can produce different images.
- **Loose runtime package pins.** `apk add dapr-daprd-1.17` installs whatever patch release the APK stream currently carries, so the daprd inside the image can drift from the `__dapr_version` pin the SDK and CI use everywhere else.
- **Secrets in build-args.** `--build-arg ACCESS_TOKEN_PWD=...` leaks the PAT into build logs, image history, and BuildKit cache metadata.
- **Unverified binary downloads.** The local-dev daprd download executed whatever bytes the network returned, with no integrity check.
- **Post-merge-only CVE scanning.** Renovate automerges grouped minor/patch dependency bumps after a green gate, but the container/Trivy gates run post-merge or on a schedule — a CVE published between the dependency-cooldown window and automerge could land on `main` unscanned.

## Decision

Adopt a single supply-chain pinning policy across the repo:

1. **Third-party GitHub Actions are pinned to full commit SHAs**, with the original tag preserved as a trailing comment (`uses: owner/action@<sha> # v4`). Internal same-repo `atlanhq/*` reusable references stay branch-pinned (that is the reusable-workflow distribution model).
2. **Base images are digest-pinned via Renovate** (`pinDigests: true` for the Dockerfile manager in `renovate-config/default.json`). Renovate resolves `tag@sha256:...` and keeps the digest current alongside tag updates, so humans never hand-maintain digests.
3. **Runtime packages are pinned to full semver.** The Dockerfile installs `dapr-daprd-1.17=~<patch>` where the patch version is an ARG kept in lockstep with `__dapr_version` in `application_sdk/version.py` by `.github/workflows/check-dapr-version.yaml`.
4. **Secrets never travel as build-args.** Build credentials are passed as BuildKit secret mounts (`secrets:` on `docker/build-push-action`, `RUN --mount=type=secret,id=...` in Dockerfiles), keeping them out of logs, layers, and cache metadata.
5. **Downloaded binaries are checksum-verified.** The embedded daprd download verifies the archive against the release's published SHA256 before extraction and refuses to run on mismatch.
6. **Dependency changes get a PR-time CVE scan.** Any PR touching `uv.lock` or `pyproject.toml` runs a Trivy filesystem scan (no image build) that fails on fixable CRITICAL/HIGH findings — closing the cooldown-to-automerge window.
7. **Cooldown + automerge stays.** Grouped minor/patch automerge behind a green gate (with the org-level dependency cooldown delaying fresh releases) remains the fleet upgrade mechanism; the PR-time scan makes it safe rather than replacing it.

## Options Considered

### Option 1: Pin everything, automate the churn (Chosen)

SHA-pin actions, digest-pin images, semver-pin runtime packages — and delegate the resulting update churn to Renovate (digests, action SHAs via the `github-actions` manager) and a scheduled drift-check workflow (daprd).

**Pros:**
- Builds are reproducible: the same commit always resolves the same actions, base image, and runtime packages
- Tag-repointing attacks (actions or registry tags) become inert
- Update fatigue is handled by bots, not humans; the tag comment keeps SHAs reviewable
- PR-time scan blocks bad releases before merge instead of detecting them after

**Cons:**
- More Renovate PRs (digest bumps ride along with tag updates, mitigating this)
- A stale pin can linger if the bots are down — drift is silent until the scheduled checks run
- SHA pins are unreadable without the tag comment; reviewers must check the comment matches the SHA

### Option 2: Tag pinning + trust upstream (Not Chosen)

Keep `@vN` action tags, `:3.13`-style image tags, and minor-stream packages; rely on registry/marketplace trust and post-merge scanning.

**Pros:**
- Minimal maintenance; updates arrive implicitly
- Human-readable references

**Cons:**
- Every reference is mutable: a compromised maintainer account repoints a tag and our publish workflows execute it with org credentials
- Non-reproducible builds make incident forensics ("what exactly did we ship?") unanswerable
- Post-merge scanning means a vulnerable dependency is already on `main` (and possibly published) when detected

### Option 3: Vendor everything (Not Chosen)

Fork third-party actions into the org, mirror base images into our registry, vendor binaries.

**Pros:**
- Strongest isolation from upstream compromise

**Cons:**
- Permanent maintenance burden for dozens of actions and images
- Forks rot; security fixes must be manually pulled
- Digest/SHA pinning achieves the same immutability guarantee at a fraction of the cost

## Consequences

**Positive:**
- A repointed upstream tag (action or image) can no longer change what we build or execute
- Secrets are absent from build logs, image history, and cache — a leaked build log no longer leaks the org PAT
- The daprd a developer runs locally is byte-verified against the official release
- Dependency CVEs are caught at PR time for lockfile changes, before automerge can land them
- One documented policy to point to in reviews: "pin it like ADR-0016 says"

**Negative:**
- Renovate digest/SHA-bump PRs add review traffic (grouped + automerged to compensate)
- New third-party action references must be SHA-pinned by hand at introduction time
- App Dockerfiles consuming `ACCESS_TOKEN_USR`/`ACCESS_TOKEN_PWD` as build-args must migrate to `RUN --mount=type=secret` (the snippet is documented in the reusable workflow headers)
- If Chainguard ships a daprd patch ahead of the upstream GitHub release (or vice versa), the lockstep pin can briefly block image builds until the drift PR merges

## Implementation

- Action SHA pins: all `.github/workflows/`, `.github/actions/`, `.github/workflow-templates/`
- Digest pinning: `renovate-config/default.json` (`pinDigests` rule, Dockerfile manager)
- Runtime pin + sync: `Dockerfile` (`DAPR_RUNTIME_VERSION` ARG), `.github/workflows/check-dapr-version.yaml`, `application_sdk/version.py`
- BuildKit secrets: `build-and-scan.yaml`, `build-image.yaml`, `harbor-release.yaml`, `build-apps-image.yaml`, `build-and-publish-app.yaml`, `secure-build-push-apps` action
- Checksum verification: `application_sdk/dev/_dapr.py`
- PR-time CVE scan: `deps-cve` job in `.github/workflows/pull_request.yaml`
