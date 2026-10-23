#!/usr/bin/env python3
"""Boot the SDK's embedded Temporal dev server and daprd once, then exit.

Run by ``warm-caches.yaml`` on every push to ``main``. Booting both through the
SDK's own helpers (``application_sdk.dev.embedded_runtime`` /
``embedded_dapr``) downloads their binaries into ``~/.cache/atlan-sdk`` exactly
as the integration suite would, so the ``embedded-runtimes`` cache that job
saves is the one a PR's integration job restores — the PR no longer pays for
the download itself. Booting (rather than only downloading) also proves the
cached binaries start.

Usage::

    uv run python .github/scripts/warm_embedded_runtimes.py
"""

from __future__ import annotations

import asyncio


async def warm() -> None:
    """Enter and leave both embedded runtimes."""
    from application_sdk.dev import (  # noqa: PLC0415 — resolved at call time so the test can substitute it
        embedded_dapr,
        embedded_runtime,
    )

    async with embedded_runtime(), embedded_dapr():
        pass


def main() -> int:
    asyncio.run(warm())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
