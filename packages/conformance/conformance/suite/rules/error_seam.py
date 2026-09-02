"""Error-seam rule definitions (P043/P045, CONNECT-970).

The SDK owns the **error seam**: ``application_sdk.errors.__all__`` is the public
error contract, and every other module that defines error classes is internal.
Internal modules get reorganised, and the class a caller observes at a boundary
can change, without a deprecation cycle.

Why this series exists
----------------------
A connector app tolerated a legitimately-empty artifact prefix by catching
``FormatReadError``, imported from ``application_sdk.storage.formats.format_errors``.
SDK 3.27.0 added an already-typed pass-through above the wrapping clause in
``storage/formats/json.py``, so that boundary began surfacing a bare
``ObjectStoreReadError``.  The two classes are siblings — they meet only at
``AppError`` — so the handler stopped matching, the guard became dead code, and a
normal empty-prefix condition escaped the activity as a terminal failure on every
retry.  The SDK broke no contract: ``FormatReadError`` was never exported.

The lesson is not "widen the catch".  It is that control flow must rest on the
public surface, where a change is a breaking change someone has to announce.

Scope
-----
Both rules are **app**-scoped: they govern consumer apps, which must consume the
public error surface, and are skipped on the SDK, which publishes it.  Coverage
is currently limited to ``application_sdk.storage.formats.*``; the other SDK
error modules follow once the app-fleet blast radius is measured and the classes
apps legitimately need are promoted.

These are P-series (prescription) rules but live in their own module, modelled on
the orchestration and storage seam series.  P-ids are a permanent public contract
(see ``prescriptions.py``).  The backing check module scans **test files too**,
because the motivating incident had the superseded exception shape frozen into a
unit-test fixture that then passed forever.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    FixLocus,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P043",
        fix_locus=FixLocus.APP,
        scope=RuleScope.APP,
        name="NonPublicErrorControlFlow",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="error-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.21.0",
        rationale=(
            "Only 'application_sdk.errors.__all__' is the SDK's public error "
            "contract. When an app builds control flow on an error class from an "
            "internal module, a minor SDK release can change which class the "
            "boundary surfaces; because the replacement is usually a sibling "
            "rather than a subclass, the 'except' silently stops matching and the "
            "guard becomes dead code with no error and no warning. That is exactly "
            "how a normal empty-prefix condition became a terminal workflow "
            "failure across 12 connections (CONNECT-970)."
        ),
        short_description="App branches on an SDK error class that application_sdk.errors does not export",
        full_description=(
            "A consumer app makes an SDK-internal error class load-bearing in one\n"
            "of five ways: ``except X``, ``except (X, Y)``, ``isinstance(e, X)``,\n"
            "``issubclass(t, X)``, or ``class Y(X)``.  ``X`` resolves to a class\n"
            "under ``application_sdk.storage.formats`` whose name ends in ``Error``\n"
            "and which ``application_sdk.errors`` does not export.\n"
            "\n"
            "This is the defect, not merely a coupling smell.  The SDK is free to\n"
            "change which class an internal boundary raises, and the replacement is\n"
            "typically a *sibling* — the two classes meet only at ``AppError``.  An\n"
            "``except`` on the old class then matches nothing, so the handler never\n"
            "runs.  Nothing fails loudly; the guarded condition simply escapes.\n"
            "\n"
            "Two fixes, depending on the class.  When a public equivalent exists,\n"
            "import it from ``application_sdk.errors``.  When it does not, catch\n"
            "``AppError`` and branch on ``.code`` — error codes are wire contracts\n"
            "consumed by the Automation Engine, so they are more stable than a\n"
            "class's module location.  A wide net with a narrow decision also covers\n"
            "both the pre-change and post-change exception shapes at once, which\n"
            "matters whenever an app's SDK range spans the change.\n"
            "\n"
            "A bare annotation such as ``def f() -> X | None`` is not flagged here;\n"
            "it changes no behaviour, and P045 already covers the import.\n"
            "\n"
            "Resolution covers the ``from X import Y`` form only: a qualified-\n"
            "attribute reference such as ``import ...format_errors as fe`` followed\n"
            "by ``except fe.FormatReadError`` resolves to no directly bound name and\n"
            "is a documented non-goal (every real occurrence across the app fleet\n"
            "uses the direct form).\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P043]\n"
            "<reason>`` records any unavoidable exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p043",
    ),
    RuleDefinition(
        id="P045",
        fix_locus=FixLocus.APP,
        scope=RuleScope.APP,
        name="PrivateErrorClassImport",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="error-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.21.0",
        rationale=(
            "An error class the SDK does not export from 'application_sdk.errors' is "
            "an implementation detail. Importing one is the coupling that makes the "
            "P043 defect possible, and it is invisible in review: the diff that "
            "breaks it is a lockfile bump, and no reviewer reads a lockfile and "
            "infers that an exception-class assumption in another file just became "
            "false (CONNECT-970)."
        ),
        short_description="App imports an SDK error class from an internal module instead of application_sdk.errors",
        full_description=(
            "A consumer app imports a class whose name ends in ``Error`` from a\n"
            "module under ``application_sdk.storage.formats`` — most often\n"
            "``application_sdk.storage.formats.format_errors``.  These modules are\n"
            "not the public error surface; ``application_sdk.errors.__all__`` is.\n"
            "They carry no deprecation guarantee, so the class can move, be\n"
            "re-parented, or stop being the one a given boundary raises, in a minor\n"
            "release.\n"
            "\n"
            "Import the class from ``application_sdk.errors`` when it is exported\n"
            "there.  When it is not, do not reach for it at all: catch ``AppError``\n"
            "and branch on ``.code``.  If your app genuinely needs a typed class\n"
            "that is not exported, raise it with the SDK team so it can be promoted\n"
            "deliberately, rather than depended on by accident.\n"
            "\n"
            "Only ``Error``-suffixed names are flagged.  These modules also hold\n"
            "helper functions that apps use legitimately (for example\n"
            "``convert_datetime_to_epoch`` and ``process_null_fields`` from\n"
            "``storage.formats.utils``), and those have no public equivalent to move\n"
            "to, so flagging them would only farm suppressions.\n"
            "\n"
            "Resolution covers the ``from X import Y`` form only: a qualified-\n"
            "attribute reference such as ``import ...format_errors as fe`` followed\n"
            "by ``except fe.FormatReadError`` resolves to no directly bound name and\n"
            "is a documented non-goal (every real occurrence across the app fleet\n"
            "uses the direct form).\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P045]\n"
            "<reason>`` records any unavoidable exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p045",
    ),
)
