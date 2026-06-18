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
        scope=RuleScope.BOTH,
        name="FStringInLogMessage",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "%-style message bodies are the fleet-wide logging convention: one consistent "
            "call-site style keeps log statements legible and reviewable, and the SDK's "
            "loguru bridge renders the values in. f-strings render identically here — the "
            "%-bridge is eager — so this is about consistency and readability, not lazy "
            "evaluation; when a hot path genuinely needs deferred rendering, use "
            "opt(lazy=True) with {}-style."
        ),
        short_description="f-string in log message — breaks log grouping and aggregation",
        full_description=(
            "Using an f-string creates a unique message string per call, breaking log\n"
            "grouping and aggregation in Grafana/ClickHouse.  It also always evaluates\n"
            "eagerly.  Rewrite as %-style message body: embed context directly in the\n"
            "format string, do not move values to kwargs.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l001",
    ),
    RuleDefinition(
        id="L002",
        scope=RuleScope.BOTH,
        name="NonCanonicalLoggerFactory",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        since="0.2.0",
        rationale=(
            "The SDK adapter (application_sdk.observability.logger_adaptor.get_logger) "
            "is the only sanctioned way to obtain a logger. It injects Temporal context "
            "(workflow_id, run_id, activity_type) as top-level indexed columns in "
            "ClickHouse/Grafana and routes records through OTel. Direct use of "
            "logging.getLogger(), structlog.get_logger(), or loguru's logger bypasses "
            "all of this — correlation IDs are lost and records may not reach the "
            "observability store."
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
            "``AtlanLoggerAdapter`` or ``get_logger`` itself is skipped.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l002",
    ),
    RuleDefinition(
        id="L003",
        scope=RuleScope.BOTH,
        name="ExtraKwargsWrongFramework",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=False,
        since="0.2.0",
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
        scope=RuleScope.BOTH,
        name="ExceptBlockMissingExcInfoLog",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="missing-traceback",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "Same failure as E005 at the logging layer: the message appears in the stream "
            "but the stack trace is absent, so every postmortem hitting this pattern must "
            "reproduce the failure to find root cause."
        ),
        short_description="logger.warning/error in except block without exc_info=True",
        full_description=(
            "Logging an exception without ``exc_info=True`` produces a message with no\n"
            "stack trace — the root cause is invisible.  Add ``exc_info=True`` to all\n"
            "``logger.warning()`` / ``logger.error()`` calls within an except block.\n"
            "``.exception()`` is exempt.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l004",
    ),
    RuleDefinition(
        id="L005",
        scope=RuleScope.BOTH,
        name="PrintInProductionCode",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        since="0.2.0",
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
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l005",
    ),
    RuleDefinition(
        id="L006",
        scope=RuleScope.BOTH,
        name="InfoInTightLoop",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-level",
        autofixable=False,
        since="0.2.0",
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
        scope=RuleScope.BOTH,
        name="LoggerCriticalUsage",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-level",
        autofixable=True,
        since="0.2.0",
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
        scope=RuleScope.BOTH,
        name="UnguardedExpensiveDebug",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-performance",
        autofixable=False,
        since="0.2.0",
        rationale=(
            "Log-call arguments evaluate eagerly regardless of level. An unguarded expensive "
            "serialisation inside logger.debug() runs on every call in production — invisible "
            "CPU/memory overhead that compounds on hot paths."
        ),
        short_description="Expensive computation in logger.debug() argument — evaluates eagerly",
        full_description=(
            "Arguments to log calls are evaluated eagerly regardless of whether the\n"
            "level is enabled.  Guard expensive serialization / computation with\n"
            "``if logger.isEnabledFor(logging.DEBUG):``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l008",
    ),
    RuleDefinition(
        id="L009",
        scope=RuleScope.BOTH,
        name="WarnThenRaiseDuplication",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-noise",
        autofixable=False,
        since="0.2.0",
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
        scope=RuleScope.BOTH,
        name="CredentialInLogOutput",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="security",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "Log aggregation stores records in plaintext accessible to more people and "
            "systems than the credential store. A credential value in a log is a persistent "
            "exposure that survives rotation and is indexed for search."
        ),
        short_description="Credential/secret value in log output — security vulnerability",
        full_description=(
            "Credentials in log output are a security vulnerability — logs are often\n"
            "stored in plaintext in log aggregation systems, accessible to more people\n"
            "than the credential store.  Requires human security review before marking\n"
            "acceptable.  Logging a credential *name* is acceptable; logging a\n"
            "credential *value* is CRITICAL.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l010",
    ),
    RuleDefinition(
        id="L011",
        scope=RuleScope.BOTH,
        name="StringConcatenationInLog",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        since="0.2.0",
        rationale=(
            "Same convention as L001: string concatenation is an ad-hoc alternative to the "
            "standard %-style message body. It reads worse at the call site and breaks "
            "fleet-wide consistency for no benefit; rewrite as a %-style message body."
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
        scope=RuleScope.BOTH,
        name="StdlibExtraReservedKeyCollision",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-crash",
        autofixable=False,
        since="0.2.0",
        rationale=(
            "stdlib's Logger.makeRecord() raises KeyError when an extra={} key collides "
            "with a LogRecord attribute, propagating to the caller's logger.info() site and "
            "crashing it. The 22 forbidden keys include natural choices: name, message, "
            "module, args, filename."
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
        scope=RuleScope.BOTH,
        name="StdlibArbitraryKwargs",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-crash",
        autofixable=True,
        since="0.2.0",
        rationale=(
            "stdlib logger.info() raises TypeError immediately for any kwarg outside its "
            "short allowlist. The most common breakage when migrating from structlog (which "
            "accepts arbitrary kwargs) — call sites look identical but fail at runtime."
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
        scope=RuleScope.BOTH,
        name="StructlogEventKwargOverwrite",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=False,
        since="0.2.0",
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
        scope=RuleScope.BOTH,
        name="DictConfigDisableExistingLoggers",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-config",
        autofixable=True,
        since="0.2.0",
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
        scope=RuleScope.BOTH,
        name="BasicConfigNoopAfterFirstCall",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-config",
        autofixable=False,
        since="0.2.0",
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
        scope=RuleScope.BOTH,
        name="LoggerExceptionUsage",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-level",
        autofixable=True,
        since="0.2.0",
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
        scope=RuleScope.BOTH,
        name="KwargsInApplicationLogCalls",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=False,
        since="0.2.0",
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
        scope=RuleScope.BOTH,
        name="DiscardedBindResult",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-config",
        autofixable=False,
        since="0.3.0",
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
        scope=RuleScope.BOTH,
        name="DeprecatedLoggingWarn",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        since="0.3.0",
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
        scope=RuleScope.BOTH,
        name="MissingLoggingLintRules",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-config",
        autofixable=True,
        since="0.3.0",
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
            "Self-check exemption: ``pyproject.toml`` files whose\n"
            "``[project].name`` starts with ``atlan-application-sdk`` are skipped\n"
            "(the SDK's own tooling config is managed separately).\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l021",
    ),
)
