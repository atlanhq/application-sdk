"""Error-recovery rule definitions (P-series)."""

from __future__ import annotations

from suite.schema.catalog import RuleDefinition
from suite.schema.disposition import EnforcementTier, RuleMechanism

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P001",
        name="BareExceptPass",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="Bare 'except: pass' silently discards every exception",
        full_description=(
            "A bare ``except: pass`` catches KeyboardInterrupt, SystemExit, and\n"
            "GeneratorExit and discards them with no trace.  This is the hardest class\n"
            "of bugs to debug.  Replace with a typed catch that at minimum logs the\n"
            "error with ``exc_info=True``.  Never acceptable — even cleanup paths\n"
            "should log at DEBUG.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p001",
    ),
    RuleDefinition(
        id="P002",
        name="TypedExceptPass",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="Typed 'except SomeError: pass' discards exception silently",
        full_description=(
            "A typed catch that still discards silently loses the stack trace entirely.\n"
            "Acceptable only for truly trivial best-effort operations where failure is\n"
            "100% expected AND the surrounding code handles the missing result, AND\n"
            "there is a comment explaining the reasoning.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p002",
    ),
    RuleDefinition(
        id="P003",
        name="BroadContextlibSuppress",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="contextlib.suppress() — check whether scope is too broad",
        full_description=(
            "``contextlib.suppress(Exception)`` or ``suppress(BaseException)`` is HIGH\n"
            "severity; narrow ``suppress(FileNotFoundError)`` on a cleanup path is\n"
            "acceptable.  The checker must inspect the suppressed exception type before\n"
            "classifying.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p003",
    ),
    RuleDefinition(
        id="P004",
        name="BroadExceptClause",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="overly-broad-catch",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="Overly broad 'except Exception/BaseException' without exc_info",
        full_description=(
            "Catches everything but the specific type is unknown.  HIGH severity when\n"
            "not logged; MEDIUM when logged but missing ``exc_info=True``.  Acceptable\n"
            "only at top-level handlers (worker loops, HTTP handlers) when properly\n"
            "logged with ``exc_info=True``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p004",
    ),
    RuleDefinition(
        id="P005",
        name="ExceptBlockMissingExcInfo",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="missing-traceback",
        autofixable=True,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="except block logs without exc_info=True — stack trace discarded",
        full_description=(
            "The message is logged but the stack trace is lost.  Add ``exc_info=True``\n"
            "to every ``logger.warning()`` / ``logger.error()`` call inside an except\n"
            "block.  ``logger.exception()`` is exempt (it implies ``exc_info=True``).\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p005",
    ),
    RuleDefinition(
        id="P006",
        name="BareExceptWithBody",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="Bare 'except:' (no type) — catches SystemExit and KeyboardInterrupt",
        full_description=(
            "Like P001 but the block may have a body.  Still catches KeyboardInterrupt\n"
            "and SystemExit.  Always specify at least ``except Exception:``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p006",
    ),
    RuleDefinition(
        id="P007",
        name="ErrorToReturnValue",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="error-to-return-value",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="except block returns a value without logging — error hidden",
        full_description=(
            "Exception is converted to a return value (None, {}, [], False) with no\n"
            "trace.  Callers see a wrong result with no idea why.  At minimum log\n"
            "before returning; prefer raising a domain-specific exception instead.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p007",
    ),
    RuleDefinition(
        id="P008",
        name="ImportErrorWithoutLogging",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="optional-import",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="except ImportError without logging — environment issues hidden",
        full_description=(
            "Optional-dependency guard.  Acceptable when the import is genuinely\n"
            "optional AND the fallback path is correct AND there is a comment.  Log at\n"
            "DEBUG if the module is preferred but not required.  Flag if the module is\n"
            "expected to be present (will fail later with a confusing AttributeError).\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p008",
    ),
    RuleDefinition(
        id="P009",
        name="ExceptBlockOnlyAssigns",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="error-to-return-value",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="except block only assigns a variable — error hidden with no log",
        full_description=(
            "Exception sets a flag or default value with no trace.  Combines P007's\n"
            "error-hiding with no logging.  Add a ``logger.warning(..., exc_info=True)``\n"
            "before the assignment.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p009",
    ),
    RuleDefinition(
        id="P010",
        name="AsyncioGatherExceptionsUnexamined",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asyncio-unexamined",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="asyncio.gather(return_exceptions=True) results not checked for exceptions",
        full_description=(
            "``return_exceptions=True`` returns exception instances as values in the\n"
            "result list.  If the list is not subsequently inspected for ``Exception``\n"
            "instances, errors vanish silently.  The pattern is only a bug when results\n"
            "are not checked; ``return_exceptions=True`` itself is acceptable.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p010",
    ),
    RuleDefinition(
        id="P012",
        name="UntypedBuiltinRaise",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="untyped-raise",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="raise ValueError/RuntimeError/... where a typed AppError applies",
        full_description=(
            "SDK code raises a bare Python builtin.  The Automation Engine receives an\n"
            "opaque string — no category, code, audience, or retryable field.\n"
            "Dashboards are blind; on-call routing is impossible.  Use the typed error\n"
            "leaf from ``application_sdk.errors``.  Acceptable only inside dataclass\n"
            "``__post_init__`` / stdlib validator methods where Python semantics require\n"
            "``TypeError``/``ValueError`` for stdlib interoperability.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p012",
    ),
    RuleDefinition(
        id="P013",
        name="LegacyAtlanErrorRaise",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="legacy-raise",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.16.0",
        short_description="raise ClientError/ApiError/... (deprecated AtlanError stack)",
        full_description=(
            "``AtlanError`` and its subclasses emit a ``DeprecationWarning`` at\n"
            "construction time and reach AE as opaque strings.  They produce no typed\n"
            "wire envelope.  Scheduled for removal in v4.0.  Replace with the\n"
            "appropriate leaf from ``application_sdk.errors``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#p013",
    ),
)
