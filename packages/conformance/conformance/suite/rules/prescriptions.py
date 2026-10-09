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

P032–P035 and P047 vacated to the F-series (F001–F005) in PR #3710: the fleet
held no machine-read suppression of them, so they were renamed rather than
retired in place.  Those five P-ids stay vacant.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import (
    RemediationKind,
    RemediationReference,
    RuleDefinition,
)
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P001",
        canonical_reference=(
            "atlan-mysql-app app/generated/_input.py — the generated "
            "`AppInputContract(ExtractionInput)` declares no allow_unbounded_fields at "
            "all: every field it adds is a concrete str or bool, and the include/exclude "
            "filters it inherits from ExtractionInput are already the bounded "
            "`FilterMap | str`."
        ),
        rule_interactions=(
            "B005 + ledger-guard bound the fix, but less tightly than they look, and "
            "reading them as a wall is how a fixable site gets suppressed. "
            "ledger-guard refuses a change to a RECORDED type; gen-contract-ledger "
            "never deletes an entry and never rewrites a recorded type — so narrowing "
            "the annotation IN SOURCE leaves the ledger entry untouched and the guard "
            "passes. A retype rejected when applied to the ledger file is not the same "
            "as one applied to the contract; run gen-contract-ledger then ledger-guard "
            "and read the result rather than inferring it. Removing a recorded field "
            "does fire B005 at BLOCK tier; retiring one is the sanctioned route, but "
            "read it precisely. B005 skips a sunset field only when it is ABSENT from "
            "source (live is None and status == 'sunset'), so retirement is: mark "
            "deprecated=True with json_schema_extra={'x-lifecycle': 'sunset'}, "
            "regenerate, THEN remove the field. A sunset field still declared with a "
            "changed type is still a retype and is judged as one. Narrowing in place is "
            "free only where _retype_is_compatible allows it: an inherited field, a "
            "widening, or replacing Any with a concrete type in the SAME OUTER SHAPE "
            "(which payload safety requires anyway, so it is not optional). "
            "Wrapping Any in MaxItems does not make it acceptable or clear P001: "
            "without the opt-out, P001's inverse finding still reports the "
            "Any-typed field and class creation raises PayloadSafetyError; with "
            "the opt-out, the class-level finding still fires. Any is refused "
            "unconditionally. MaxItems bounds a collection only when its "
            "element/value type is already payload-safe. Note also that an "
            "app-level OVERRIDE of a base-class field is often what introduces "
            "the Any — the SDK's own ExtractionInput already models its filters "
            "payload-safely — and dropping an override is not a retype of your "
            "contract at all."
        ),
        terminal_state=(
            "The opt-out removed, with every field it guarded bounded, not merely "
            "accepted by the payload-safety check. A kept opt-out with a justified "
            "`# conformance: ignore[P001] <reason>` is the owner's decision, reached only "
            "after the fixes in the description were tried and refused (each refusal "
            "named); it keeps the gate green but check_migration still fails, so it "
            "stays an open migration item. A field being recorded in the ledger does "
            "not license it, and a reason naming no attempted alternative is "
            "unremediated."
        ),
        scope=RuleScope.BOTH,
        name="UnboundedContractFields",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="contract-payload-safety",
        # Removing the opt-out changes what a contract accepts on the wire, so each site needs the
        # app owner's decision on the payload shape, often with a ledger retirement: a migration.
        autofixable=False,
        orthogonal_gate="tests",
        since="0.3.0",
        rationale=(
            "Temporal enforces a hard 2MB payload limit on workflow/activity I/O (ADR-0008). "
            "Unbounded fields can silently grow past it in production, failing the workflow "
            "with a cryptic size error instead of a type error at import time. Typing every "
            "field payload-safely and removing the opt-out restores that import-time check; "
            "an opt-out that genuinely cannot be removed carries a justified inline "
            "suppression, which keeps it visible in review and auditable in SARIF. "
            "Customer impact: payload size scales with the customer's data, so the app that "
            "passed every test fails only in the tenant with the largest source system — the "
            "customer's crawl dies mid-run with a serialization error nothing in their "
            "configuration explains."
        ),
        short_description="Input/Output contract declared with allow_unbounded_fields=True — opts out of payload safety",
        full_description=(
            "An ``Input``/``Output`` contract subclass that sets ``allow_unbounded_fields=True``\n"
            "(or a truthy ``_allow_unbounded_fields`` class attribute, set on the class or\n"
            "inherited from a class in the same file) opts out of the SDK's\n"
            "payload-safety check, so untyped or unbounded fields can cross task boundaries\n"
            "and fail at Temporal's 2MB limit in the tenant with the largest source.  Each\n"
            "finding names the fields this class declares that block removing it (the\n"
            "check refuses them), those the check accepts that still bound nothing, and any\n"
            "whose type this file cannot resolve (an import, or an alias defined elsewhere).\n"
            "\n"
            "**Removing the opt-out is the goal; the class's fields decide how.**  Work\n"
            "through these in order.  Each is a real fix only once the field is bounded,\n"
            "not merely accepted by the check.\n"
            "\n"
            "1. *Nothing to fix.*  When the finding says every declared field is already\n"
            "payload-safe (str, int, a nested model, ``FilterMap``, a ``MaxItems``-bounded\n"
            "collection), remove the keyword, or the ``_allow_unbounded_fields``\n"
            "attribute.  This is the most common case.  ``Input``\n"
            "drops undeclared keys with or without the opt-out, so extra arguments a\n"
            "caller sends never justify it.\n"
            "\n"
            "2. *Drop an override of an SDK base field.*  Re-declaring ``connection`` as\n"
            "``dict[str, Any]`` over ``ConnectionRef``, or filters as ``dict[str, Any]``\n"
            "over ``FilterMap | str``, is how most ``Any`` arrives.  Deleting the override\n"
            "inherits the safe type and keeps the wire shape.\n"
            "\n"
            "3. *Use the SDK's types in the same outer shape.*  ``ConnectionRef``;\n"
            "``CredentialRef`` or ``credential_guid`` (credentials cross tasks by\n"
            "reference, never inline); ``AgentCredentialSpec`` for ``agent_json``; a\n"
            "``BaseMetadataConfig`` subclass for wizard metadata; ``FilterMap`` for\n"
            "filter maps; a small ``BaseModel`` for a dict whose keys are known.  Absorb\n"
            "legacy wire shapes with a ``mode='before'`` validator.\n"
            "\n"
            "4. *Bound a collection of safe values* with ``Annotated[..., MaxItems(n)]``,\n"
            "``n`` taken from the field's real cardinality.  Every nested ``dict`` or\n"
            "``list`` needs its own bound.\n"
            "\n"
            "5. *Data that grows with the source system* (file lists, event batches,\n"
            "per-object results) moves by reference: write it to the object store and\n"
            "pass a ``FileReference``.  This is the fix that holds at 2MB.\n"
            "\n"
            "6. *Retire a field nothing populates*: mark it ``deprecated=True`` with\n"
            "``x-lifecycle: sunset``, regenerate the ledger, then remove it from source.\n"
            "\n"
            "**Not fixes.**  ``object``, ``JsonValue``, a bare ``dict`` or ``list``, a\n"
            "recursive alias, or a nested ``BaseModel(extra='allow')`` pass the check while\n"
            "bounding nothing; reach for them only as an owner's recorded decision.\n"
            "``MaxItems`` never makes ``Any`` safe.  Removing the opt-out while an ``Any``\n"
            "field remains raises ``PayloadSafetyError`` at class definition, so the app\n"
            "stops importing: import the module after the change, because a base class or\n"
            "mixin the detector cannot see may carry the ``Any``.\n"
            "\n"
            "**Generated contracts** (``app/generated/``) are fixed at their source: the\n"
            "``contract/*.pkl`` widget, or the toolkit when it emits ``dict[str, Any]``\n"
            "for a widget (``APITree`` and similar).  Never hand-edit the generated file,\n"
            "and never let a post-generate hook re-insert the opt-out.\n"
            "\n"
            "**Narrowing changes what the app accepts.**  Before narrowing, list the shapes\n"
            "the field really carries (its normaliser, its producers, the next task's\n"
            "contract) and confirm each still validates.  Only ``@entrypoint`` fields are\n"
            "in the contract ledger, so task contracts can be narrowed freely; for an\n"
            "entrypoint field run ``gen-contract-ledger`` then ``ledger-guard`` and read\n"
            "the result rather than inferring it.\n"
            "\n"
            "**Keeping the opt-out** is the owner's decision, reached only after the steps\n"
            "above were tried and refused, with each refusal named in\n"
            "``# conformance: ignore[P001] <reason>``.  The directive keeps the opt-out\n"
            "visible and the gate green, but ``check_migration`` still fails on\n"
            "``allow_unbounded_fields=True``, so a kept opt-out stays an open migration\n"
            "item, not an end state.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p001",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
    RuleDefinition(
        id="P002",
        canonical_reference=(
            "atlan-metabase-app app/errors.py — every subclass overrides `code` and never "
            "`category`. The category comes from the SDK leaf you chose to extend; "
            "redeclaring it detaches the class from the taxonomy the dashboards group by."
        ),
        scope=RuleScope.BOTH,
        name="CategoryFieldOverride",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="category-immutability",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.3.0",
        rationale=(
            "FailureCategory is consumed as an immutable reporting metric by the Automation "
            "Engine, SLA dashboards, and on-call routing (ADR-0013). A redeclaration either "
            "duplicates the parent (drifts on rename) or substitutes a different value "
            "(splits one failure mode across two buckets), corrupting the reporting layer "
            "for every downstream consumer. "
            "Customer impact: a drifted category miscounts or misroutes the customer's "
            "failures — an incident that should page as an availability breach files under "
            "the wrong bucket, so SLA reporting understates their outage and on-call "
            "responds late or not at all."
        ),
        short_description="AppError subclass redeclares the `category` ClassVar — drifts the canonical taxonomy",
        full_description=(
            "``FailureCategory`` is the closed, single-axis taxonomy the SDK owns —\n"
            "every value is the canonical answer to *what happened* and is consumed as\n"
            "an immutable reporting metric (dashboards, SLA gates, on-call routing).\n"
            "The categorical leaves in ``application_sdk.errors.leaves`` (and\n"
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
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
    RuleDefinition(
        id="P003",
        canonical_reference=(
            "atlan-openapi-app app/errors.py — every subclass extends an SDK leaf and "
            "declares a code carrying that leaf's prefix (`ZipNoSpecFoundError"
            "(InvalidInputError)` → `INVALID_INPUT_OPENAPI_ZIP_NO_SPEC`, "
            "`SpecFetchAuthError(AuthError)` → `AUTH_OPENAPI_SPEC_FETCH`), and none "
            "overrides to_failure_details, so that code is what dashboards read. The "
            "prefix table itself is application_sdk/errors/leaves.py: the categorical "
            "leaves and the prefix each one owns."
        ),
        terminal_state=(
            "A class whose MRO overrides to_failure_details() builds the wire "
            "envelope itself, so `code` is not what a dashboard reads and adding a "
            "prefixed one would be dead code beside the real one. Those are exempt. "
            "Overriding qualified_code alone is NOT exempt — that is the log surface "
            "only."
        ),
        scope=RuleScope.BOTH,
        name="ErrorCodePrefixMismatch",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="error-code-shape",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.3.0",
        rationale=(
            "Each categorical leaf owns a prefix that embeds its category into every error code "
            "(`AUTH_`, `INTERNAL_`, etc.). Without it, the code column is opaque — dashboards "
            "must join the category column for every query, and subclasses that inherit the bare "
            "leaf code collapse all their distinct failure modes into one undifferentiated bucket. "
            "Customer impact: when distinct failure modes share one code, support cannot tell a "
            "customer's credential expiry from a source-system outage without reading raw logs — "
            "the customer gets a slower, less accurate answer to 'why did my crawl fail'."
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
            "\n"
            "Exempt: classes whose own MRO overrides ``to_failure_details()`` —\n"
            "usually via a shared connector error mixin.  Those build the wire\n"
            "envelope themselves, so ``code`` is not what a dashboard reads and\n"
            "prefixing it would change nothing observable.  The exemption is\n"
            "inherited, so the override may sit on the mixin rather than on each\n"
            "exception class.  Overriding ``qualified_code`` alone does NOT exempt:\n"
            "that is the log surface, and the wire code still collapses to the leaf.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p003",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
    RuleDefinition(
        id="P013",
        canonical_reference=(
            "atlan-metabase-app app/connector.py — `@entrypoint async def "
            "extract_metadata(self, input: MetabaseInput) -> MetabaseOutput`. Both sides "
            "of the boundary are SDK Input/Output subclasses, which is what makes the "
            "payload validatable and the schema evolvable."
        ),
        rule_interactions=(
            "Resolution is by bare class name. Two files declaring the same name, or "
            "a class shadowing its own generated base (`class X(_X)` over `from "
            "generated import X as _X`), used to read as violations on correct code."
        ),
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
            "pre-decorator code paths. "
            "Customer impact: because the runtime rejection fires at import, an untyped "
            "entrypoint that reaches a release does not degrade gracefully — the app "
            "crash-loops at container start in the tenant, taking every workflow the "
            "customer runs on it down with the one bad method."
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
            "Annotations resolve against the *defining module* first: when two\n"
            "files declare the same class name, the one in the file being scanned\n"
            "wins, matching Python. A base that de-aliases to the class's own name\n"
            "(``class X(_X)`` over ``from generated import X as _X``) is an import\n"
            "of a same-named class, so the chain reads as unresolvable rather than\n"
            "as a proven non-contract.\n"
            "\n"
            "Suppressed declarations are still emitted to the SARIF report.\n"
            "This rule is ``BLOCK`` (suppress-only): an unsuppressed violation\n"
            "fails the conformance gate — suppress with\n"
            "``# conformance: ignore[P013] <reason>`` at the method definition.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p013",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
    RuleDefinition(
        id="P014",
        canonical_reference=(
            "atlan-openapi-app app/connector.py — each @task is typed with its own "
            "pair: `extract_spec(self, input: ExtractSpecInput) -> ExtractSpecOutput`, "
            "`download_cloud_spec(...) -> DownloadCloudSpecOutput`, `transform(...) -> "
            "TransformOutput`, all subclassing the SDK Input/Output. A dict or a bare "
            "str across a task boundary has no schema to evolve."
        ),
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
            "violation earlier (PR/CI). "
            "Customer impact: same failure mode as P013 — the import-time rejection means "
            "one untyped task in a shipped release crash-loops the worker in the tenant, "
            "an outage the customer discovers before anyone else does."
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
            "Annotations resolve against the *defining module* first: when two\n"
            "files declare the same class name, the one in the file being scanned\n"
            "wins, matching Python. A base that de-aliases to the class's own name\n"
            "(``class X(_X)`` over ``from generated import X as _X``) is an import\n"
            "of a same-named class, so the chain reads as unresolvable rather than\n"
            "as a proven non-contract.\n"
            "\n"
            "Suppressed declarations are still emitted to the SARIF report.\n"
            "This rule is ``BLOCK`` (suppress-only): an unsuppressed violation\n"
            "fails the conformance gate — suppress with\n"
            "``# conformance: ignore[P014] <reason>`` at the method definition.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p014",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
    RuleDefinition(
        id="P015",
        canonical_reference=(
            "atlan-metabase-app app/contracts.py — the collection filters are "
            "containers of a typed model, `CollectionFilter = Annotated[dict[str, "
            "CollectionSelection], MaxItems(1000)]`, and `CollectResidualsInput.residual_files` "
            "is `Annotated[dict[str, FileReference], MaxItems(16)]`. The value type is what "
            "this rule grades: a bounded dict of str would still fire, because MaxItems "
            "keeps the payload small but gives the keys and values no schema."
        ),
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
            "\n"
            "On a field already recorded in the contract ledger, retyping it to a\n"
            "model in place is a B005 break (the ledger keys a field's type by its\n"
            "name). Add the typed field under a new name and retire the old one, or\n"
            "keep the bounded container and suppress this warning with that reason.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p015",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
    RuleDefinition(
        id="P026",
        canonical_reference=(
            "atlan-openapi-app app/connector.py — `extract_spec` reads "
            "`input.spec_url` as a plain attribute and raises SpecUrlRequiredError "
            "when it is empty. The field is typed, so the right move is to read it and "
            "assert it is present, not to getattr past the type with a default that "
            "silently changes behaviour when the field is renamed."
        ),
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
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
    RuleDefinition(
        id="P027",
        canonical_reference=(
            "No reference app uses app state as a cross-task channel; atlan-metabase-app "
            "app/connector.py mentions `get_app_state` only in a comment about the error "
            "it raises outside app context. Data between tasks travels as typed Output → "
            "Input."
        ),
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
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="programs/areas/prescriptions.prose.md",
        ),
    ),
    RuleDefinition(
        id="P028",
        canonical_reference=(
            "atlan-openapi-app app/asset_mapper.py — qualifiedName comes from "
            "`APISpec.creator()` / `APIPath.creator()`, so the grammar is pyatlan's. Where "
            "a caller genuinely needs the string and not the asset, atlan-metabase-app "
            "app/qualified_names.py carries a per-function ignore[P028] naming the creator "
            "whose grammar it mirrors."
        ),
        terminal_state=(
            "A justified per-function inline `# conformance: ignore[P028] <reason>` IS "
            "the correct end state in two cases, and the reason must say which. "
            "Either the caller needs the qualifiedName STRING and not the asset, and "
            "the f-string mirrors a pyatlan creator's grammar — the reason then names "
            "that creator and the module it lives in, so a drift in pyatlan can be "
            "traced here. Or no pyatlan creator owns the grammar at all (a Process / "
            "ColumnProcess identity, a content-hashed ARS key), in which case the "
            "reason says so and the site is centralised as the single source of truth "
            "rather than repeated. A directive on a site that could simply call the "
            "creator is unremediated."
        ),
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
            "Not flagged — object-store keys: a qn reference preceded by a ``/``-bearing\n"
            "literal segment (e.g.\n"
            '``f"persistent-artifacts/.../{connection_qualified_name}/publish-state"``) is\n'
            "a storage path that embeds a qn, not an asset qualifiedName rooted at its\n"
            "parent qn, so it is not flagged.\n"
            "\n"
            "Fix: construct assets through the pyatlan asset ``.creator()`` factories,\n"
            "which compute qualifiedName from typed parent references.  WARN tier —\n"
            "suppress with ``# conformance: ignore[P028] <reason>`` where a raw\n"
            "qualifiedName string is genuinely required.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p028",
        remediation_reference=RemediationReference(
            kind=RemediationKind.SKILL,
            target="migrate-asset-modeling",
        ),
    ),
    RuleDefinition(
        id="P052",
        canonical_reference=(
            "atlan-openapi-app app/connector.py — `_transform_blocking` writes every "
            "connection, APISpec and APIPath line as `entity_bytes(asset, "
            "entity_type=..., envelope=ENTITY_ENVELOPE)`; no mapper result is "
            "serialized any other way."
        ),
        terminal_state=(
            "A justified inline `# conformance: ignore[P052] <reason>` is the "
            "correct end state only where the value serialized is not an entity "
            "line at all — e.g. a `ConnectionRef` built from `to_atlas_format`, as "
            "the SDK's own `application_sdk/contracts/types.py` does. The reason "
            "must name what the output is used for. A directive on a site that "
            "writes an asset to transformed output is unremediated."
        ),
        scope=RuleScope.APP,
        name="EntitySerializationBypass",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-modeling",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.38.0",
        rationale=(
            "entity_bytes is the SDK's single serialization seam for a mapper "
            "result: it owns connectionName injection, the declared entity "
            "envelope and placeholder-guid stripping. A central fix there reaches "
            "only the apps that go through it; an app that calls "
            "asset.to_nested_bytes() itself silently misses every one, and because "
            "reference apps are copied, the bypass spreads."
        ),
        short_description=(
            "Pyatlan asset serialized in app code without going through entity_bytes"
        ),
        full_description=(
            "App code under ``app/`` (``app/generated/`` excluded) turns a pyatlan\n"
            "asset into wire output itself instead of through\n"
            "``application_sdk.common.asset_serialization.entity_bytes``:\n"
            "\n"
            "* ``<x>.to_nested_bytes()`` or ``<x>.to_nested_dict()``;\n"
            "* ``to_atlas_format(...)`` resolved to ``pyatlan_v9``, or the SDK's\n"
            "  internal ``application_sdk.common.entity_envelope.to_atlas_format_dict``\n"
            "  (also importable from ``application_sdk.common.asset_serialization``)\n"
            "  (a bare imported name, aliased or not, or an attribute call through a\n"
            "  module bound to it).\n"
            "\n"
            "Names resolve by lexical scope, as Python binds them (comprehensions\n"
            "get their own scope; class bodies are skipped): a parameter or local\n"
            "helper that shadows an imported encoder is not flagged, and an import\n"
            "inside one function does not reach another.  Where a name may hold\n"
            "several bindings, the rule fires if any is a bypass: a rebinding in a\n"
            "branch, loop or ``try`` the call is not in may not run, and a\n"
            "function reads a module global when called, so every module binding\n"
            "counts there.  A simple saved alias is followed (``encode =\n"
            "asset.to_nested_bytes; encode()``, ``enc = to_atlas_format``, chained\n"
            "``a = b = …``); ``getattr`` / ``functools.partial`` / container\n"
            "indirection is out of scope.\n"
            "\n"
            "``entity_bytes`` owns the dispatch, the ``connectionName`` injection,\n"
            "the connector's declared entity envelope and the placeholder-guid\n"
            "strip.  Bypassing it means none of those apply, and no SDK-side fix\n"
            "can reach the app.\n"
            "\n"
            "Fix: serialize through\n"
            "``entity_bytes(asset, envelope=...)`` with an envelope that keeps the\n"
            "connector's released wire shape — ``to_nested_bytes()`` /\n"
            "``to_nested_dict()`` output matches ``EnvelopeShape.PYATLAN``,\n"
            "``to_atlas_format()`` output matches ``FLATTENED`` — and pass\n"
            "``connection_name`` / ``last_sync`` unless the mapper already stamps\n"
            "them.  When the line needs a key the model cannot hold, decode what\n"
            "``entity_bytes`` produced and decorate it.  WARN tier — suppress with\n"
            "``# conformance: ignore[P052] <reason>`` only for a genuine non-entity\n"
            "use, such as a ``ConnectionRef`` built from ``to_atlas_format``.\n"
            "\n"
            "The fix needs an envelope choice.  ``PYATLAN`` keeps today's output but\n"
            "is a temporary lever, deprecated in 3.36.0 and removed in v4.0\n"
            "(``application_sdk/common/entity_envelope.py``).  ``FLATTENED`` is the\n"
            "target but changes the wire shape, which is an owner decision; the\n"
            "``migrate-asset-modeling`` skill runs it with a parity check.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p052",
        remediation_reference=RemediationReference(
            kind=RemediationKind.SKILL,
            target="migrate-asset-modeling",
            note=(
                "needs an envelope choice: PYATLAN keeps today's output but is a "
                "temporary lever removed in v4.0; FLATTENED is the target but "
                "changes the wire shape (owner decision); the skill runs it with "
                "a parity check"
            ),
        ),
    ),
    RuleDefinition(
        id="P055",
        canonical_reference=(
            "atlan-metabase-app app/asset_mapper.py — the dashboard and question "
            "mappers link each child to its collection from the child side "
            "(`asset.metabase_collection = RelatedMetabaseCollection(...)`) and "
            "never populate `MetabaseCollection.metabase_dashboards` / "
            "`metabase_questions`."
        ),
        terminal_state=(
            "A justified inline `# conformance: ignore[P055] <reason>` is correct "
            "only where the list end is set on an asset that is never published "
            "ahead of its children — e.g. a value built for a comparison or a "
            "test double. The reason must name why publish ordering cannot apply. "
            "A directive on a mapper that writes the asset to transformed output "
            "is unremediated."
        ),
        scope=RuleScope.APP,
        name="OneToManyLinkFromParent",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asset-modeling",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.44.0",
        rationale=(
            "Publish orders entities by type and sends the '1' side of a 1-to-N "
            "relationship first, relying on each child to reference its parent. "
            "A parent that lists its children instead names entities that do not "
            "exist yet, so Atlas returns ATLAS-404-00-00A and the run fails until "
            "a later run, after the children were created. Nothing at runtime "
            "stops a new connector from writing the link this way, so it recurs "
            "app by app; catching it in the mapper is the earliest point."
        ),
        short_description=(
            "Mapper populates the list end of a 1-to-N relationship instead of "
            "the child's single reference"
        ),
        full_description=(
            "In a module importing ``pyatlan_v9.model.assets``, the mapper sets\n"
            "the list end of a 1-to-N relationship — the parent's\n"
            "``Process.fabric_activities`` or ``Table.columns`` — instead of the\n"
            "single end on each child (``FabricActivity.fabric_process``,\n"
            "``Column.table``):\n"
            "\n"
            "* ``X(..., a=...)`` or ``X.creator(..., a=...)``;\n"
            "* ``x.a = ...``, ``x.a += ...`` or ``x.a.append(...)`` /\n"
            "  ``.extend(...)`` / ``.insert(...)``, where ``x`` is bound in the same\n"
            "  scope to ``X(...)`` / ``X.creator(...)`` or annotated ``X``;\n"
            "* the same assignment or append on a receiver of unknown type, when\n"
            "  the value names the child type (``[RelatedColumn(...)]``,\n"
            "  ``Column.ref_by_qualified_name(...)``) and that pairs with ``a``.\n"
            "\n"
            "The list ends come from a table generated off the pinned pyatlan_v9\n"
            "models (``gen-relationship-directions``): an end whose single-valued\n"
            "inverse is on the child type.  Many-to-many ends such as\n"
            "``Process.inputs`` are not in it, and assigning ``None`` is not\n"
            "flagged.  Raw-dict mappers are out of scope — move them to\n"
            "``pyatlan_v9`` first (O004).\n"
            "\n"
            "Fix: drop the list from the parent and set the single reference on\n"
            "each child, e.g. ``activity.fabric_process =\n"
            "RelatedProcess(qualified_name=process_qn)``.  Publish then sends the\n"
            "parent first and each child references a parent that already exists.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p055",
        remediation_reference=RemediationReference(
            kind=RemediationKind.SKILL,
            target="migrate-asset-modeling",
        ),
    ),
)
