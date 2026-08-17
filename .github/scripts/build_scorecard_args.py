"""Build the `scorecard` argument list for atlan-application-sdk-conformance.

The scorecard job feeds evidence from up to three tiers plus two descriptive
cross-CSP fields, and three of those arguments are *conditional* — which is
exactly the branching docs/standards/ci.md keeps out of YAML.  Callers do::

    mapfile -t sc_args < <(python .github/scripts/build_scorecard_args.py)
    uvx atlan-application-sdk-conformance scorecard "${sc_args[@]}"

Everything is read from the environment (set by a GitHub Actions ``env:``
block), so the workflow stays straight-line.

The three conditional rules, and why each is a rule rather than a default:

``--e2e-junit``
    Passed only when the e2e job actually RAN (``E2E_RESULT`` is neither empty
    nor ``skipped``).  A *failed* e2e still counts — its junit is exactly the
    evidence the scorecard should reflect.  Passing the glob unconditionally
    would be almost harmless (the CLI treats "matched nothing" as
    not-applicable), but "the job was skipped" is knowable here and saying so
    explicitly keeps the two ways of learning it from drifting.

``--cross-cloud-configured``
    Passed whenever the configured-clouds resolution SUCCEEDED
    (``CONFIGURED_KNOWN``), including when it resolved to *no* clouds — that is
    the "repo has no tenant matrix" state, which is a fact, not an absence.
    Omitted when the resolution failed or e2e is disabled for the repo, where
    nothing is known.

``--cross-cloud-observed``
    Passed only when e2e ran, on the same rule as ``--e2e-junit``.  Observed
    coverage is inherently sparse (the scorecard runs on push/merge_group; e2e
    runs on label/dispatch), which is precisely why ``configured`` is the field
    that carries rollout visibility.
"""

from __future__ import annotations

import argparse
import os
import sys

#: Job results that mean "this job did not produce evidence". A GitHub `needs.
#: <job>.result` is one of success/failure/cancelled/skipped, and is "" when the
#: job is not in `needs` at all.
_DID_NOT_RUN = frozenset({"", "skipped"})


def e2e_ran(e2e_result: str) -> bool:
    """Whether the e2e job produced evidence worth reading.

    ``failure`` counts: a red e2e run is evidence, and suppressing it would let
    an app's scorecard improve by having its e2e break.  ``cancelled`` also
    counts — a cancelled matrix can still have uploaded finished legs, and the
    CLI's glob no-ops when it did not.
    """
    return e2e_result.strip().lower() not in _DID_NOT_RUN


def build_args(
    *,
    repo: str,
    commit: str,
    out: str,
    unit_junit: str,
    unit_coverage: str,
    integration_junit: str,
    integration_coverage: str,
    e2e_junit_glob: str,
    e2e_result: str,
    configured_clouds: str,
    configured_known: bool,
    observed_clouds: str,
) -> list[str]:
    args = [
        "--unit-junit",
        unit_junit,
        "--unit-coverage",
        unit_coverage,
        # Always passed: a missing integration junit scores an empty tier, so an
        # app with no integration suite still counts it against the grade.
        "--integration-junit",
        integration_junit,
        "--integration-coverage",
        integration_coverage,
    ]

    ran = e2e_ran(e2e_result)
    if ran and e2e_junit_glob:
        args += ["--e2e-junit", e2e_junit_glob]
    # Both cross-CSP values may legitimately be EMPTY — "" is the degraded
    # single-tenant state, which is a fact rather than an absence — so they are
    # emitted as empty lines and the caller's `mapfile` must keep them.
    if configured_known:
        args += ["--cross-cloud-configured", configured_clouds]
    if ran:
        args += ["--cross-cloud-observed", observed_clouds]

    # Deliberately LAST, and deliberately a value that is never empty: the caller
    # reads this with `mapfile`, and a trailing empty line is exactly the element
    # a `raw=$(...)` capture (command substitution strips trailing newlines)
    # would silently drop — turning `--cross-cloud-observed ""` into a dangling
    # flag and an argparse error. Ending on a non-empty argument means the
    # encoding does not depend on that subtlety at all.
    return args + ["--repo", repo, "--commit", commit, "--out", out]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    env = os.environ.get
    args = build_args(
        repo=env("REPO", ""),
        commit=env("SHA", ""),
        out=env("OUT", "results/test-readiness.json"),
        unit_junit=env("UNIT_JUNIT", ""),
        unit_coverage=env("UNIT_COVERAGE", ""),
        integration_junit=env("INTEGRATION_JUNIT", ""),
        integration_coverage=env("INTEGRATION_COVERAGE", ""),
        e2e_junit_glob=env("E2E_JUNIT_GLOB", ""),
        e2e_result=env("E2E_RESULT", ""),
        configured_clouds=env("CONFIGURED_CLOUDS", ""),
        configured_known=env("CONFIGURED_KNOWN", "").lower() == "true",
        observed_clouds=env("OBSERVED_CLOUDS", ""),
    )
    # stdout IS this script's output mechanism — the workflow reads it with
    # `mapfile`, not a logger — so the ruff T201 exemption matches
    # build_conformance_args.py, which is consumed the same way.
    print("\n".join(args))  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
