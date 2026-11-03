"""Retry-ladder rule definitions (P060).

The SDK used to carry about a dozen hand-rolled retry ladders — a loop around a
``try``/``except`` with a ``sleep`` between attempts — with three jitter
conventions between them and five with no jitter at all (FND-4284).  A fleet of
workers that all lose the same dependency at once then retries in lockstep, the
thundering herd jitter exists to break up.  They were consolidated onto one
helper, ``application_sdk.common.backoff``; this rule keeps new ladders from
growing back beside it.

Scope
-----
``P060`` is ``sdk``-scoped.  The helper it points at is new, so a consumer app
pinned to an older SDK could not act on the finding; widening to ``both`` (with
a locked-SDK gate, like P053) is the follow-up once the helper is in the fleet
floor.

Rule-id stability (non-migration policy)
----------------------------------------
Rule ids are a permanent public contract — each is exposed in SARIF ``help_uri``
and referenced by inline ``# conformance: ignore[Pxxx]`` suppressions across the
fleet.  An id therefore **never migrates and never changes**.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P060",
        scope=RuleScope.SDK,
        name="HandRolledRetryLadder",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="retry-policy",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.54.0",
        rationale=(
            "Every hand-rolled retry loop picks its own delay arithmetic and its "
            "own jitter, and the conventions drift: before FND-4284 the SDK had "
            "three jitter schemes across about a dozen ladders and five with no "
            "jitter at all. An un-jittered ladder makes every worker that lost "
            "the same dependency retry in lockstep, a fleet-wide retry storm "
            "against the dependency that is trying to recover. One helper, "
            "application_sdk.common.backoff, owns the arithmetic and the jitter "
            "so they cannot drift again."
        ),
        short_description=(
            "Hand-rolled retry ladder — a loop with try/except and a sleep, "
            "outside application_sdk.common.backoff"
        ),
        full_description=(
            "Flags a ``sleep`` call inside a ``for`` / ``while`` / ``async for``\n"
            "loop whose body also holds a ``try`` statement with at least one\n"
            "``except`` handler — the shape of a hand-rolled retry ladder.  Both\n"
            "may sit at any depth inside the loop (the sleep in the ``except``\n"
            "block is the commonest spelling), but not inside a nested function,\n"
            "lambda or class, which runs on its own schedule.  The finding is\n"
            "reported at the **sleep call's** line, once per sleep call, so the\n"
            "suppression comment sits next to the delay it justifies.\n"
            "\n"
            "Sleep calls matched: ``asyncio.sleep(...)``, ``time.sleep(...)``,\n"
            "``anyio.sleep(...)`` (also through an ``import ... as`` alias), and a\n"
            "bare ``sleep(...)`` imported from one of those modules.\n"
            "\n"
            "Not flagged:\n"
            "\n"
            "* ``application_sdk/common/backoff.py`` itself — it is the one loop\n"
            "  allowed to walk a ladder.\n"
            "* ``asyncio.sleep(0)`` — the yield-to-the-event-loop idiom, not a\n"
            "  delay.\n"
            "* a loop that sleeps but has no ``try``/``except`` — a plain poller or\n"
            "  periodic flush loop has nothing to retry.\n"
            "* ``workflow.sleep(...)`` — a durable Temporal timer, not an\n"
            "  in-process delay.\n"
            "\n"
            "Fix: express the ladder as a ``Backoff`` and hand the call to\n"
            "``application_sdk.common.backoff.retry_async(call, backoff=...,\n"
            "retry_if=...)``.  A loop with its own control flow — a supervisor that\n"
            "resets its streak, or one racing a shutdown event — keeps its loop and\n"
            "asks ``Backoff.delay(retry)`` for the number, so the arithmetic and the\n"
            "jitter still come from the one helper.\n"
            "\n"
            "Suppress a loop that is genuinely not a retry ladder (a poller whose\n"
            "``try`` only guards a log call, say) with a justification:\n"
            "``# conformance: ignore[P060] <reason>``.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/"
            "conformance/docs/rules/prescriptions.md#p060"
        ),
    ),
)
