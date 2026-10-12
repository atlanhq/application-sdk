#!/usr/bin/env python3
"""Refuse to continue unless a minted App token belongs to the expected App.

``actions/create-github-app-token`` mints a token for whatever App the
``app-id`` and ``private-key`` belong to. A mis-set variable or secret would
hand a fleet-writing job another App's token, so the job checks the minted
``app-slug`` first and stops before it writes anything.

Usage:
    APP_SLUG=<minted slug> python3 assert_app_token_slug.py <expected slug>
"""

from __future__ import annotations

import os
import sys


def check(actual: str, expected: str) -> tuple[bool, str]:
    if actual and actual == expected:
        return True, ""
    return False, (
        f"::error::the App credentials minted a token for {actual or '<none>'!r}, "
        f"expected {expected!r} — refusing to write to the fleet."
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0]:
        print("usage: assert_app_token_slug.py <expected-slug>", file=sys.stderr)
        return 2
    ok, message = check(os.environ.get("APP_SLUG", ""), args[0])
    if not ok:
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
