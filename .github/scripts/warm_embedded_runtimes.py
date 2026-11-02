#!/usr/bin/env python3
"""Boot the SDK's embedded Temporal dev server, daprd and the Temporal
time-skipping test server once, then exit.

Run by ``warm-caches.yaml`` on every push to ``main``. Booting the first two
through the SDK's own helpers (``application_sdk.dev.embedded_runtime`` /
``embedded_dapr``) downloads their binaries into ``~/.cache/atlan-sdk`` exactly
as the integration suite would. The time-skipping test server is a separate
binary that integration tests boot themselves; it is downloaded into the same
dir (``_download_dir``) those tests use. So the ``embedded-runtimes`` cache
this job saves is the one a PR's integration job restores, and the PR no
longer pays for, or fails on (HTTP 429), the download itself. Booting (rather
than only downloading) also proves the cached binaries start.

Usage::

    uv run python .github/scripts/warm_embedded_runtimes.py
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def time_skipping_server() -> AsyncIterator[None]:
    """Boot and stop the time-skipping test server from the cached dir."""
    from temporalio.testing import WorkflowEnvironment  # noqa: PLC0415

    from application_sdk.dev._embedded import _download_dir  # noqa: PLC0415

    async with await WorkflowEnvironment.start_time_skipping(
        download_dest_dir=_download_dir()
    ):
        yield


async def warm() -> None:
    """Enter and leave every runtime whose binary the cache holds."""
    from application_sdk.dev import (  # noqa: PLC0415 — resolved at call time so the test can substitute it
        embedded_dapr,
        embedded_runtime,
    )

    async with embedded_runtime(), embedded_dapr(), time_skipping_server():
        pass


def main() -> int:
    asyncio.run(warm())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
