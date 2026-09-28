"""Hosted API member rule definition (P053, FND-2964).

An app hosted on the consolidated API server ships its handler as a uv workspace
member under ``api/<pkg>/`` and names it with a
``[project.entry-points."atlan.app_api"]`` entry point.  The server imports that
package — and only that package — into a process shared with every other hosted
app, without the worker's dependency tree.  P053 keeps the member importable
there.

Like the orchestration-seam rules, P053 is a P-series (prescription) rule in its
own module, backed by its own ``suite.checks.api_member`` registration: it reads
``pyproject.toml`` entry points and ``atlan.yaml`` as well as Python sources,
which none of the per-file P checks do.  P-ids are a permanent public contract
(see ``prescriptions.py``).
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
        id="P053",
        canonical_reference=(
            "application_sdk packages/api/pyproject.toml — the api distribution the "
            "consolidated server installs declares no temporalio, dapr, daft, "
            "duckdb, pandas, pyarrow, boto3 or pyatlan, and packages/api/"
            "application_sdk_api/ never imports application_sdk. A hosted member "
            "mirrors that: it imports application_sdk_api and its own package, and "
            "reads configuration inside handler methods, not at import. No reference "
            "app is hosted yet, so none has an api/ member to cite."
        ),
        scope=RuleScope.APP,
        name="HostedApiMemberNotThin",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="api-member-isolation",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.40.0",
        rationale=(
            "The consolidated API server imports every hosted app's api member into "
            "one process that has atlan-application-sdk-api installed and NOT "
            "atlan-application-sdk. A member that imports application_sdk (or the "
            "app's worker package 'app', which does) fails with ImportError when "
            "the server loads it — or, where the name happens to resolve, drags the "
            "worker's dependency tree into a pod sized for none of it. A "
            "module-level os.environ / os.getenv read runs once, at import, in a "
            "process whose environment belongs to the host, not the app, so the "
            "value is either absent or another app's. And the entry-point name is "
            "the key the server routes the app under: a name that differs from the "
            "app's atlan.yaml name serves the handler at a path nothing calls. "
            "Customer impact: a hosted app whose member fails to import, or "
            "reads another app's configuration, stops serving its credential "
            "test, preflight and metadata-browsing routes on the consolidated "
            "server, so the customer cannot set up or run the connector. "
            "Every one of these breaks the app only once it is hosted, which no "
            "worker test exercises, so the rule is BLOCK from day one — a deliberate "
            "exception to landing new rules as WARN. It is opt-in by construction: "
            "it evaluates nothing unless the repo declares an atlan.app_api entry "
            "point, so it cannot red a repo that has not adopted hosting "
            "(FND-2964)."
        ),
        short_description=(
            "A hosted api/ member imports application_sdk or the worker package, "
            "reads the environment at import, or is named unlike the app"
        ),
        full_description=(
            "**Evaluated only when** some ``pyproject.toml`` in the repo declares\n"
            '``[project.entry-points."atlan.app_api"]`` (FND-2964).  Otherwise\n'
            "the rule is not evaluated and reports nothing.\n"
            "\n"
            'Inside each package an entry point names (``<name> = "<pkg>:handler"``,\n'
            "resolved relative to the declaring ``pyproject.toml``), flags:\n"
            "\n"
            "* any import of ``application_sdk`` or ``application_sdk.*`` — the\n"
            "  member must import ``application_sdk_api`` only (the handler\n"
            "  surface and error taxonomy both live there);\n"
            "* any import of the worker package ``app`` / ``app.*``;\n"
            "* a module-level ``os.environ`` / ``os.getenv`` read — at module or\n"
            "  class-body level, in a decorator or a default argument; a read\n"
            "  inside a function body runs per call and is not flagged.\n"
            "\n"
            "It also flags an entry-point name that differs from the top-level\n"
            "``name:`` in ``atlan.yaml`` (or the generated ``manifest.json``\n"
            "fallback); that sub-check is skipped when no contract name is\n"
            "readable.\n"
            "\n"
            "**Fix.**  Import the handler surface from ``application_sdk_api``;\n"
            "move anything shared with the worker into the member (or into a\n"
            "package both depend on that does not import ``application_sdk``);\n"
            "read configuration inside the handler methods; rename the entry\n"
            "point to the app's name.  Suppress a reviewed exception with\n"
            "``# conformance: ignore[P053] <reason>`` on the line (in\n"
            "``pyproject.toml`` for the name sub-check).\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p053",
    ),
)
