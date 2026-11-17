"""Generate the contract-toolkit baseline from the toolkit's PklProject.

Usage
-----
Regenerate the committed baseline (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-toolkit-baseline

Check whether the committed baseline is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-toolkit-baseline --check

Direct invocation:

    python -m conformance.tools.generate_toolkit_baseline [--sdk-root DIR] [--check]

Design
------
Reads ``contract-toolkit/src/PklProject`` (the toolkit's own package manifest) and
records the canonical package base URI + current published version into the
committed ``data/toolkit_baseline.json``.  Output is deterministic, so ``--check``
is the forcing function that fails CI when the toolkit is bumped but the baseline
is not regenerated in the same PR — keeping K007/K008 honest offline.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks._toolkit_baseline import (
    BASELINE_PATH,
    TOOLKIT_PKLPROJECT_RELPATH,
    build_baseline,
    serialize,
)
from conformance.tools._generator import Generated, GeneratorSpec, run_generator


def _build(sdk_root: Path | None) -> Generated:
    assert sdk_root is not None
    baseline = build_baseline(sdk_root)
    return Generated(
        serialize(baseline),
        check_detail=f"toolkit {baseline.latest_version} @ {baseline.canonical_base}",
        write_detail=f"toolkit {baseline.latest_version}",
    )


SPEC = GeneratorSpec(
    doc=__doc__ or "",
    description="Generate the contract-toolkit baseline from the toolkit PklProject.",
    command="gen-toolkit-baseline",
    outfile=BASELINE_PATH,
    noun="baseline",
    title="Baseline",
    build=_build,
    sdk_marker=TOOLKIT_PKLPROJECT_RELPATH,
    sdk_marker_label="contract-toolkit/src/PklProject",
)


def main(argv: list[str] | None = None) -> None:
    run_generator(SPEC, argv)


if __name__ == "__main__":
    main()
