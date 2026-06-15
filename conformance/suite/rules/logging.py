"""Logging rule definitions (L-series)."""

from __future__ import annotations

from suite.schema.catalog import RuleDefinition
from suite.schema.disposition import EnforcementTier, RuleMechanism

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="L001",
        name="FStringInLogMessage",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        orthogonal_gate="tests",
        since="3.16.0",
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
        name="InconsistentLoggerFactory",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        since="3.16.0",
        short_description="Logger factory inconsistent with project canonical factory",
        full_description=(
            "Mixing logger factories produces inconsistent log formats, loses structured\n"
            "fields, and breaks log correlation.  The checker discovers the canonical\n"
            "factory (dominant by occurrence count) in Phase 1 and flags deviations.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l002",
    ),
    RuleDefinition(
        id="L003",
        name="ExtraKwargsWrongFramework",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=False,
        since="3.16.0",
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
        name="ExceptBlockMissingExcInfoLog",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="missing-traceback",
        autofixable=True,
        orthogonal_gate="tests",
        since="3.16.0",
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
        name="PrintInProductionCode",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        since="3.16.0",
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
        name="InfoInTightLoop",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-level",
        autofixable=False,
        since="3.16.0",
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
        name="LoggerCriticalUsage",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-level",
        autofixable=True,
        since="3.16.0",
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
        name="UnguardedExpensiveDebug",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-performance",
        autofixable=False,
        since="3.16.0",
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
        name="WarnThenRaiseDuplication",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-noise",
        autofixable=False,
        since="3.16.0",
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
        name="CredentialInLogOutput",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="security",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
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
        name="StringConcatenationInLog",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=True,
        since="3.16.0",
        short_description="String concatenation in log message — breaks log grouping",
        full_description=(
            "Like f-strings (L001), string concatenation embeds values into the message\n"
            "string in a way that breaks log grouping.  Rewrite as %-style message body.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l011",
    ),
    RuleDefinition(
        id="L012",
        name="PctStyleInNonStdlibLogger",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=False,
        since="3.16.0",
        short_description="%-style formatting in non-stdlib logger — silently produces wrong output",
        full_description=(
            "%-style is a stdlib feature.  In structlog, ``%s`` appears literally in\n"
            "output.  In loguru (without a bridge adapter), the positional arg is\n"
            "silently ignored.  Framework-dependent: ACCEPTABLE for stdlib, MEDIUM for\n"
            "structlog, LOW-to-HIGH for loguru depending on whether a bridge adapter is\n"
            "present.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l012",
    ),
    RuleDefinition(
        id="L013",
        name="StdlibExtraReservedKeyCollision",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-crash",
        autofixable=False,
        since="3.16.0",
        short_description="extra={} key collides with stdlib LogRecord attribute — crashes caller",
        full_description=(
            "stdlib's ``Logger.makeRecord()`` raises ``KeyError`` if any key in\n"
            "``extra={}`` matches a ``LogRecord`` attribute.  This crash propagates\n"
            "directly to the caller — NOT caught by ``handleError()``.  The 22 forbidden\n"
            "keys include: ``name``, ``message``, ``module``, ``args``, ``filename``,\n"
            "``process``, ``thread``.  Applies to stdlib only.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l013",
    ),
    RuleDefinition(
        id="L014",
        name="StdlibArbitraryKwargs",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="log-crash",
        autofixable=True,
        since="3.16.0",
        short_description="Arbitrary kwargs in stdlib logger — raises TypeError immediately",
        full_description=(
            "stdlib ``logger.info()`` only accepts ``exc_info``, ``extra``,\n"
            "``stack_info``, and ``stacklevel``.  Any other kwarg raises ``TypeError``\n"
            "and crashes the caller.  Very common when migrating from structlog/loguru.\n"
            "Applies to stdlib only.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l014",
    ),
    RuleDefinition(
        id="L015",
        name="StructlogEventKwargOverwrite",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=False,
        since="3.16.0",
        short_description="event= kwarg in structlog silently overwrites the log message",
        full_description=(
            "In structlog, the first positional argument is stored as the ``event`` key\n"
            "— it IS the log message.  Passing ``event=`` as a keyword argument silently\n"
            "overwrites the message with the domain value.  Rename the domain field to\n"
            "avoid collision.  Applies to structlog only.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l015",
    ),
    RuleDefinition(
        id="L016",
        name="DictConfigDisableExistingLoggers",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-config",
        autofixable=True,
        since="3.16.0",
        short_description="dictConfig without disable_existing_loggers=False silently kills loggers",
        full_description=(
            "``logging.config.dictConfig()``'s ``disable_existing_loggers`` defaults to\n"
            "``True``, which silently disables all loggers created before the call.  This\n"
            'is the most common source of "why is my logging not working?".  Always set\n'
            '``"disable_existing_loggers": False``.  Applies to stdlib only.\n'
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l016",
    ),
    RuleDefinition(
        id="L017",
        name="BasicConfigNoopAfterFirstCall",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-config",
        autofixable=False,
        since="3.16.0",
        short_description="Multiple basicConfig() calls — second+ are silent no-ops",
        full_description=(
            "``logging.basicConfig()`` is silently ignored if the root logger already\n"
            "has handlers.  Multiple calls across the codebase mean whichever runs first\n"
            "wins; the rest are dropped silently.  Flag any codebase with more than one\n"
            '``basicConfig()`` call outside ``if __name__ == "__main__":`` blocks.\n'
            "Applies to stdlib only.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l017",
    ),
    RuleDefinition(
        id="L018",
        name="LoggerExceptionOutsideExcept",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-level",
        autofixable=False,
        since="3.16.0",
        short_description="logger.exception() called outside an except block",
        full_description=(
            "``logger.exception()`` captures the current exception info via\n"
            "``sys.exc_info()``.  Called outside an except block, it captures nothing\n"
            "(or a stale exception from a previous except clause), producing a misleading\n"
            "traceback or no traceback.  Use ``logger.error(..., exc_info=True)`` only\n"
            "inside except blocks.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l018",
    ),
    RuleDefinition(
        id="L024",
        name="KwargsInApplicationLogCalls",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="log-format",
        autofixable=False,
        since="3.16.0",
        short_description="kwargs in application log calls — use %-style message body instead",
        full_description=(
            "Arbitrary kwargs in log calls are an anti-pattern in this project.\n"
            "Framework context (Temporal fields, correlation IDs) is auto-injected by\n"
            "the logging adapter; all other kwargs land in an unindexed JSON blob\n"
            "invisible in the log stream.  Embed context directly in the message body\n"
            "using %-style formatting.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/logging.md#l024",
    ),
)
