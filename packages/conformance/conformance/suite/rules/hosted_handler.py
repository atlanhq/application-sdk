"""Hosted-handler rule definitions (P060).

An app whose root ``pyproject.toml`` declares ``[tool.atlan-app-api]`` has its
handler served by the consolidated API host. The host installs a wheel built
from the handler's own ``app/`` files on ``atlan-application-sdk-api``, which
carries no structured logger. Handler code reports outcomes through its return
value or a typed ``AppError``, and the SDK's shared routes log the result on
both the worker and the host. So handler code does not log.

The rule scans only the handler module named by ``[tool.atlan-app-api].handler``.
The ``app/`` files it imports (a shared SQL client, say) also run on the worker,
so they keep their logging and import the logger from
``application_sdk.common.logger``. An app without that block is not being
hosted, and the rule is silent for it.

P-ids are a permanent public contract (see ``prescriptions.py``).
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
        id="P060",
        canonical_reference=(
            "atlan-mysql-app app/handler.py — the hosted handler returns an "
            "AuthOutput / PreflightOutput or raises a typed AppError from "
            "app/failures.py and never logs; the SDK's shared routes "
            "(application_sdk/handler/routes.py) log every outcome with the request "
            "id on both the worker and the consolidated API host."
        ),
        scope=RuleScope.APP,
        name="HostedHandlerLogs",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="hosted-handler",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.41.0",
        rule_interactions=(
            "Evaluated only for an app that declares [tool.atlan-app-api]: that "
            "block is the app opting into the consolidated API host, and complying "
            "is part of that migration. application-sdk's gen_app_api.py `fix` "
            "removes the flagged statements."
        ),
        rationale=(
            "The consolidated API host serves every hosted app's handler from one "
            "process on atlan-application-sdk-api, which carries no structured "
            "logger. Handler log lines there would reach a stdlib logger with none "
            "of the worker's context, duplicate what the SDK's shared routes already "
            "log per request, and risk printing request credentials. The handler's "
            "result or typed AppError is the report; the routes log it once, the "
            "same way on both surfaces. Customer impact: a handler log line on the "
            "shared host can carry request credentials (a driver error embedding a "
            "DSN, a logged request body) into logs every hosted app's operators "
            "read, outside the redaction the SDK's routes apply."
        ),
        short_description=(
            "Hosted handler code logs; return the result or raise a typed AppError "
            "and let the SDK's routes log it"
        ),
        full_description=(
            "A hosted app's handler module logs.  That is the module\n"
            "``[tool.atlan-app-api].handler`` names.  The ``app/`` modules it\n"
            "imports also run on the worker, so they may log; ``gen_app_api.py\n"
            "fix`` moves their logger import to ``application_sdk.common.logger``,\n"
            "which the consolidated API host also has.\n"
            "\n"
            "Fires on each of:\n"
            "\n"
            "* a call to ``<logger>.debug / info / warning / warn / error /\n"
            "  exception / critical / log(...)`` on a name bound to\n"
            "  ``get_logger(...)`` or ``logging.getLogger(...)`` (or named\n"
            "  ``logger`` / ``log``), or on the ``logging`` module itself;\n"
            "* ``self.context.log_debug / log_info / log_warning / log_error(...)``;\n"
            "* binding a logger (``logger = get_logger(__name__)``);\n"
            "* importing ``logging``, ``loguru``, ``get_logger``, or anything from\n"
            "  ``application_sdk.observability``.\n"
            "\n"
            "Report through the handler's return value (``AuthOutput``,\n"
            "``PreflightOutput`` with its checks) or a typed ``AppError``: the SDK's\n"
            "shared routes log every outcome, with the request id, on the worker\n"
            "and on the host alike.\n"
            "\n"
            "application-sdk's ``.github/scripts/gen_app_api.py fix`` deletes these\n"
            "statements (a block left empty gets ``pass``); review what it removed,\n"
            "since a log line was sometimes the only report of a failure the\n"
            "handler should instead raise as a typed ``AppError``.\n"
            "\n"
            "Silent for an app without ``[tool.atlan-app-api]``: it is not hosted.\n"
            "Blocks for one that has it, because declaring the block is the app\n"
            "moving to the host.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p060",
        remediation_reference=RemediationReference(
            kind=RemediationKind.GUIDE,
            target="docs/hosted-handler-guide.md",
        ),
    ),
    RuleDefinition(
        id="P061",
        canonical_reference=(
            "atlan-mysql-app pyproject.toml — the [tool.atlan-app-api] block names "
            'handler = "app.handler:MySQLAppHandler", a Handler subclass in app/ '
            "whose files import each other relatively; the repo has no server/ "
            "package, and the host stages that handler from app/."
        ),
        scope=RuleScope.APP,
        name="HostedHandlerMigration",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="hosted-handler",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.51.0",
        rule_interactions=(
            "Logging in the handler module is P060's; this rule never reports it. "
            "Once an app declares [tool.atlan-app-api], application-sdk's "
            "gen_app_api.py check (the api-member CI job) enforces the same "
            "handler-misuse cases as a test, and this rule mirrors them."
        ),
        rationale=(
            "The consolidated API host serves many apps' HTTP surfaces from one "
            "process, so an app stops running its own always-on API pod. An app "
            "gets there by serving its real app/ handler through "
            "atlan-application-sdk-api: one [tool.atlan-app-api] block, no copied "
            "code. Until then each app either runs its own server pod or keeps a "
            "hand-written server/ copy on atlan-server-sdk that drifts from the real "
            "handler. Customer impact: a drifted server/ copy answers test-credential, "
            "preflight and metadata calls differently from the worker that runs the "
            "extraction, so a credential that tests green can fail the run."
        ),
        short_description=(
            "App is not yet served from the consolidated API host the supported way"
        ),
        full_description=(
            "Reports what still stands between an app and the consolidated API\n"
            "host.  Each finding hands off to the\n"
            "``api-server-consolidation-migration`` skill.\n"
            "\n"
            "* **Not migrated** — a class under ``app/`` subclasses the SDK's\n"
            "  ``Handler`` and the root ``pyproject.toml`` has no\n"
            "  ``[tool.atlan-app-api]`` block.\n"
            "* **Old path still present** — ``server/pyproject.toml`` depends on\n"
            "  ``atlan-server-sdk``: a hand-written handler copy.  Delete it once\n"
            "  the host's pin serves the app's own handler, not before.\n"
            "* **Handler not used correctly** (the block is present) — ``handler``\n"
            "  does not name a ``Handler`` subclass in ``app/``; a ``data`` glob\n"
            "  matches nothing; or the handler's import closure has an absolute\n"
            "  ``from app.`` import, an import that moved off the host\n"
            "  (``execution.heartbeat.run_in_thread``,\n"
            "  ``observability.logger_adaptor.get_logger``), or an\n"
            "  ``APPLICATION_NAME`` read (on the host that names the host; use\n"
            "  ``self.context.app_name``).\n"
            "* **Cannot be hosted yet** — an ``app/<entrypoint>/core.py`` defines\n"
            "  ``compute_manifest``.  The host serves static manifests only, so the\n"
            "  hook has to become static data first.\n"
            "\n"
            "The handler-misuse and hook cases mirror application-sdk's\n"
            "``gen_app_api.py check``.  Logging in the handler module is P060.\n"
            "WARN: the host is rolling out app by app.  Suppress with\n"
            "``# conformance: ignore[P061] <reason>`` on the reported line.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p061",
        remediation_reference=RemediationReference(
            kind=RemediationKind.SKILL,
            target="api-server-consolidation-migration",
        ),
    ),
)
