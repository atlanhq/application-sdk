"""P016 EntryPointContractCodeDrift — entry-point name alignment rule (BLDX-1425).

Two independent sources name an app's entry points, and they drift silently:

1. **Contract (toolkit-owned):** ``contract/app.pkl`` → ``pkl eval`` →
   ``app/generated/<name>/manifest.json``.  The ``<name>`` drives the manifest
   dir, the ``workflow_type``, and what callers pass as ``?entrypoint=<name>``.

2. **Code (developer-owned):** ``@entrypoint``-decorated ``App`` methods.  The
   registered wire name is the explicit ``name=`` argument or the Python method
   name in kebab-case (``extract_metadata`` → ``extract-metadata``).

``poe generate`` only runs ``pkl eval``; it never imports app code, so the two
can drift silently.  When they do, the HTTP create path may fail: e.g.
``/manifest?entrypoint=crawler`` returns 200 while
``/input-contract?entrypoint=crawler`` returns 404, with no caller-side
workaround — the exact failure seen on a production connector app (BLDX-1425).

Scope
-----
``APP`` only: consumer apps have a ``contract/app.pkl``; the SDK itself does not.
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
        id="P016",
        scope=RuleScope.APP,
        name="EntryPointContractCodeDrift",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="entrypoint-alignment",
        autofixable=False,
        since="0.6.0",
        rationale=(
            "Two independent sources define an app's entry-point names: the contract "
            "(contract/app.pkl → pkl eval → app/generated/<name>/ dirs) and the code "
            "(@entrypoint-decorated App methods). poe generate only runs pkl eval and "
            "never imports app code, so the two can drift silently. When they drift, "
            "the HTTP create path fails — /manifest?entrypoint=<name> may return 200 "
            "while /input-contract?entrypoint=<name> returns 404 — with no caller-side "
            "workaround (BLDX-1425)."
        ),
        short_description=(
            "Entry-point names in @entrypoint code do not match app/generated/ contract dirs"
        ),
        full_description=(
            "An app's entry-point names are declared in two independent places:\n"
            "\n"
            "1. **Contract (toolkit-owned):** ``contract/app.pkl`` → ``pkl eval`` →"
            " ``app/generated/<name>/manifest.json``.  The ``<name>`` is what callers"
            " pass as ``?entrypoint=<name>`` and the Automation Engine ``workflow_type``.\n"
            "\n"
            "2. **Code (developer-owned):** ``@entrypoint``-decorated ``App`` methods."
            " The registered wire name is the explicit ``name=`` argument or the"
            " method name in kebab-case (``extract_metadata`` → ``extract-metadata``).\n"
            "\n"
            "``poe generate`` only runs ``pkl eval``; it never imports app code, so the"
            " two can drift silently.  Drift causes the HTTP create path to fail —"
            " ``/manifest?entrypoint=crawler`` returns 200 while"
            " ``/input-contract?entrypoint=crawler`` returns 404 — with no caller-side"
            " workaround (BLDX-1425).\n"
            "\n"
            "**This rule checks (per repo):**\n"
            "\n"
            "* **Multi-entry-point apps** (``app/generated/`` has named subdirs each"
            " containing ``manifest.json``): the set of ``@entrypoint`` wire names must"
            " exactly equal the set of subdir names.\n"
            "\n"
            "* **Single-entry-point apps** (root ``app/generated/manifest.json``, no"
            " subdirs): at most one ``@entrypoint`` is permitted in code.  The name is"
            " not constrained — the single entry point is the implicit default.\n"
            "\n"
            "* **Non-literal ``name=``:** a ``@entrypoint(name=variable)`` that cannot"
            " be statically resolved is flagged as unverifiable.\n"
            "\n"
            "* **No ``app/generated/``:** the repo is not a native-app-contract repo;"
            " this rule is a no-op.\n"
            "\n"
            "**Remediation**\n"
            "\n"
            "The contract ``app/generated/`` tree is the authoritative source; align"
            " code to it as the default fix:\n"
            "\n"
            "1. For each ``@entrypoint`` name in *code* that is not in the contract,"
            ' pin it: ``@entrypoint(name="<contract-name>")``.  Confirm the pairing'
            " (code name → contract name) intentionally — renaming an entry point is"
            " a **breaking wire change** (the Temporal workflow type and"
            " ``?entrypoint=`` value both change; coordinate with callers).\n"
            "\n"
            "2. For each contract name not matched in code, add or rename an"
            ' ``@entrypoint(name="<missing-name>")`` on the corresponding App method.\n'
            "\n"
            "3. If the *code* name is the intended one and the contract is wrong, update"
            ' the ``entrypoints { new Entrypoint { name = "..." } }`` block in'
            " ``contract/app.pkl`` and re-run ``pkl eval`` so the"
            " ``app/generated/<name>/`` dir and ``workflow_type`` follow — never"
            " hand-edit ``app/generated/`` (C002 catches stale generated artifacts).\n"
            "\n"
            "4. For a single-entry-point app that now has multiple ``@entrypoint``s,"
            " either add named ``Entrypoint`` blocks in ``contract/app.pkl`` or"
            " remove the extra ``@entrypoint``.\n"
            "\n"
            "Land as ``BLOCK``: a justified inline ``# conformance: ignore[P016]\n"
            "<reason>`` on the decorator line (or the comment-only line directly above"
            " it) records the exception and stays visible in SARIF.\n"
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/prescriptions.md#p016"
        ),
    ),
)
