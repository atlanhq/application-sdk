"""Prescription rule definitions (P-series).

Above-the-bar engineering prescriptions the SDK mandates that do not fall under
a narrower category series (error-handling E, logging L, CI C, security S,
backwards-compatibility B, tests T, automation A).  Optimisations and
recommendations *below* the prescription bar live in the O-series instead.

Rule-id stability (non-migration policy)
----------------------------------------
P-ids are a permanent public contract: each is exposed in SARIF ``help_uri`` and
referenced by inline ``# conformance: ignore[Pxxx]`` suppressions across the
fleet.  A P-id therefore **never migrates and never changes**, even if a future
domain series (S/B/T/A/…) later subsumes the same topic.  When a domain series
takes over an area, the P-rule is retired in place (kept documented, no longer
firing) and the new rule gets a fresh id — the original P-id is never reused or
reassigned.  The same policy applies to O-ids.
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
        id="P001",
        scope=RuleScope.BOTH,
        name="UnboundedContractFields",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="contract-payload-safety",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.3.0",
        rationale=(
            "Temporal enforces a hard 2MB payload limit on workflow/activity I/O (ADR-0008). "
            "Unbounded fields can silently grow past it in production, failing the workflow "
            "with a cryptic size error instead of a type error at import time. A justified "
            "inline suppression keeps every opt-out visible in review and auditable in SARIF."
        ),
        short_description="Input/Output contract declared with allow_unbounded_fields=True — opts out of payload safety",
        full_description=(
            "An ``Input``/``Output`` contract subclass declared with the\n"
            "``allow_unbounded_fields=True`` class keyword opts out of the SDK's\n"
            "payload-safety enforcement: arbitrary, untyped fields may cross task\n"
            "boundaries unchecked.  This is intended to be *extremely* exceptional —\n"
            "the sanctioned way to use it is an inline, justified suppression at the\n"
            "declaration site (``# conformance: ignore[P001] <reason>``), so the\n"
            "carve-out is reviewed and stays visible.\n"
            "\n"
            "Suppressed declarations are still emitted to the SARIF report (counted in\n"
            "their own category), so every opt-out is reported every single time.\n"
            "This rule is ``BLOCK`` (suppress-only): an unsuppressed declaration fails\n"
            "the conformance gate — the only sanctioned use is the justified inline\n"
            "suppression above — see BLDX-1428.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p001",
    ),
    RuleDefinition(
        id="P002",
        scope=RuleScope.BOTH,
        name="CategoryFieldOverride",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="category-immutability",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.3.0",
        rationale=(
            "FailureCategory is consumed as an immutable reporting metric by the Automation "
            "Engine, SLA dashboards, and on-call routing (ADR-0013). A redeclaration either "
            "duplicates the parent (drifts on rename) or substitutes a different value "
            "(splits one failure mode across two buckets), corrupting the reporting layer "
            "for every downstream consumer."
        ),
        short_description="AppError subclass redeclares the `category` ClassVar — drifts the canonical taxonomy",
        full_description=(
            "``FailureCategory`` is the closed, single-axis taxonomy the SDK owns —\n"
            "every value is the canonical answer to *what happened* and is consumed as\n"
            "an immutable reporting metric (dashboards, SLA gates, on-call routing).\n"
            "The 14 categorical leaves in ``application_sdk.errors.leaves`` (and\n"
            "``AppError`` itself) are the sole defining sites: each leaf binds exactly\n"
            "one ``FailureCategory`` to its ``category`` ``ClassVar``.\n"
            "\n"
            "Domain subclasses MUST inherit ``category`` from their categorical-leaf\n"
            "parent — never redeclare it.  A redeclaration is either a same-value\n"
            "duplication (clutter that drifts as soon as the parent is renamed) or a\n"
            "true override (silent taxonomy drift that splits a metric across\n"
            "lookalike values).  Both are blocked uniformly: domain subclasses\n"
            "specialise via ``code`` and evidence fields, not ``category``.\n"
            "\n"
            "Suppressed declarations are still emitted to the SARIF report (counted in\n"
            "their own category), so every opt-out is reported every single time.\n"
            "This rule is ``BLOCK`` (suppress-only): an unsuppressed redeclaration\n"
            "fails the conformance gate — the only sanctioned use is the justified\n"
            "inline suppression ``# conformance: ignore[P002] <reason>`` at the\n"
            "declaration site — see BLDX-1432.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p002",
    ),
    RuleDefinition(
        id="P003",
        scope=RuleScope.BOTH,
        name="ErrorCodePrefixMismatch",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="error-code-shape",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.3.0",
        rationale=(
            "Each categorical leaf owns a prefix that embeds its category into every error code "
            "(`AUTH_`, `INTERNAL_`, etc.). Without it, the code column is opaque — dashboards "
            "must join the category column for every query, and subclasses that inherit the bare "
            "leaf code collapse all their distinct failure modes into one undifferentiated bucket."
        ),
        short_description="AppError subclass code missing or doesn't start with the parent leaf's category prefix",
        full_description=(
            "Every concrete subclass of an ``application_sdk.errors`` leaf "
            "(``AuthError``, ``InternalError``, ``InvalidInputError``, etc.) must\n"
            "declare its own ``code: ClassVar[str]`` that starts with the leaf's\n"
            "category prefix and an underscore (``AUTH_``, ``INTERNAL_``,\n"
            "``INVALID_INPUT_``, etc.).  Without that prefix the code is opaque to\n"
            "dashboards and on-call routing — the category column has to be joined\n"
            "for every query.  Without an override, every site of the subclass\n"
            "collapses into the leaf's bare bucket and is impossible to triage.\n"
            "\n"
            "The check resolves inheritance transitively: an intermediate class with\n"
            "no ``code`` (a 'pass-through' subclass between a leaf and a concrete\n"
            "leaf) is also flagged so failures don't silently inherit the bare\n"
            "leaf's code.  Suppress with ``# conformance: ignore[P003] <reason>``\n"
            "at the declaration when an intermediate is genuinely abstract — see\n"
            "typed-error-prescription §4 and BLDX-1431.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p003",
    ),
    RuleDefinition(
        id="P013",
        scope=RuleScope.APP,
        name="UntypedEntrypointBoundary",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="typed-contract-boundary",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
        rationale=(
            "Every @entrypoint method (and the implicit run() override) is the "
            "public API boundary of the app — the payload that crosses it must be "
            "validated, versioned, and evolvable.  Using a primitive, a container, "
            "or a class that does not subclass Input/Output bypasses the SDK's "
            "payload-safety validation, config-hash computation, and backwards-"
            "compatibility tracking.  The runtime @entrypoint decorator already "
            "rejects these at import time, so no conforming running app is untyped "
            "today — this rule surfaces the violation earlier (PR/CI) and covers "
            "pre-decorator code paths."
        ),
        short_description=(
            "@entrypoint (or implicit run()) input/output is not an SDK Input/Output subclass"
        ),
        full_description=(
            "A method decorated with ``@entrypoint`` (or a concrete ``run()`` "
            "override on an ``App`` subclass, which is the implicit single-"
            "entrypoint form) must declare:\n"
            "\n"
            "* its non-``self`` parameter as a subclass of ``Input``\n"
            "  (``application_sdk.contracts``);\n"
            "* its return type as a subclass of ``Output``\n"
            "  (``application_sdk.contracts``).\n"
            "\n"
            "Violations include: missing annotation, a primitive / container type\n"
            "(``dict``, ``list``, ``str``, ``Any``, etc. — even subscripted/bounded\n"
            "forms like ``dict[str, str]``), or a class that exists in the scanned\n"
            "source tree but does not transitively subclass ``Input``/``Output``\n"
            "(e.g. a plain ``pydantic.BaseModel`` subclass or a dataclass).\n"
            "\n"
            "Suppressed declarations are still emitted to the SARIF report.\n"
            "This rule is ``BLOCK`` (suppress-only): an unsuppressed violation\n"
            "fails the conformance gate — suppress with\n"
            "``# conformance: ignore[P013] <reason>`` at the method definition.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p013",
    ),
    RuleDefinition(
        id="P014",
        scope=RuleScope.APP,
        name="UntypedTaskBoundary",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="typed-contract-boundary",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
        rationale=(
            "Every @task method is an internal activity boundary — the payload must "
            "be typed and bounded so the SDK can validate it at the activity layer "
            "and detect drift across deployments.  Using an untyped structure "
            "bypasses the SDK's payload-safety enforcement and makes the task's "
            "I/O invisible to dashboards, schema tooling, and the contract registry.  "
            "The runtime @task decorator already rejects these at import time, so "
            "no conforming running app is untyped today — this rule surfaces the "
            "violation earlier (PR/CI)."
        ),
        short_description="@task input/output is not an SDK Input/Output subclass",
        full_description=(
            "A method decorated with ``@task`` must declare:\n"
            "\n"
            "* its non-``self`` parameter as a subclass of ``Input``\n"
            "  (``application_sdk.contracts``);\n"
            "* its return type as a subclass of ``Output``\n"
            "  (``application_sdk.contracts``).\n"
            "\n"
            "Violations include: missing annotation, a primitive / container type\n"
            "(``dict``, ``list``, ``str``, ``Any``, etc. — even subscripted/bounded\n"
            "forms like ``dict[str, str]``), or a class that exists in the scanned\n"
            "source tree but does not transitively subclass ``Input``/``Output``\n"
            "(e.g. a plain ``pydantic.BaseModel`` subclass or a dataclass).\n"
            "\n"
            "Suppressed declarations are still emitted to the SARIF report.\n"
            "This rule is ``BLOCK`` (suppress-only): an unsuppressed violation\n"
            "fails the conformance gate — suppress with\n"
            "``# conformance: ignore[P014] <reason>`` at the method definition.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p014",
    ),
    RuleDefinition(
        id="P015",
        scope=RuleScope.APP,
        name="UnmodeledBoundedContractField",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="contract-modeling",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
        rationale=(
            "Bounded containers (Annotated[dict[str, str], MaxItems(50)]) pass "
            "payload-safety validation (P001) but are still stringly-typed: "
            "keys and values carry no schema, typos surface only at runtime, and "
            "the field is invisible to contract diffing and schema tooling.  "
            "The SDK's make-contract guidance explicitly prefers typed properties "
            "over arbitrary string keys ('avoid stringly-typed contracts where the "
            "user can typo a key and only discover it at runtime').  WARN (not "
            "BLOCK) because the bounded form is technically sanctioned; this is a "
            "modeling nudge toward a typed nested model."
        ),
        short_description=(
            "Input/Output contract field uses a container of primitives/Any — "
            "replace with a typed nested model"
        ),
        full_description=(
            "A field on an ``Input``/``Output`` contract whose annotation is a "
            "container of primitives or ``Any`` — ``dict[str, str]``,\n"
            "``list[str]``, ``set[int]``, or the bounded equivalents\n"
            "``Annotated[dict[str, str], MaxItems(N)]`` — is considered an\n"
            "unmodeled boundary.  Even though the bounded form satisfies the\n"
            "payload-safety gate (P001), the container has no schema: keys and\n"
            "values are opaque strings, typos are runtime-only failures, and the\n"
            "field is invisible to contract diffing and the SDK's\n"
            "``is_backwards_compatible`` checker.\n"
            "\n"
            "The SDK contract guidance (``make-contract`` skill, §6) prefers\n"
            "typed properties / a nested ``pydantic.BaseModel`` subclass over\n"
            "arbitrary string keys.\n"
            "\n"
            "**Exempt:** ``list[FooModel]``, ``dict[str, FooModel]`` — containers\n"
            "of a typed class are the canonical bounded pattern and are fine.\n"
            "\n"
            "This rule lands as ``WARN`` (not ``BLOCK``) because the bounded form\n"
            "is technically sanctioned — this is a modeling nudge, not a gate\n"
            "failure.  Suppress with\n"
            "``# conformance: ignore[P015] <reason>`` when a typed replacement\n"
            "is not feasible.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p015",
    ),
    RuleDefinition(
        id="P026",
        scope=RuleScope.APP,
        name="GetattrOnTypedContractField",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="typed-contract-boundary",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.9.0",
        rationale=(
            "P013/P014 buy a typed Input/Output boundary; reading a declared field "
            "via getattr(param, 'field', default) spends it. A renamed or removed "
            "contract field silently yields the default instead of raising "
            "AttributeError, so contract drift goes undetected at the call site and "
            "the type annotation stops being load-bearing."
        ),
        short_description=(
            "getattr() with a default on a typed entrypoint/task contract param — "
            "defeats the typed boundary"
        ),
        full_description=(
            "Inside an ``@entrypoint`` or ``@task`` method, a declared field of a\n"
            "typed ``Input``/``Output`` contract parameter is read via\n"
            '``getattr(param, "field", default)`` instead of attribute access.\n'
            "Only the three-argument form (a *default* present) is flagged: it\n"
            "silently substitutes the default when the field is renamed or removed,\n"
            "where ``param.field`` would raise ``AttributeError`` and surface the\n"
            "drift.  This defeats the typed boundary P013/P014 establish and hides\n"
            "the change from the contract ledger (B005/B006), which only sees schema\n"
            "edits, not reads.\n"
            "\n"
            "Fix: use attribute access (``param.field``).  Suppress with\n"
            "``# conformance: ignore[P026] <reason>`` only when a value genuinely may\n"
            "be absent and the contract models it as ``Optional`` with a real default.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p026",
    ),
    RuleDefinition(
        id="P027",
        scope=RuleScope.APP,
        name="AppStateAsCrossTaskChannel",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="state-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.9.0",
        rationale=(
            "app_state is an in-memory bag scoped to a single execution id. Using it "
            "to hand data between tasks silently no-ops across activity/worker "
            "boundaries. A get_app_state(KEY) whose KEY is never written by any "
            "set_app_state(KEY) is a dead side channel — the read always falls "
            "through to its default, so the intended hand-off never happens."
        ),
        short_description=(
            "get_app_state(KEY) with no matching set_app_state(KEY) writer anywhere — "
            "dead cross-task side channel"
        ),
        full_description=(
            "An ``App.get_app_state(KEY)`` read whose ``KEY`` is never written by a\n"
            "``set_app_state(KEY, <non-None value>)`` anywhere in the app (a writer\n"
            "that only stores ``None`` — a placeholder 'claim ownership' write whose\n"
            "real populating write never lands — does not count).  ``app_state`` is\n"
            "in-memory\n"
            "and keyed by execution id, so it cannot carry data across activity or\n"
            "worker boundaries; a read with no writer always returns the default and\n"
            "the optimisation it was meant to enable is dead code.\n"
            "\n"
            "Cross-file: keys are resolved through module-level string constants, so\n"
            "a key defined in one module and read in another is matched.  Keys that\n"
            "do not resolve to a string literal are ignored on both sides.\n"
            "\n"
            "Fix: pass cross-task data through the typed entrypoint/task contract.\n"
            "Suppress with ``# conformance: ignore[P027] <reason>`` only when the\n"
            "writer is genuinely external to the scanned source.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p027",
    ),
    RuleDefinition(
        id="P028",
        scope=RuleScope.APP,
        name="ManualQualifiedNameFString",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-modeling",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.9.0",
        rationale=(
            "qualifiedName is the identity primitive for every Atlan asset (dedup, "
            "lineage, linking). Building it with an f-string scatters the grammar "
            "(segments, order, separator, escaping) across every connector, so a "
            "single grammar change breaks each one independently and silently. The "
            "pyatlan asset .creator() factories own the grammar centrally."
        ),
        short_description=(
            "Asset qualifiedName composed by hand with an f-string instead of via "
            "pyatlan asset creators"
        ),
        full_description=(
            "An f-string composes a slash-delimited ``qualifiedName`` — it both\n"
            "interpolates a ``*qualified_name`` / ``*_qn`` value and contains a\n"
            '``/`` separator (e.g. ``f"{connection_qualified_name}/{schema}"``).\n'
            "qualifiedName is Atlan's asset identity; hand-building it duplicates the\n"
            "grammar across the fleet, and a grammar change (tenant scoping, escaping)\n"
            "then breaks every connector independently with no single source of truth.\n"
            "\n"
            "Fix: construct assets through the pyatlan asset ``.creator()`` factories,\n"
            "which compute qualifiedName from typed parent references.  WARN tier —\n"
            "suppress with ``# conformance: ignore[P028] <reason>`` where a raw\n"
            "qualifiedName string is genuinely required.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p028",
    ),
)
