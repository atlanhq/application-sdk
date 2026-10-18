"""The bump-PR skip every job in the gated reusables starts with (FND-3947).

`tests-reusable`, `checks-reusable`, `conformance-reusable` and
`build-and-scan` prefix each job's `if:` with ``BUMP_SKIP_TERM && (…)``.
test_bump_pr_ci_gates.py tests that term. Every other test of a job condition
reads the condition without it, through :func:`without_bump_skip`, so it keeps
testing its own job's logic.
"""

from __future__ import annotations

BUMP_SKIP_TERM = (
    "!(inputs.skip-on-bump-pr && github.event_name == 'pull_request' && "
    "startsWith(github.head_ref, 'bump-version'))"
)


def without_bump_skip(condition: object) -> str:
    """Return *condition* with the leading ``BUMP_SKIP_TERM && (…)`` removed.

    Fails if the term is missing, so a job that loses it is caught here too.
    A ``${{ … }}`` wrapper is kept.
    """
    source = str(condition).strip()
    wrapped = source.startswith("${{") and source.endswith("}}")
    if wrapped:
        source = source[3:-2].strip()
    prefix = BUMP_SKIP_TERM + " &&"
    assert source.startswith(prefix), f"missing the bump-PR skip: {source[:80]!r}"
    rest = source[len(prefix) :].strip()
    assert rest.startswith("(") and rest.endswith(")"), rest[:80]
    inner = rest[1:-1].strip()
    return "${{ " + inner + " }}" if wrapped else inner
