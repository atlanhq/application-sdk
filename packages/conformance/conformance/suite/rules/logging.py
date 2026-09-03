"""Logging rule definitions (L-series)."""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="L001",
        canonical_reference=(
            'atlan-hello-world-app app/connector.py — `summarize` logs "summarize '
            'completed record_count=%d message=%s" with the values passed positionally. '
            "One template, so every run of that line groups together in ClickHouse."
        ),
        scope=RuleScope.BOTH,
        name="FStringInLogMessage",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.4.0",
        rationale=(
            "%-style message bodies are the fleet-wide logging convention: one consistent "
            "call-site style keeps log statements legible and reviewable, and the SDK's "
            "loguru bridge renders the values in. Beyond consistency, %-style now carries "
            "a real performance guarantee: the SDK adapter short-circuits before interpolation "
            "when the level is filtered, so __str__ is never called on the arguments — the "
            "same laziness stdlib logging provides for free. f-strings always evaluate "
            "eagerly at the call site regardless of level, so they pay the formatting cost "
            "even when the record is never emitted. "
            "Customer impact: during a tenant incident the on-call groups and counts log "
            "records by message template; an f-string explodes one failure signature into "
            "thousands of unique strings, so the signal that would localise the customer's "
            "outage cannot be found or trended, extending time-to-resolution."
        ),
        short_description="f-string in log message — breaks log grouping and aggregation",
        full_description=(
            "Using an f-string creates a unique message string per call, breaking log\n"
            "grouping and aggregation in Grafana/ClickHouse.  It also always evaluates\n"
            "eagerly — __str__ / __format__ is called on every interpolated value even\n"
            "when the level is filtered and the record is never emitted.  %-style avoids\n"
            "both: the SDK adapter's _is_enabled guard short-circuits before interpolation,\n"
            "so argument __str__ is skipped entirely for filtered levels.  Rewrite as\n"
            "%-style message body: embed context directly in the format string, do not\n"
            "move values to kwargs.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l001",
    ),
    RuleDefinition(
        id="L002",
        canonical_reference=(
            "atlan-mysql-app app/handler.py — `get_logger(__name__)` at module scope, "
            "imported from application_sdk.observability.logger_adaptor. No reference app "
            "calls logging.getLogger, structlog.get_logger, or loguru's logger."
        ),
        scope=RuleScope.BOTH,
        name="NonCanonicalLoggerFactory",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.4.0",
        rationale=(
            "The SDK adapter (application_sdk.observability.logger_adaptor.get_logger) "
            "is the only sanctioned way to obtain a logger. It injects Temporal context "
            "(workflow_id, run_id, activity_type) as top-level indexed columns in "
            "ClickHouse/Grafana and routes records through OTel. Direct use of "
            "logging.getLogger(), structlog.get_logger(), or loguru's logger bypasses "
            "all of this — correlation IDs are lost and records may not reach the "
            "observability store. Promoted from warn to block (CNCT-108, parent "
            "CNCT-93): rolled-own loggers strip correlation_id/workflow context/source "
            "provenance, making those lines unfindable on the tenant UI. "
            "Customer impact: when a customer's workflow fails, the Workflow Center shows "
            "'No error logs available' for the step even though the app logged everything — "
            "the customer waits while support hunts for records that were never indexed "
            "under the run."
        ),
        short_description=(
            "Non-canonical logger factory — use "
            "`from application_sdk.observability.logger_adaptor import get_logger`"
        ),
        full_description=(
            "Every module must obtain its logger via the SDK adapter::\n"
            "\n"
            "    from application_sdk.observability.logger_adaptor import get_logger\n"
            "    logger = get_logger(__name__)\n"
            "\n"
            "Direct use of ``logging.getLogger()``, ``structlog.get_logger()``, or\n"
            "``from loguru import logger`` bypasses the adapter that:\n"
            "\n"
            "* injects Temporal context fields (``workflow_id``, ``run_id``,\n"
            "  ``activity_type``, ``task_queue``, ``attempt``) as top-level indexed\n"
            "  columns in ClickHouse/Grafana;\n"
            "* routes log records through OTel so they appear in the observability\n"
            "  store;\n"
            "* enforces the project's five-level model\n"
            "  (DEBUG/INFO/WARNING/ERROR/CRITICAL).\n"
            "\n"
            "Adapter definition files are exempt — the file that defines\n"
            "``AtlanLoggerAdapter`` or ``get_logger`` itself is skipped.  Dev\n"
            "harnesses are exempt too: files under ``scripts/`` and\n"
            "``run_dev*.py`` never run inside a workflow, so the provenance\n"
            "argument does not apply (test files are already excluded by\n"
            "discovery).  Block-tier since CNCT-108 (parent CNCT-93): a\n"
            "rolled-own logger loses correlation IDs and source provenance,\n"
            "making its records unfindable on the tenant UI.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l002",
    ),
    RuleDefinition(
        id="L003",
        canonical_reference=(
            "No reference app passes `extra={}`. Context travels positionally in the "
            '%-style body — atlan-metabase-app app/utils.py, `to_epoch_ms`: "Datetime %r '
            'did not match format %r", dt_str, fmt.'
        ),
        scope=RuleScope.BOTH,
        name="ExtraKwargsWrongFramework",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "Whether kwargs land in indexed top-level fields or an unindexed nested dict "
            "depends on the framework. The wrong form routes context where aggregation "
            "queries can't reach — present in the record but invisible to GROUP BY/filter."
        ),
        short_description="extra={} used where framework expects direct kwargs (or vice versa)",
        full_description=(
            "Whether ``extra={}`` is correct depends on the logging framework.  For\n"
            "structlog and loguru, ``extra={}`` is usually wrong — the data lands in an\n"
            "unindexed nested dict invisible to aggregation queries.  Framework-dependent\n"
            "classification; the checker must detect the active framework first.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l003",
    ),
    RuleDefinition(
        id="L004",
        canonical_reference=(
            "atlan-metabase-app app/handler.py — every log call inside an except block "
            "carries exc_info=True, in `test_auth` and in each of the preflight check "
            "helpers. The rule is about the except block, not about the level."
        ),
        scope=RuleScope.BOTH,
        name="ExceptBlockMissingExcInfoLog",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="missing-traceback",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.4.0",
        rationale=(
            "Same failure as E005 at the logging layer: the message appears in the stream "
            "but the stack trace is absent, so every postmortem hitting this pattern must "
            "reproduce the failure to find root cause. "
            "Customer impact: root-causing a customer-reported failure now requires "
            "reproducing it against their source system — often impossible without their "
            "data — so the incident stays open for days instead of being read off the trace."
        ),
        short_description="logger.warning/error in except block without exc_info=True",
        full_description=(
            "Logging an exception without ``exc_info=True`` produces a message with no\n"
            "stack trace — the root cause is invisible.  Add ``exc_info=True`` to all\n"
            "``logger.warning()`` / ``logger.error()`` calls within an except block.\n"
            "``.exception()`` is exempt.\n"
            "\n\nExempt: calls whose arguments flow through a recognised redaction\n"
            "helper (redact*/sanitiz*/safe_traceback/scrub_secret*/mask_secret*) —\n"
            "these mark a deliberate no-traceback boundary where exc_info=True\n"
            "would serialize the raw exception past the sanitizer and can leak\n"
            "credentials (JDBC URLs, Authorization headers, OAuth bodies)."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l004",
    ),
    RuleDefinition(
        id="L005",
        canonical_reference=(
            "atlan-mysql-app pyproject.toml — T201 sits in the repo-wide lint select and "
            "is ignored only for `.github/**/*.py`, where a CI script's stdout is the "
            "point. No print() exists under app/."
        ),
        scope=RuleScope.BOTH,
        name="PrintInProductionCode",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        since="0.4.0",
        rationale=(
            "print() bypasses the logging adapter entirely: no level, no correlation ID, "
            "no structured fields, no OTel forwarding. In containers, stdout may route to a "
            "different sink or interleave with structured lines, invisible to observability."
        ),
        short_description="print() in production code — bypasses logging framework",
        full_description=(
            "``print()`` produces no level, no structured fields, no correlation IDs.\n"
            "In production services, output may go to stdout unformatted, be lost, or\n"
            "interleave with structured log lines.  Acceptable in CLI scripts, test/debug\n"
            'scripts, and ``if __name__ == "__main__":`` blocks.\n'
            "\n\nExempt: standalone scripts/CLIs — files with a shebang or an\n"
            "if __name__ == '__main__' guard. For those,\n"
            "stdout is the user interface, not a logging bypass."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l005",
    ),
    RuleDefinition(
        id="L006",
        canonical_reference=(
            "atlan-metabase-app app/extracts/process.py — the per-dashboard skip inside "
            "`process_assets` logs at DEBUG. INFO belongs to the run's lifecycle, not to "
            "one iteration of it."
        ),
        scope=RuleScope.BOTH,
        name="InfoInTightLoop",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-level",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "Per-item INFO in a large loop emits O(N) records at the level operators "
            "monitor, drowning lifecycle signals in noise and inflating storage cost. INFO "
            "is for milestones; per-item progress belongs at DEBUG."
        ),
        short_description="logger.info() inside a tight loop — generates excessive log volume",
        full_description=(
            "Per-item INFO logging in a large loop drowns meaningful signals and\n"
            "degrades performance.  INFO is for lifecycle milestones, not per-item\n"
            "events.  Use DEBUG per-item and INFO for the loop summary.  The checker\n"
            "should inspect whether the loop is clearly bounded (≤10 items) before\n"
            "flagging.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l006",
    ),
    RuleDefinition(
        id="L007",
        canonical_reference=(
            "No reference app calls logger.critical(). The top severity in use is ERROR at "
            "a boundary — atlan-mysql-app app/handler.py, `test_auth`. There is no "
            "CRITICAL sink behind the adaptor, so the level only costs a reader their "
            "filter."
        ),
        scope=RuleScope.BOTH,
        name="LoggerCriticalUsage",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-level",
        autofixable=True,
        since="0.4.0",
        rationale=(
            "ADR-0011 codifies exactly four levels (DEBUG/INFO/WARNING/ERROR) — there is no "
            "CRITICAL. Fatal conditions are communicated through process exit codes and "
            "Temporal workflow failure, not a log level, so a CRITICAL record adds a fifth "
            "level nothing in the stack is built to consume. Use ERROR (with exc_info=True) "
            "and let the failure propagate."
        ),
        short_description="logger.critical() — CRITICAL is not a meaningful level here",
        full_description=(
            "CRITICAL is not a meaningful level in distributed systems — every service\n"
            'failure is "critical" from some perspective.  Use ERROR and handle severity\n'
            "through alerting rules on the observability platform.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l007",
    ),
    RuleDefinition(
        id="L008",
        canonical_reference=(
            'atlan-mysql-app app/client.py — `provide_token` logs "IAM token refreshed '
            'for connection (length: %d)", len(token). The argument is cheap, and %-style '
            "defers interpolation until the level is known to be enabled."
        ),
        scope=RuleScope.BOTH,
        name="UnguardedExpensiveDebug",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-performance",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "Python evaluates all function arguments before calling the log method, so an "
            "expensive expression in a log argument — json.dumps(big), obj.serialize(), a "
            "comprehension — runs unconditionally even when the level is filtered. "
            "The SDK adapter's _is_enabled guard short-circuits %-style __str__ interpolation "
            "for simple object args, but it fires inside the method, after Python has already "
            "evaluated every argument expression. Calls with expensive argument expressions "
            "still need an explicit guard."
        ),
        short_description="Expensive computation in logger.debug() argument — evaluates eagerly",
        full_description=(
            "Python evaluates all arguments before calling the log method, so expensive\n"
            "expressions in log arguments run on every call regardless of level:\n"
            "\n"
            "    logger.debug('snapshot: %s', json.dumps(big_dict))  # json.dumps always runs\n"
            "\n"
            "Note: the SDK adapter's _is_enabled guard does skip %-style __str__ calls for\n"
            "simple object args (logger.debug('x: %s', obj) — obj.__str__ not called when\n"
            "filtered).  But that guard fires inside the method, after Python has evaluated\n"
            "the argument expressions.  When the expensive work is in the expression itself,\n"
            "an explicit level guard is still required::\n"
            "\n"
            "    if logger.isEnabledFor(logging.DEBUG):\n"
            "        logger.debug('snapshot: %s', json.dumps(big_dict))\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l008",
    ),
    RuleDefinition(
        id="L009",
        canonical_reference=(
            "atlan-hello-world-app app/connector.py — `generate_greetings` raises "
            "InvalidRepeatCountError with no log line before it. The raise is the record; "
            "whichever handler catches it logs it once."
        ),
        scope=RuleScope.BOTH,
        name="WarnThenRaiseDuplication",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-noise",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "Logging immediately before re-raising creates two records for one event (raise "
            "site + handler), inflating error counts and making 'how many times did this "
            "fail?' unanswerable without dedup logic."
        ),
        short_description="logger.warning/error immediately before raise — duplicate log records",
        full_description=(
            "Logging an error immediately before re-raising creates duplicate records in\n"
            "the log stream, inflating error counts in dashboards.  Acceptable only\n"
            "when adding context not available to the caller.  Otherwise: just re-raise.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l009",
    ),
    RuleDefinition(
        id="L010",
        canonical_reference=(
            "atlan-mysql-app app/client.py — `get_iam_role_token` logs that AWS "
            "credentials were staged into the environment and names none of them. Log that "
            "a credential was used, never the credential."
        ),
        scope=RuleScope.BOTH,
        name="CredentialInLogOutput",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="security",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.4.0",
        rationale=(
            "Log aggregation stores records in plaintext accessible to more people and "
            "systems than the credential store. A credential value in a log is a persistent "
            "exposure that survives rotation and is indexed for search. "
            "Customer impact: the value leaked is the customer's own source-system "
            "credential — one occurrence in a tenant is a reportable security incident and "
            "can obligate the customer to rotate production database access, regardless of "
            "whether it was ever exploited."
        ),
        short_description="Credential/secret value in log output — security vulnerability",
        full_description=(
            "Credentials in log output are a security vulnerability — logs are often\n"
            "stored in plaintext in log aggregation systems, accessible to more people\n"
            "than the credential store.  Requires human security review before marking\n"
            "acceptable.  Logging a credential *name* is acceptable; logging a\n"
            "credential *value* is CRITICAL.\n"
            "\n\nExempt: arguments assigned a redaction placeholder in the module\n"
            '(e.g. password = "[REDACTED]" if creds.get("password") else None) —\n'
            "logging them is a presence indicator, not a value leak."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l010",
    ),
    RuleDefinition(
        id="L011",
        canonical_reference=(
            "atlan-metabase-app app/extracts/databases.py — `fetch_databases_summaries` "
            'logs "Failed to fetch databases: %s" with the status as an argument, so the '
            "template stays constant across every failure."
        ),
        scope=RuleScope.BOTH,
        name="StringConcatenationInLog",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        since="0.4.0",
        rationale=(
            "Same convention as L001: string concatenation is an ad-hoc alternative to the "
            "standard %-style message body. It reads worse at the call site and breaks "
            "fleet-wide consistency for no benefit; rewrite as a %-style message body. "
            "Customer impact: same failure surface as L001 — concatenated values fragment "
            "the message template, so the log signature an on-call needs to find and count "
            "a customer-affecting failure never groups in the aggregation store."
        ),
        short_description="String concatenation in log message — breaks log grouping",
        full_description=(
            "Like f-strings (L001), string concatenation embeds values into the message\n"
            "string in a way that breaks log grouping.  Rewrite as %-style message body.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l011",
    ),
    RuleDefinition(
        id="L012",
        canonical_reference=(
            "No app builds an `extra={}` dict at all — "
            "application_sdk/observability/logger_adaptor.py takes %-style arguments "
            "positionally and injects the Temporal context itself, so there is no "
            "caller-supplied key that can collide with a stdlib LogRecord attribute."
        ),
        scope=RuleScope.BOTH,
        name="StdlibExtraReservedKeyCollision",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-crash",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "stdlib's Logger.makeRecord() raises KeyError when an extra={} key collides "
            "with a LogRecord attribute, propagating to the caller's logger.info() site and "
            "crashing it. The 22 forbidden keys include natural choices: name, message, "
            "module, args, filename. "
            "Customer impact: the crash detonates on the first code path that logs with the "
            "colliding key — typically an error path exercised only in production — so a "
            "customer run dies with a KeyError raised by its own logging call instead of "
            "reporting the original problem."
        ),
        short_description="extra={} key collides with stdlib LogRecord attribute — crashes caller",
        full_description=(
            "stdlib's ``Logger.makeRecord()`` raises ``KeyError`` if any key in\n"
            "``extra={}`` matches a ``LogRecord`` attribute.  This crash propagates\n"
            "directly to the caller — NOT caught by ``handleError()``.  The 22 forbidden\n"
            "keys include: ``name``, ``message``, ``module``, ``args``, ``filename``,\n"
            "``process``, ``thread``.  Applies to stdlib only.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l012",
    ),
    RuleDefinition(
        id="L013",
        canonical_reference=(
            "atlan-openapi-app app/api_client.py — the logger comes from `get_logger`, "
            "which accepts the SDK adaptor's kwargs. A stdlib logging.Logger appears "
            "nowhere in the four reference apps, and it is the stdlib one that raises "
            "TypeError on arbitrary kwargs."
        ),
        scope=RuleScope.BOTH,
        name="StdlibArbitraryKwargs",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-crash",
        autofixable=True,
        since="0.4.0",
        rationale=(
            "stdlib logger.info() raises TypeError immediately for any kwarg outside its "
            "short allowlist. The most common breakage when migrating from structlog (which "
            "accepts arbitrary kwargs) — call sites look identical but fail at runtime. "
            "Customer impact: any customer run that reaches the miswritten call site crashes "
            "with a TypeError from the logging layer — a latent landmine on every code path "
            "tests did not execute, detonating first in the tenant."
        ),
        short_description="Arbitrary kwargs in stdlib logger — raises TypeError immediately",
        full_description=(
            "stdlib ``logger.info()`` only accepts ``exc_info``, ``extra``,\n"
            "``stack_info``, and ``stacklevel``.  Any other kwarg raises ``TypeError``\n"
            "and crashes the caller.  Very common when migrating from structlog/loguru.\n"
            "Applies to stdlib only.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l013",
    ),
    RuleDefinition(
        id="L014",
        canonical_reference=(
            "atlan-metabase-app app/api_types.py — one factory, `get_logger`, in every "
            "module. structlog is not a dependency of any reference app, so no call site "
            "can shadow the message with an `event=` kwarg."
        ),
        scope=RuleScope.BOTH,
        name="StructlogEventKwargOverwrite",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "In structlog the first positional arg is the message (stored as 'event'). "
            "Passing event= as a keyword silently replaces the message with the domain "
            "value, corrupting both message and field in one call."
        ),
        short_description="event= kwarg in structlog silently overwrites the log message",
        full_description=(
            "In structlog, the first positional argument is stored as the ``event`` key\n"
            "— it IS the log message.  Passing ``event=`` as a keyword argument silently\n"
            "overwrites the message with the domain value.  Rename the domain field to\n"
            "avoid collision.  Applies to structlog only.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l014",
    ),
    RuleDefinition(
        id="L015",
        canonical_reference=(
            "atlan-hello-world-app app/run_dev.py — the app awaits `run_dev_combined` and "
            "configures no logging of its own. Handler configuration belongs to the SDK "
            "runtime; an app calling dictConfig is reaching past it."
        ),
        scope=RuleScope.BOTH,
        name="DictConfigDisableExistingLoggers",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-config",
        autofixable=True,
        since="0.4.0",
        rationale=(
            "dictConfig() defaults disable_existing_loggers=True, silently disabling every "
            "logger created before the call. SDK components create loggers at import — before "
            "any app dictConfig() — so a misconfigured call makes all library logging vanish "
            "with no error."
        ),
        short_description="dictConfig without disable_existing_loggers=False silently kills loggers",
        full_description=(
            "``logging.config.dictConfig()``'s ``disable_existing_loggers`` defaults to\n"
            "``True``, which silently disables all loggers created before the call.  This\n"
            'is the most common source of "why is my logging not working?".  Always set\n'
            '``"disable_existing_loggers": False``.  Applies to stdlib only.\n'
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l015",
    ),
    RuleDefinition(
        id="L016",
        canonical_reference=(
            "atlan-openapi-app app/run_dev.py — the dev entrypoint boots the SDK runtime "
            "and never calls logging.basicConfig(). The first caller wins and every later "
            "call is a silent no-op, which is why the SDK owns this exactly once."
        ),
        scope=RuleScope.BOTH,
        name="BasicConfigNoopAfterFirstCall",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-config",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "basicConfig() is silently ignored if the root logger already has handlers. "
            "Multiple calls rely on import order to decide which wins; the rest are silently "
            "dropped."
        ),
        short_description="Multiple basicConfig() calls — second+ are silent no-ops",
        full_description=(
            "``logging.basicConfig()`` is silently ignored if the root logger already\n"
            "has handlers.  Multiple calls across the codebase mean whichever runs first\n"
            "wins; the rest are dropped silently.  Flag any codebase with more than one\n"
            '``basicConfig()`` call outside ``if __name__ == "__main__":`` blocks.\n'
            "Applies to stdlib only.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l016",
    ),
    RuleDefinition(
        id="L017",
        canonical_reference=(
            "atlan-metabase-app app/handler.py — `test_auth` logs warning(..., "
            "exc_info=True). The level is chosen for the site and exc_info is explicit; "
            "logger.exception() would have pinned it to ERROR regardless."
        ),
        scope=RuleScope.BOTH,
        name="LoggerExceptionUsage",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-level",
        autofixable=True,
        since="0.4.0",
        rationale=(
            "logger.exception() is rejected outright. ADR-0011 restricts logging to four "
            "levels with exc_info=True as the sanctioned way to attach a traceback; "
            "logger.exception() implies a distinct level, reads sys.exc_info() implicitly "
            "(empty/stale outside an active except block), and overlaps the explicit "
            "exc_info rules. Use logger.error(..., exc_info=True) instead."
        ),
        short_description="logger.exception() used — use logger.error(..., exc_info=True) instead",
        full_description=(
            "``logger.exception()`` is not a sanctioned logging method in this project.\n"
            "ADR-0011 restricts app logging to four levels (DEBUG/INFO/WARNING/ERROR) and\n"
            "``exc_info=True`` is the canonical way to attach a traceback.  Beyond that,\n"
            "``logger.exception()`` reads ``sys.exc_info()`` implicitly — capturing\n"
            "nothing (or a stale exception) when called outside an active except block.\n"
            "Replace every call site with ``logger.error(..., exc_info=True)``.\n"
            "\n"
            "Checker note: the ``AtlanLoggerAdapter``'s own ``exception()`` shim is\n"
            "exempt — it exists only to satisfy third-party Temporal callers and\n"
            "immediately delegates to ``self.error(..., exc_info=True)``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l017",
    ),
    RuleDefinition(
        id="L018",
        canonical_reference=(
            "atlan-hello-world-app app/connector.py — `generate_greetings` passes its "
            "values as positional arguments to a %-style template, not as kwargs. Kwargs "
            "on an application log call do not reach the message a reader greps."
        ),
        scope=RuleScope.BOTH,
        name="KwargsInApplicationLogCalls",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "The adapter auto-injects Temporal context (workflow/run/activity IDs) as the "
            "only top-level indexed columns in ClickHouse/Grafana. App kwargs land in an "
            "unindexed JSON blob aggregation can't reach — context belongs in the message "
            "body via %-style."
        ),
        short_description="kwargs in application log calls — use %-style message body instead",
        full_description=(
            "Arbitrary kwargs in log calls are an anti-pattern in this project.\n"
            "Framework context (Temporal fields, correlation IDs) is auto-injected by\n"
            "the logging adapter; all other kwargs land in an unindexed JSON blob\n"
            "invisible in the log stream.  Embed context directly in the message body\n"
            "using %-style formatting.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l018",
    ),
    RuleDefinition(
        id="L019",
        canonical_reference=(
            "atlan-metabase-app app/handler.py — the module-level logger is used directly "
            "and no reference app calls logger.bind(). Workflow/run correlation is "
            "injected by the adaptor, so there is no bound logger to discard by accident."
        ),
        scope=RuleScope.BOTH,
        name="DiscardedBindResult",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-config",
        autofixable=False,
        since="0.4.0",
        rationale=(
            "structlog and loguru bind() returns a *new* logger with the bound context — "
            "the original is unchanged. A bare call (result not assigned) constructs the "
            "context and immediately discards it; the log call that follows has no extra "
            "context attached."
        ),
        short_description="logger.bind() result discarded — bind() returns a new logger",
        full_description=(
            "``structlog`` and ``loguru`` ``bind()`` returns a *new* bound logger;\n"
            "the original is unchanged.  A bare ``logger.bind(key=value)`` expression\n"
            "discards the result, so the context is never attached to any log call.\n"
            "Assign the result: ``log = logger.bind(key=value)``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l019",
    ),
    RuleDefinition(
        id="L020",
        canonical_reference=(
            "atlan-metabase-app pyproject.toml — LOG009 sits in the lint select list with "
            "the comment that names the replacement, so logger.warn() cannot reach main in "
            "that repo."
        ),
        scope=RuleScope.BOTH,
        name="DeprecatedLoggingWarn",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        since="0.4.0",
        rationale=(
            "logging.warn() is a long-deprecated alias for logging.warning(). It emits "
            "DeprecationWarning at import time in newer Python versions and will be removed. "
            "The fix is a trivial rename."
        ),
        short_description="logger.warn() is deprecated — use logger.warning() instead",
        full_description=(
            "``logger.warn()`` / ``logging.warn()`` is a deprecated alias for\n"
            "``logger.warning()`` that will be removed in a future Python version.\n"
            "Rename every call site to ``logger.warning(...)``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l020",
    ),
    RuleDefinition(
        id="L021",
        canonical_reference=(
            'atlan-openapi-app pyproject.toml — `extend-select = ["G001", "G003", '
            '"G004", "T201", "LOG009"]`, which is the exact set this rule looks for. '
            "atlan-metabase-app spells the same list one rule per line, with a comment on "
            "why G002 is deliberately absent."
        ),
        scope=RuleScope.BOTH,
        name="MissingLoggingLintRules",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-config",
        autofixable=True,
        since="0.4.0",
        rationale=(
            "The conformance suite catches logging anti-patterns at review time; ruff "
            "catches the same issues at edit time and in pre-commit. The two are "
            "complementary — ruff gives faster feedback in the IDE, conformance gives "
            "auditable SARIF output. Without the ruff rules enabled, engineers get no "
            "in-editor signal for L001/L005/L011/L020 equivalents."
        ),
        short_description=(
            "pyproject.toml ruff config is missing logging lint rules (G001, G003, "
            "G004, T201, LOG009)"
        ),
        full_description=(
            "The project's ``[tool.ruff.lint]`` ``select`` / ``extend-select`` must\n"
            "cover the following rules (or their category prefixes, or ``ALL``):\n"
            "\n"
            "* ``G001`` — ``logging.warn()`` deprecated (overlaps L020)\n"
            "* ``G003`` — string concatenation in log message (overlaps L011)\n"
            "* ``G004`` — f-string in log message (overlaps L001)\n"
            "* ``T201`` — ``print()`` statement (overlaps L005)\n"
            "* ``LOG009`` — ``logging.warn()`` deprecated (overlaps L020)\n"
            "\n"
            "A rule is covered if its full ID, any prefix (e.g. ``G`` covers all\n"
            "``G``-prefixed rules), or ``ALL`` appears in ``select`` or\n"
            "``extend-select`` and is not in ``ignore`` / ``extend-ignore``.\n"
            "\n"
            "Pin the five rules individually. Selecting the bare ``G`` category\n"
            "satisfies this check but also enables ``G201``, which demands\n"
            "``.exception(...)`` over ``.error(..., exc_info=True)`` — the exact\n"
            "inverse of conformance L017 (LoggerExceptionUsage). With ``G``\n"
            "selected, ruff and the conformance suite contradict each other on\n"
            "every except-block log call.\n"
            "\n"
            "Self-check exemption: ``pyproject.toml`` files whose\n"
            "``[project].name`` starts with ``atlan-application-sdk`` are skipped\n"
            "(the SDK's own tooling config is managed separately).\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l021",
    ),
)
