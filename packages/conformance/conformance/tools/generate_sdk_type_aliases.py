"""Generate the SDK type-alias table that B005 reads at scan time.

Usage
-----
Regenerate the committed table (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-sdk-type-aliases

Check whether the committed table is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-sdk-type-aliases --check

Design
------
Reads every public module-level type alias under ``application_sdk/`` at
SDK-dev time and writes the committed JSON that B005 uses to expand an alias an
app imports from the SDK.  The suite runs inside consumer app repos, where no
SDK source is installed, so the data must ship baked into the wheel — the same
mechanism as the deprecation manifest, the public-error allowlist and the
contract-toolkit baseline.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.deprecation._sdk_type_aliases import (
    DATA_PATH,
    SDK_PACKAGE,
    build_sdk_type_aliases,
    serialize,
)
from conformance.tools._generator import Generated, GeneratorSpec, run_generator


def _build(sdk_root: Path | None) -> Generated:
    assert sdk_root is not None
    table = build_sdk_type_aliases(sdk_root)
    detail = f"{len(table)} aliases"
    return Generated(serialize(table), detail, detail)


SPEC = GeneratorSpec(
    doc=__doc__ or "",
    description="Generate the SDK type-alias table from application_sdk/.",
    command="gen-sdk-type-aliases",
    outfile=DATA_PATH,
    noun="table",
    title="Type-alias table",
    build=_build,
    sdk_marker=(SDK_PACKAGE, "__init__.py"),
    sdk_marker_label="application_sdk/",
)


def main(argv: list[str] | None = None) -> None:
    run_generator(SPEC, argv)


if __name__ == "__main__":
    main()
