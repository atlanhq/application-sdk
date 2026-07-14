"""Preflight-gate rule definitions (P-series, BLDX-1545).

The SDK injects a mandatory ``{app_name}:preflight`` activity as the first step
of every extraction workflow; it runs the app's ``Handler.preflight_check`` and
blocks the run on a ``NOT_READY`` verdict (application-sdk PRs #2361, #2626).
These four rules make the gate pattern statically enforceable across the fleet:
they surface a boot-time collision at review time (P032), the app-owned duplicate
that drifts from the gate (P033), untyped failure results that lose their wire
metadata (P034), and the silent metadata/contract drift that no runtime signal
can catch (P035).

The detector lives in ``suite.checks.preflight``; these rules reuse the ``P``
series so they run on the existing P leg of the fleet CI matrix with no workflow
change. Per the P-series stability policy (see ``prescriptions.py``) a P-id is a
permanent public contract and is never renumbered or reused.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

_HELP_BASE = (
    "https://github.com/atlanhq/application-sdk/blob/main/"
    "packages/conformance/conformance/docs/rules/prescriptions.md"
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P032",
        scope=RuleScope.APP,
        name="ReservedPreflightActivityName",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.15.0",
        rationale=(
            "The SDK injects a mandatory pre-extraction gate as the activity "
            "'{app_name}:preflight'. An app @task that also registers the 'preflight' "
            "activity name collides with it: the worker raises "
            "WorkerActivityNameCollisionError at boot and never starts. Catching the "
            "collision statically surfaces it in the PR instead of on the first deploy."
        ),
        short_description=(
            "An app @task registers the 'preflight' activity name reserved by the SDK gate"
        ),
        full_description=(
            "The SDK reserves the activity name ``{app_name}:preflight`` for the "
            "injected preflight gate and registers it unconditionally on the worker. "
            "An app ``@task`` whose effective activity name is ``preflight`` (an explicit "
            '``@task(name="preflight")`` or a bare ``@task`` on a method named '
            "``preflight``) collides with the reserved name; worker boot fails with "
            "``WorkerActivityNameCollisionError``.\n"
            "\n"
            "Remediation: rename the task, or fold its logic into the app's "
            "``Handler.preflight_check`` (which the gate already calls). A non-literal "
            "``@task(name=<expr>)`` is not statically resolvable and is not flagged."
        ),
        help_uri=f"{_HELP_BASE}#p032",
    ),
    RuleDefinition(
        id="P033",
        scope=RuleScope.APP,
        name="DuplicateInWorkflowPreflight",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.15.0",
        rationale=(
            "An app that defines Handler.preflight_check AND its own preflight-named "
            "@task has two preflight implementations that inevitably drift. The gate "
            "runs preflight_check, so the app-owned activity is redundant — the exact "
            "anti-pattern the SDK-native gate eliminates."
        ),
        short_description=(
            "App defines Handler.preflight_check and a separate preflight-named @task that will drift"
        ),
        full_description=(
            "When an app declares a ``Handler.preflight_check`` and also registers its "
            "own preflight-named ``@task`` (any ``@task`` whose effective name contains "
            "``preflight`` as a token but is not the reserved gate name — that exact "
            "case is P032), the two preflight paths diverge over time. The SDK gate "
            "invokes ``Handler.preflight_check``; the app-owned activity is dead weight "
            "that silently rots.\n"
            "\n"
            "Remediation: delete the app-owned preflight activity and keep the single "
            "``Handler.preflight_check`` implementation the gate calls."
        ),
        help_uri=f"{_HELP_BASE}#p033",
    ),
    RuleDefinition(
        id="P034",
        scope=RuleScope.APP,
        name="UntypedPreflightCheckFailure",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.15.0",
        rationale=(
            "A failed PreflightCheck constructed without a typed error= falls back to "
            "the generic PREFLIGHT_CHECK_FAILED code, so the Automation Engine and the "
            "UI lose the category/code/audience/suggested_action the typed form carries "
            "on the wire. This points at the exact lines to migrate to typed failures."
        ),
        short_description=(
            "PreflightCheck(passed=False) constructed without a typed error= — untyped failure"
        ),
        full_description=(
            "A ``PreflightCheck`` with an explicit ``passed=False`` and no typed "
            "``error=`` (absent, or the literal ``None``) is an untyped failure: the "
            "gate falls back to the generic ``PREFLIGHT_CHECK_FAILED`` code and only the "
            "deprecated free-text ``message`` reaches the caller. The typed form "
            "``error=AuthError(message=..., suggested_action=..., cause=exc)"
            ".to_failure_details()`` carries category / code / audience / retryable / "
            "suggested_action to the Automation Engine and the UI.\n"
            "\n"
            "Only an explicit ``passed=False`` literal is flagged; a failure expressed "
            "purely by the default ``passed`` and a non-literal ``passed`` are left "
            "alone to keep false positives near zero. A locally-defined, non-SDK class "
            "named ``PreflightCheck`` is not flagged."
        ),
        help_uri=f"{_HELP_BASE}#p034",
    ),
    RuleDefinition(
        id="P035",
        scope=RuleScope.APP,
        name="PreflightMetadataContractParity",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="preflight-gate",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.15.0",
        rationale=(
            "On the gate path metadata is rebuilt from the extraction input's "
            "model_dump, so a metadata key that is not a field on any entrypoint Input "
            "contract is silently absent — a defensive input.metadata.get(key, default) "
            "read then passes vacuously with the wrong config. No runtime signal can "
            "catch this class of silent drift; the static check is the only guard."
        ),
        short_description=(
            "A preflight_check metadata key is not declared on any entrypoint Input contract"
        ),
        full_description=(
            "The preflight gate does not forward the live UI form: it rebuilds "
            "``PreflightInput.metadata`` from the extraction input's ``model_dump()`` "
            "(``_config_from_snapshot``), so only fields declared on an entrypoint "
            "``Input`` contract survive. A key read inside ``preflight_check`` via "
            '``input.metadata.get("key", ...)`` or ``input.metadata["key"]`` that is '
            "absent from the union of every entrypoint Input contract's fields is "
            "silently missing on the gate path, so a defensive ``.get(key, default)`` "
            "read passes vacuously with the wrong configuration (e.g. database scoping "
            "silently dropped).\n"
            "\n"
            "Remediation: declare the key as a field on the extraction input contract "
            "(matching the UI form), or stop reading it in ``preflight_check``. Keys are "
            "compared to contract field names with underscore/hyphen normalization; field "
            "aliases are not treated as allowed because ``model_dump`` emits field names, "
            "not aliases. The rule does not fire when no entrypoint Input contract is "
            "resolvable or when a contract (or an in-repo ancestor) opts into extra keys "
            "via either ``model_config`` form: "
            '``ConfigDict(extra="allow")`` or ``{"extra": "allow"}``.'
        ),
        help_uri=f"{_HELP_BASE}#p035",
    ),
)
