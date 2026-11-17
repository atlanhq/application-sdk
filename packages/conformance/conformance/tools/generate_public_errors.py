"""Generate the public-error allowlist from SDK source.

Usage
-----
Regenerate the committed allowlist (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-public-errors

Check whether the committed allowlist is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-public-errors --check

Design
------
Reads the ``__all__`` of ``application_sdk/errors/__init__.py`` at SDK-dev time
and writes the committed JSON that P043/P045 read at scan time.  The suite runs
inside consumer app repos, where no SDK source and no network are available, so
the data must ship baked into the wheel — the same mechanism as the B-series
deprecation manifest and the contract-toolkit baseline.

The allowlist decides which remediation a finding suggests, never whether it
fires.  A stale file therefore degrades a message rather than changing the gate,
but ``--check`` keeps it honest anyway (CONNECT-970).
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.error_seam._public_error_surface import (
    ALLOWLIST_PATH,
    SDK_ERRORS_INIT_RELPATH,
    build_allowlist,
    serialize,
)
from conformance.tools._generator import Generated, GeneratorSpec, run_generator


def _build(sdk_root: Path | None) -> Generated:
    assert sdk_root is not None
    names = build_allowlist(sdk_root)
    detail = f"{len(names)} public error classes"
    return Generated(serialize(names), detail, detail)


SPEC = GeneratorSpec(
    doc=__doc__ or "",
    description="Generate the public-error allowlist from application_sdk.errors.__all__.",
    command="gen-public-errors",
    outfile=ALLOWLIST_PATH,
    noun="allowlist",
    title="Allowlist",
    build=_build,
    sdk_marker=SDK_ERRORS_INIT_RELPATH,
    sdk_marker_label="application_sdk/errors/__init__.py",
)


def main(argv: list[str] | None = None) -> None:
    run_generator(SPEC, argv)


if __name__ == "__main__":
    main()
