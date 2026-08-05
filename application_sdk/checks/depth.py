"""How deep a check run goes.

A caller that wants "is the credential still valid" should not have to pay for a
full permission sweep against the customer's source, and a caller that wants the
full picture should not have to know which check names a particular connector
happens to use. :class:`CheckDepth` is the shared vocabulary for that: handlers
tag each :class:`~application_sdk.handler.contracts.PreflightCheck` with the
depth it belongs to, and a caller asks for a maximum.

Two things depend on it:

* ``test_auth`` collapses into ``preflight_check`` — "test authentication" is a
  preflight run capped at :attr:`CheckDepth.AUTH`, not a parallel handler
  operation with its own resolution and error handling to keep in step.
* Scheduled drift detection can ask for a cheap run on a frequent cadence and a
  full one rarely, without the scheduler knowing anything connector-specific.

The levels are ordered by cost and by what they can prove. Each admits the ones
before it, so requesting ``PERMISSIONS`` also runs ``AUTH`` and ``REACHABILITY``
— a permission answer is meaningless if the credential is dead.
"""

from __future__ import annotations

from application_sdk.contracts.base import SerializableEnum


class CheckDepth(SerializableEnum):
    """How far a check run should go. Ordered cheapest-first.

    Compare with :func:`admits` rather than ``<``: ``SerializableEnum`` is a
    ``StrEnum``, so the comparison operators it inherits order *alphabetically*
    (``"full" < "permissions"``), which is not the order meant here.
    """

    AUTH = "auth"
    """The credential is present, well-formed, and accepted by the source.

    Cheapest useful check and the one that catches the most common drift: a
    rotated password, a revoked token, an expired certificate.
    """

    REACHABILITY = "reachability"
    """The source is resolvable and answers on the network path we will use.

    Distinct from :attr:`AUTH` because the remediation differs — a firewall rule
    or a DNS entry is not a credential problem, and reporting it as one sends the
    customer to the wrong screen.
    """

    PERMISSIONS = "permissions"
    """The credential can actually do what the run needs — read the catalog,
    list the schemas, call the endpoint.

    The drift class that silently produces empty extractions: auth still passes,
    the source is reachable, and a grant was removed underneath.
    """

    FULL = "full"
    """Everything the handler knows how to check, including whatever is specific
    to this source and the app's own artifact-write path.

    What the pre-extraction gate runs, because it is the last chance to catch a
    problem before a real run pays for it.
    """


# Rank outside the class: StrEnum treats class-level dicts as member values.
# Kept adjacent to CheckDepth so adding a level without ranking it fails loudly
# the first time it is compared, rather than silently sorting as unknown.
_DEPTH_RANK: dict[CheckDepth, int] = {
    CheckDepth.AUTH: 0,
    CheckDepth.REACHABILITY: 1,
    CheckDepth.PERMISSIONS: 2,
    CheckDepth.FULL: 3,
}


def rank(depth: CheckDepth) -> int:
    """Cost ordering for ``depth``. Raises ``KeyError`` on an unranked level."""
    return _DEPTH_RANK[depth]


def admits(requested: CheckDepth, check: CheckDepth) -> bool:
    """Whether a run capped at ``requested`` should include a ``check``-depth check.

    Inclusive downward: ``admits(PERMISSIONS, AUTH)`` is ``True``, because a
    permission verdict on a dead credential is not a permission verdict.
    """
    return rank(check) <= rank(requested)


def selected(requested: CheckDepth, depths: list[CheckDepth | None]) -> list[bool]:
    """Which of ``depths`` a run capped at ``requested`` should include.

    An untagged check (``None``) is always included. Depth tagging is additive on
    an already-shipped contract, so the overwhelming majority of connector checks
    carry no tag yet; excluding them would silently narrow every run made through
    a depth-aware caller. Erring toward running an untagged check costs time,
    while dropping it costs the verdict its meaning.
    """
    return [d is None or admits(requested, d) for d in depths]
