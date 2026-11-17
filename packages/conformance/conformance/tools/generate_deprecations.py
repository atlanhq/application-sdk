"""Generate the deprecated-symbol manifest from SDK source.

Usage
-----
Regenerate the committed manifest (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-deprecations

Check whether the committed manifest is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-deprecations --check

Direct invocation:

    python -m conformance.tools.generate_deprecations [--sdk-root DIR] [--check]

Design
------
Scans ``<sdk-root>/application_sdk`` with the shared B-series extractor, records
every *marked* deprecated symbol, and either writes the canonical JSON manifest
or (with ``--check``) compares it to the committed file.  Output is deterministic
(sorted), so ``--check`` is a reliable staleness gate — the forcing function that
makes a new ``@deprecated`` fail CI until the manifest is regenerated in the same
PR (BLDX-1418).
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.deprecation._manifest import (
    MANIFEST_PATH,
    SDK_IMPORT_ROOT,
    build_manifest,
    serialize,
)
from conformance.tools._generator import Generated, GeneratorSpec, run_generator


def _build(sdk_root: Path | None) -> Generated:
    assert sdk_root is not None
    manifest = build_manifest(sdk_root)
    detail = f"{len(manifest.symbols)} symbols"
    return Generated(serialize(manifest), detail, detail)


SPEC = GeneratorSpec(
    doc=__doc__ or "",
    description="Generate the deprecated-symbol manifest from SDK source.",
    command="gen-deprecations",
    outfile=MANIFEST_PATH,
    noun="manifest",
    title="Manifest",
    build=_build,
    sdk_marker=(SDK_IMPORT_ROOT, "__init__.py"),
    sdk_marker_label="application_sdk/",
    # build_manifest scans the directory's .py files and needs no
    # __init__.py, so an explicit root only has to hold the directory.
    accepts_sdk_root=lambda root: (root / SDK_IMPORT_ROOT).is_dir(),
)


def main(argv: list[str] | None = None) -> None:
    run_generator(SPEC, argv)


if __name__ == "__main__":
    main()
