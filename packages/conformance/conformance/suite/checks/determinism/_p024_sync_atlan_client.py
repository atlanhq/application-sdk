"""P024 — SyncAtlanClientInApp.

``pyatlan`` (atlan-python) is Atlan's client SDK and ships **both** a synchronous
``AtlanClient`` and an asynchronous ``AsyncAtlanClient`` (``pyatlan.client.aio``).
App code runs in an async execution path (Temporal activities, the FastAPI
server), so constructing the **sync** ``AtlanClient`` blocks the event loop on
every call.  The async client must be used instead — ideally through the SDK seam
(``application_sdk.credentials.create_async_atlan_client`` /
``AtlanClientMixin.get_or_create_async_atlan_client``), which returns a configured,
observability-stamped ``AsyncAtlanClient``.

This complements the client-seam rule P019: P019 says "don't hand-roll
raw HTTP — use pyatlan"; P024 says "when you use pyatlan, use the *async* client."
P019 deliberately does not flag client construction (reuse is good); P024 narrows
that to the sync variant only.

Detection is receiver-anchored via :func:`resolve_call_target`: a call whose
callable resolves to a ``pyatlan`` (or vendored ``pyatlan_v9``) ``AtlanClient`` —
the constructor or a factory like ``AtlanClient.from_token(...)`` — is flagged,
while ``AsyncAtlanClient`` and the SDK seam helpers are left untouched.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.orchestration._temporal_common import (
    collect_import_bindings,
)
from conformance.suite.schema.findings import Finding

from ._workflow_methods import resolve_call_target

RULE_ID = "P024"

# pyatlan is the dependency; pyatlan_v9 is the SDK's vendored alias of v9.
_PYATLAN_ROOTS = frozenset({"pyatlan", "pyatlan_v9"})
_SYNC_CLIENT = "AtlanClient"
_ASYNC_CLIENT = "AsyncAtlanClient"

_HINT = (
    "pyatlan ships an async client; the sync AtlanClient blocks the event loop in "
    "the app's async execution path. Use AsyncAtlanClient — ideally via the SDK "
    "seam 'await self.get_or_create_async_atlan_client(cred)' (AtlanClientMixin) "
    "or 'create_async_atlan_client(cred)' from application_sdk.credentials."
)


def _is_sync_atlan_client(target: str) -> bool:
    """True if *target* resolves to a pyatlan **sync** ``AtlanClient`` callable.

    Matches the constructor and its classmethod factories (``AtlanClient`` /
    ``AtlanClient.from_token`` / …) under a ``pyatlan`` root, while excluding
    ``AsyncAtlanClient`` (whose segment is a distinct name).
    """
    segs = target.split(".")
    return (
        segs[0] in _PYATLAN_ROOTS and _SYNC_CLIENT in segs and _ASYNC_CLIENT not in segs
    )


def check_p024(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit P024 findings for sync pyatlan ``AtlanClient`` construction/use."""
    bindings = collect_import_bindings(tree)
    # Only worth resolving calls if a pyatlan AtlanClient name is actually bound.
    if not any(
        _is_sync_atlan_client(origin) for origin in bindings.values()
    ) and not any(root in _PYATLAN_ROOTS for root in bindings.values()):
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = resolve_call_target(node.func, bindings)
        if target is None or not _is_sync_atlan_client(target):
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id=RULE_ID,
                node=node,
                message=f"Synchronous pyatlan '{_SYNC_CLIENT}' used in app code. {_HINT}",
                directives=directives,
            )
        )
    return findings
