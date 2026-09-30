"""Hosted-handler rule definitions (P054).

An app whose root ``pyproject.toml`` declares ``[tool.atlan-app-api]`` has its
handler served by the consolidated API host. The host installs a wheel built
from the handler's own ``app/`` files on ``atlan-application-sdk-api``, which
carries no structured logger. Handler code reports outcomes through its return
value or a typed ``AppError``, and the SDK's shared routes log the result on
both the worker and the host. So handler code does not log.

The rule scans only the handler module named by ``[tool.atlan-app-api].handler``
and the ``app/`` files it imports. An app without that block is not being
hosted, and the rule is silent for it.

P-ids are a permanent public contract (see ``prescriptions.py``).
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
        id="P054",
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
            "A file in a hosted app's handler code logs.  The handler code is the\n"
            "module ``[tool.atlan-app-api].handler`` names plus every ``app/``\n"
            "module it imports, which is exactly what the consolidated API host\n"
            "installs.\n"
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p054",
    ),
)
