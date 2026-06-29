"""Determinism / async-correctness rule definitions (P020–P024).

App and SDK code must respect the SDK's async and determinism expectations in the
execution path.  Temporal **workflow** code (an ``App`` subclass's ``run`` /
``@entrypoint`` / ``@signal`` / ``@query`` / ``@update`` methods) is replayed
deterministically and must not read wall-clock time, generate randomness, sleep on
the wall clock, or perform I/O; those belong in a ``@task`` **activity**.  Async
SDK methods must be awaited, not called like sync functions or bridged through a
nested event loop.  These bugs are invisible in normal runs and only surface as
non-deterministic replay failures or event-loop errors under production
orchestration.

These are P-series (prescription) rules backed by a **separate**
``suite.checks.determinism`` check registration.  Multiple check modules under one
series letter is the established pattern (the orchestration P004–P007 rules and the
prescriptions P001–P003 rules already coexist under ``P``), so this rides the
existing ``P`` CI matrix leg with no new plumbing.

Scope
-----
All five rules are ``both``-scoped: workflow-context code and async SDK usage exist
in the SDK itself and in every consumer app.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

_HELP_BASE = (
    "https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/"
    "conformance/docs/rules/prescriptions.md"
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P020",
        scope=RuleScope.BOTH,
        name="NonDeterministicPrimitiveInWorkflow",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="determinism",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "Temporal workflow code is re-executed on replay, so it must be "
            "deterministic: the same inputs must always produce the same sequence "
            "of commands. Reading the wall clock (datetime.now/time.time), "
            "generating a UUID (uuid.uuid4), sleeping on the wall clock "
            "(time.sleep/asyncio.sleep), or drawing randomness produces a different "
            "value on replay and corrupts the workflow history. The SDK exposes "
            "deterministic equivalents through its seam — self.now()/now, "
            "self.uuid()/uuid4, sleep — that record their result in history so "
            "replay is faithful."
        ),
        short_description=(
            "Non-deterministic time/uuid/sleep/random call in workflow-context code"
        ),
        full_description=(
            "Inside an ``App`` subclass's workflow-context method (``run``, an\n"
            "``@entrypoint`` method, or a ``@signal`` / ``@query`` / ``@update``\n"
            "handler) a call reads wall-clock time, generates a UUID, sleeps, or\n"
            "draws randomness.  Workflow code is replayed deterministically, so use\n"
            "the SDK seam instead: ``self.now()`` or ``from application_sdk.app\n"
            "import now``; ``self.uuid()`` or ``from application_sdk.app import\n"
            "uuid4``; ``from application_sdk.app import sleep``.  ``@task`` activity\n"
            "bodies are exempt — they run once and may do any of this.\n"
            "\n"
            "Randomness (``random`` / ``secrets`` / ``os.urandom``) is also flagged:\n"
            "it is a real determinism bug, but the SDK exposes no deterministic-random\n"
            "primitive, so the fix is to move it into a ``@task`` or raise a seam\n"
            "request with the SDK team rather than a mechanical swap.\n"
            "\n"
            "Matching is receiver-anchored: only a ``.now()`` whose receiver is\n"
            "``datetime`` is flagged, so the sanctioned ``self.now()`` is untouched.\n"
            "Land as ``WARN``; record an unavoidable exception with\n"
            "``# conformance: ignore[P020] <reason>``.\n"
        ),
        help_uri=f"{_HELP_BASE}#p020",
    ),
    RuleDefinition(
        id="P021",
        scope=RuleScope.BOTH,
        name="SideEffectIoInWorkflow",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="determinism",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "Workflow code is replayed, so any interaction with the outside world — "
            "opening a file, making a network call, reading the environment, "
            "spawning a thread or process — runs again on every replay and produces "
            "non-deterministic, un-recorded results that break replay correctness. "
            "Side effects belong in a @task activity, which runs exactly once and "
            "whose result is durably recorded in workflow history."
        ),
        short_description="File / network / env / process I/O in workflow-context code",
        full_description=(
            "Inside an ``App`` subclass's workflow-context method a call performs\n"
            "side-effecting I/O — ``open``, ``requests``/``httpx``/``urllib``,\n"
            "``socket``, ``subprocess``, ``threading``/``multiprocessing``,\n"
            "``os.getenv`` / ``os.environ[...]``.  Move it into a ``@task`` method:\n"
            "workflow code must be deterministic, and activities are where I/O and\n"
            "external state belong.\n"
            "\n"
            "The detected surface is a curated high-signal subset, not an exhaustive\n"
            "list of every I/O API.  Remediation is structural (extract a ``@task``),\n"
            "so findings route to residue rather than an autofix.  Land as ``WARN``;\n"
            "suppress a reviewed exception with ``# conformance: ignore[P021]\n"
            "<reason>``.\n"
        ),
        help_uri=f"{_HELP_BASE}#p021",
    ),
    RuleDefinition(
        id="P022",
        scope=RuleScope.BOTH,
        name="UnawaitedCoroutine",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="async-correctness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "SDK app methods (run, @task, @entrypoint, interaction handlers) are "
            "async and return a coroutine. Calling one like a sync function — "
            "'self.fetch(x)' as a bare statement instead of 'await self.fetch(x)' — "
            "constructs the coroutine and immediately discards it, so the work never "
            "runs and the bug is silent (no error, no result). This is the most "
            "common way apps misuse the SDK's async surface."
        ),
        short_description="A same-class async method is called without await (dropped coroutine)",
        full_description=(
            "A bare expression statement calls a same-class ``async def`` method via\n"
            "``self.<name>(...)`` without ``await`` and without wrapping it in\n"
            "``asyncio.create_task`` / ``asyncio.gather``.  The call returns a\n"
            "coroutine that is never scheduled, so the work silently does nothing.\n"
            "Add ``await`` (or schedule it explicitly if concurrency is intended).\n"
            "\n"
            "Scope is intentionally narrow — a bare ``self.<async-method>()``\n"
            "statement inside an ``async def`` — so the target is provably a\n"
            "coroutine and the finding is false-positive-free.  Land as ``WARN``;\n"
            "suppress with ``# conformance: ignore[P022] <reason>``.\n"
        ),
        help_uri=f"{_HELP_BASE}#p022",
    ),
    RuleDefinition(
        id="P023",
        scope=RuleScope.BOTH,
        name="BlockingCallInAsyncDef",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="async-correctness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "Inside an async function the event loop must never be blocked or "
            "re-entered. Calling asyncio.run()/loop.run_until_complete() from within "
            "a running loop raises or deadlocks; calling a synchronous blocking "
            "library (requests, time.sleep) stalls the loop and every other "
            "coroutine on it. The correct pattern is to await an async equivalent, "
            "or offload blocking work via App.run_in_thread() inside a @task — not "
            "to bridge async with a sync workaround."
        ),
        short_description="Event-loop re-entry bridge or blocking sync call inside an async def",
        full_description=(
            "Inside an ``async def``, code either re-enters the event loop\n"
            "(``asyncio.run(...)`` or ``*.run_until_complete(...)``, including\n"
            "``loop.run_until_complete`` / ``asyncio.get_event_loop()....``) or makes\n"
            "a blocking synchronous call (``requests.*``, ``urllib.request.*``,\n"
            "``time.sleep``).  Await the coroutine directly, or offload genuinely\n"
            "blocking work with ``App.run_in_thread()`` inside a ``@task``.\n"
            "\n"
            "Blocking sync I/O is reported only **outside** workflow context — inside\n"
            "workflow methods the same calls are owned by P020 (sleep) and P021\n"
            "(network), so they are not double-counted.  Remediation is a restructure,\n"
            "so findings route to residue.  Land as ``WARN``; suppress with\n"
            "``# conformance: ignore[P023] <reason>``.\n"
        ),
        help_uri=f"{_HELP_BASE}#p023",
    ),
    RuleDefinition(
        id="P024",
        scope=RuleScope.BOTH,
        name="SyncAtlanClientInApp",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="async-correctness",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.8.0",
        rationale=(
            "pyatlan (atlan-python) ships both a synchronous AtlanClient and an "
            "asynchronous AsyncAtlanClient. App code runs in an async execution path "
            "(Temporal activities, the FastAPI server), so the sync client blocks the "
            "event loop on every Atlan call and stalls every other coroutine on it. "
            "The async client must be used instead — ideally through the SDK seam "
            "(create_async_atlan_client / AtlanClientMixin.get_or_create_async_atlan_client), "
            "which returns a configured, observability-stamped AsyncAtlanClient. This "
            "complements P019 (use pyatlan, not raw HTTP) by requiring the async "
            "variant of that client."
        ),
        short_description="Synchronous pyatlan AtlanClient used instead of the async client",
        full_description=(
            "App code constructs or invokes pyatlan's synchronous ``AtlanClient`` (or\n"
            "the vendored ``pyatlan_v9`` equivalent) — its constructor or a factory\n"
            "like ``AtlanClient.from_token(...)``.  Because the app's execution path is\n"
            "async, the sync client blocks the event loop.  Use\n"
            "``AsyncAtlanClient`` (``pyatlan.client.aio``) — preferably via the SDK\n"
            "seam: ``await self.get_or_create_async_atlan_client(cred)`` on an\n"
            "``App`` that mixes in ``AtlanClientMixin``, or\n"
            "``create_async_atlan_client(cred)`` from ``application_sdk.credentials``.\n"
            "\n"
            "Matching is receiver-anchored: ``AsyncAtlanClient`` and the SDK seam\n"
            "helpers are not flagged, only the sync ``AtlanClient`` under a pyatlan\n"
            "root.  Closing it makes downstream calls ``await``-ed, so remediation is a\n"
            "restructure routed to residue.  Land as ``WARN``; suppress with\n"
            "``# conformance: ignore[P024] <reason>``.\n"
        ),
        help_uri=f"{_HELP_BASE}#p024",
    ),
)
