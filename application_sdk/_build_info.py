"""The build-identity file reader, with no dependencies beyond the stdlib.

:mod:`application_sdk.constants` exposes it as ``load_build_info``, as before.
It lives here so :mod:`application_sdk.common.build_identity` can read the file
without importing ``constants``, whose module-level imports the consolidated
API host's core install does not carry.
"""

from __future__ import annotations

import json
import os

__all__ = ["load_build_info"]


def load_build_info(path: str | None = None) -> dict[str, str]:
    """Read the build-identity file the reusable CI bakes into the image.

    ``build-and-publish-app.yaml`` writes ``app/atlan_build.json`` into the
    build context and the template Dockerfile's ``COPY app/ app/`` carries it
    into the image. Keys: ``app_version`` (the exact string Global Marketplace
    stores as ``versions.version``), ``commit_sha``, ``build_id`` (the immutable
    image tag, the same value ``ATLAN_BUILD_ID`` carries on the e2e path),
    ``image``, ``built_at``.

    Missing or malformed file → ``{}``; callers fall back to the env vars.
    ``ATLAN_BUILD_INFO_PATH`` overrides the lookup for non-template layouts.

    Public because :mod:`application_sdk.app.build_identity` reads the same file
    through it. A second parser there would be a second place for the key names
    to drift from what CI writes.
    """
    if path:
        candidates = [path]
    else:
        override = os.getenv("ATLAN_BUILD_INFO_PATH", "")
        generated_dir = os.environ.get("ATLAN_CONTRACT_GENERATED_DIR", "app/generated")
        candidates = (
            [override]
            if override
            else [
                "app/atlan_build.json",
                os.path.join(
                    os.path.dirname(generated_dir.rstrip("/")) or ".",
                    "atlan_build.json",
                ),
            ]
        )
    for candidate in candidates:
        try:
            with open(candidate, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return {
                str(k): str(v)
                for k, v in data.items()
                if isinstance(v, (str, int, float)) and not isinstance(v, bool)
            }
    return {}
