#!/usr/bin/env bash
#
# Re-pin the app-contract-toolkit pkl package in the caller repo's
# contract/PklProject to a specific PUBLISHED version, re-resolve the
# lock, and regenerate app/generated/ (manifest.json, atlan.yaml, the
# Python input types, ...). Invoked from action.yaml when
# `contract-toolkit-version` is supplied — i.e. on cross-repo dispatches
# from a contract-toolkit release.
#
# Why this exists: the full-DAG e2e seeds the AE workflow version from
# the committed app/generated/manifest.json AND the worker container
# serves /workflows/v1/manifest from app/generated/ baked into its image
# (Dockerfile `COPY app/ app/`). To genuinely exercise a new toolkit
# end-to-end, app/generated/ must be regenerated BEFORE the image build
# (so the worker serves the new manifest) and AGAIN after setup-deps'
# checkout (so the runner-side harness seeds AE from the same manifest).
# This script is idempotent, so running it in both places is safe.
#
# Unlike repin-application-sdk.sh (a git rev — any branch/SHA resolves),
# the toolkit is a pkl package resolved from the published package index
# at atlanhq.github.io. TOOLKIT_VERSION must therefore be a PUBLISHED
# version, not an arbitrary git ref.
#
# Reads:  $TOOLKIT_VERSION  (published app-contract-toolkit version, e.g. 0.15.0)
#         $PKL_VERSION      (optional; pkl toolchain to install, default 0.27.2)
# Writes: contract/PklProject, contract/PklProject.deps.json, app/generated/*
#         in CWD (the caller workspace).

set -euo pipefail

if [ -z "${TOOLKIT_VERSION:-}" ]; then
  echo "::error::TOOLKIT_VERSION env var not set"
  exit 1
fi

if [ ! -f contract/PklProject ]; then
  echo "::error::contract/PklProject not found in $(pwd)"
  exit 1
fi

if [ ! -f contract/app.pkl ]; then
  echo "::error::contract/app.pkl not found in $(pwd)"
  exit 1
fi

# Install pkl if absent. Mirrors renovate-pkl-sync.yaml's pin (0.27.2).
# Both call sites (pre-build, post-setup) may run on a runner without pkl;
# the check keeps this a no-op when it is already on PATH.
PKL_VERSION="${PKL_VERSION:-0.27.2}"
if ! command -v pkl >/dev/null 2>&1; then
  echo "Installing pkl ${PKL_VERSION}..."
  curl -fsSL -o /tmp/pkl \
    "https://github.com/apple/pkl/releases/download/${PKL_VERSION}/pkl-linux-amd64"
  chmod +x /tmp/pkl
  sudo mv /tmp/pkl /usr/local/bin/pkl
fi

python3 - <<PYEOF
import os
import pathlib
import re

version = os.environ["TOOLKIT_VERSION"]
path = pathlib.Path("contract/PklProject")
src = path.read_text()

# Rewrite the version suffix on the app-contract-toolkit package URI,
# leaving the package path untouched. Matches the published-package form:
#   package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@0.14.2
pattern = re.compile(
    r'(app-contract-toolkit@)[^"\s]+',
)
replaced, n = pattern.subn(rf'\g<1>{version}', src)

if n == 0:
    raise SystemExit(
        "No app-contract-toolkit@<version> pin found in contract/PklProject; "
        "expected exactly one."
    )
if n > 1:
    raise SystemExit(
        f"Multiple app-contract-toolkit pins matched ({n}); expected exactly one."
    )

path.write_text(replaced)
print(f"Re-pinned app-contract-toolkit to {version}")
PYEOF

# Re-resolve the lock for the new version, then regenerate the generated
# artifacts. The eval MUST run from the app root with `--project-dir contract/`
# and `-m .` (NOT `-m app/generated`): the toolkit's modules already encode
# their own output paths (`app/generated/manifest.json`, root `atlan.yaml`,
# root `app.yaml`), so `-m .` lands them correctly. Verified to reproduce a
# connector's committed app/generated/manifest.json byte-identically; the
# `-m app/generated` form double-nests to app/generated/app/generated/.
# (Canonical path is `atlan app contract generate`, but the atlan CLI is not
# installed in this runner — this is the pkl fallback the make-contract skill
# documents.)
pkl project resolve contract/
pkl eval --project-dir contract/ -m . contract/app.pkl
echo "Regenerated contract artifacts from contract/app.pkl with toolkit ${TOOLKIT_VERSION}"
