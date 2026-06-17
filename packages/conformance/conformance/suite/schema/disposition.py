"""Disposition: the three-state outcome model for a conformance result.

A *disposition* is **derived** from native SARIF fields — it is never stored
directly in the report.  This keeps the schema clean (no competing bespoke
status enum) while giving every consumer a single, unambiguous answer.

Disposition table
-----------------

+-----------------+-----------+------------------+--------------------+-------------------+
| Disposition     | kind      | level (effective)| suppressions       | Gate effect       |
+=================+===========+==================+====================+===================+
| PASS            | "pass"    | any              | (n/a)              | none              |
+-----------------+-----------+------------------+--------------------+-------------------+
| FAILING         | "fail"    | "error"          | empty              | **blocks** CI     |
+-----------------+-----------+------------------+--------------------+-------------------+
| WARNING         | "fail"    | "warning"        | empty              | counted, logged   |
+-----------------+-----------+------------------+--------------------+-------------------+
| SUPPRESSED      | "fail"    | any              | non-empty          | own category      |
+-----------------+-----------+------------------+--------------------+-------------------+

Any other combination (``kind="open"``, ``kind="review"``, etc.) is left as
``None`` — callers should treat unknown dispositions as non-blocking but logged.

The ``EnforcementTier`` and ``RuleMechanism`` enums are defined here because
``extensions.py`` imports them; keeping them here avoids a circular import.
"""

from __future__ import annotations

from enum import Enum

# ---------------------------------------------------------------------------
# Supporting enums (also used by extensions.py and catalog.py)
# ---------------------------------------------------------------------------


class EnforcementTier(str, Enum):
    """Lifecycle tier for a rule (BLDX-1393).

    * ``WARN``  — rule is visible and counted but does **not** fail the gate.
      New rules land here so they don't turn the fleet red overnight.
    * ``BLOCK`` — rule failures fail the CI gate and block merges.

    There is intentionally no ``OFF`` value — a wrong rule is *deleted*, not
    parked.  Suppression (``result.suppressions``) handles the per-site
    exception case.
    """

    WARN = "warn"
    BLOCK = "block"

    def to_sarif_level(self) -> str:
        """Map to the SARIF ``level`` value used on rules and results."""
        return "error" if self == EnforcementTier.BLOCK else "warning"


class RuleMechanism(str, Enum):
    """How a rule is verified — determines the minimum re-check scope.

    * ``STATIC`` — AST / regex analysis; milliseconds, no execution needed.
      Safe to run in a pre-commit hook (``--static`` flag).
    * ``TEST``   — requires a built environment and test execution; slower,
      can flake.  Only run in CI and the full gate.
    """

    STATIC = "static"
    TEST = "test"


# ---------------------------------------------------------------------------
# Disposition
# ---------------------------------------------------------------------------


class Disposition(str, Enum):
    """The three-state outcome for a single conformance result.

    Derived by ``derive_disposition()`` from the SARIF ``kind``, ``level``,
    and ``suppressions`` fields — never stored directly.
    """

    PASS = "pass"
    """Unequivocal success — the rule ran cleanly against this location."""

    FAILING = "failing"
    """Unequivocal failure — blocks the CI gate."""

    WARNING = "warning"
    """Failed, but the rule is in the ``warn`` tier — counted, non-blocking.
    The fleet-wide count for ``WARNING`` results on a rule is the graduation
    signal (BLDX-1393): when it drops below threshold and stops growing, the
    rule can be promoted to ``BLOCK``.
    """

    SUPPRESSED = "suppressed"
    """Failed, but intentionally suppressed by an inline annotation in the
    monitored source code.  Reported as its own category — the fleet-wide
    *suppression rate* for a rule is the early-warning signal that the rule
    is wrong before it reaches ``BLOCK`` and stalls everyone.
    """


def derive_disposition(result: Result) -> Disposition | None:  # type: ignore[name-defined]  # noqa: F821
    """Derive the :class:`Disposition` for a single SARIF result.

    Parameters
    ----------
    result:
        A :class:`~suite.schema.sarif.Result` instance.

    Returns
    -------
    :class:`Disposition` or ``None``
        ``None`` is returned for SARIF result kinds other than ``"pass"`` and
        ``"fail"`` (e.g. ``"open"``, ``"review"``) — callers should treat
        these as non-blocking but log them for human review.

    Notes
    -----
    Suppression takes precedence over level: a ``BLOCK``-tier result with an
    ``inSource`` suppression is ``SUPPRESSED``, not ``FAILING``.  This ensures
    inline justified suppressions are always counted in their own category
    regardless of the rule's enforcement tier.
    """
    # Import here to avoid circular imports at module load time.
    from conformance.suite.schema.sarif import Result

    if not isinstance(result, Result):
        raise TypeError(f"Expected Result, got {type(result)!r}")

    if result.kind == "pass":
        return Disposition.PASS

    if result.kind != "fail":
        return None

    # Suppression takes precedence — checked before level.
    if result.suppressions:
        return Disposition.SUPPRESSED

    effective_level = result.level or "warning"
    if effective_level == "error":
        return Disposition.FAILING

    return Disposition.WARNING
