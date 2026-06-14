"""Error-handling rule definitions (E-series)."""

from __future__ import annotations

from suite.schema.catalog import RuleDefinition
from suite.schema.disposition import EnforcementTier, RuleMechanism

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="E001",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e001",
    ),
    RuleDefinition(
        id="E002",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e002",
    ),
    RuleDefinition(
        id="E003",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e003",
    ),
    RuleDefinition(
        id="E004",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e004",
    ),
    RuleDefinition(
        id="E005",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e005",
    ),
    RuleDefinition(
        id="E006",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e006",
    ),
    RuleDefinition(
        id="E007",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e007",
    ),
    RuleDefinition(
        id="E008",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e008",
    ),
    RuleDefinition(
        id="E009",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e009",
    ),
    RuleDefinition(
        id="E010",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e010",
    ),
    RuleDefinition(
        id="E012",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e012",
    ),
    RuleDefinition(
        id="E013",
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e013",
    ),
    RuleDefinition(
        id="E011",
        name="LoggingFilterUnsafeBody",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="filter-safety",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.17.0",
        short_description="logging.Filter.filter() body not wrapped in try/except — can crash caller",
        full_description=(
            "``Logger.handle()`` calls ``self.filter(record)`` with no surrounding\n"
            "try/except — unlike handler errors, filter exceptions are NOT caught by\n"
            "``handleError()``.  An unguarded attribute access\n"
            "(``record.custom_field``) or any external call that can raise will\n"
            "propagate directly to the code that called ``logger.info()`` (or similar),\n"
            "crashing the caller.  Wrap the entire ``filter()`` body in try/except with\n"
            "a safe fallback (return True to pass-through, False to drop).  Use\n"
            "``getattr(record, 'field', default)`` for optional record attributes.\n"
            "Never acceptable — filter methods must never let exceptions propagate.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e011",
    ),
    RuleDefinition(
        id="E014",
        name="ExceptLoopControlSwallow",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.17.0",
        short_description="except block exits loop silently (continue/break) without logging",
        full_description=(
            "An ``except`` block inside a loop whose body is only ``continue``,\n"
            "``break``, or ``pass`` — with no logging call — silently swallows the\n"
            "exception and resumes or exits the iteration.  This is the loop-body twin\n"
            "of P001/P002 (ruff S112 family).  At minimum log at DEBUG before the loop\n"
            "control statement; if the exception signals a genuine error, re-raise or\n"
            "log at WARNING/ERROR with ``exc_info=True``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e014",
    ),
    RuleDefinition(
        id="E015",
        name="ExceptionTextInErrorMessage",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="error-message-hygiene",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.17.0",
        short_description="Caught exception text interpolated into typed error message= — leaks unsanitised text",
        full_description=(
            "A typed ``AppError`` raise whose ``message=`` keyword value embeds the\n"
            "caught exception via an f-string (``f'…{exc}…'``), ``str(exc)``, or\n"
            "``repr(exc)`` — see typed-error-prescription.md §6.  This leaks\n"
            "unsanitised, potentially user-facing text from an upstream library into a\n"
            "field that is displayed to operators and indexed in dashboards.  It also\n"
            "breaks aggregation by collapsing distinct failure modes into one\n"
            "variable-text bucket.  Place the exception context in a typed evidence\n"
            "field (``cause=exc``, ``network_error=str(exc)``) and keep ``message=`` a\n"
            "stable human summary.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e015",
    ),
    RuleDefinition(
        id="E016",
        name="MissingExceptionChaining",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="exception-chaining",
        autofixable=True,
        orthogonal_gate="tests",
        since="3.17.0",
        short_description="raise inside except block missing 'from exc' cause — breaks exception chain",
        full_description=(
            "A non-bare ``raise`` inside an ``except … as e:`` block that does not\n"
            "include ``from e`` (or ``from None``).  Without explicit chaining, Python\n"
            "attaches the original as ``__context__`` with ``__suppress_context__=False``\n"
            "— the chain is preserved at the interpreter level but AE's wire serialiser\n"
            "only follows the ``__cause__`` chain, so the original exception is lost in\n"
            "dashboards.  Fix: ``raise NewError(...) from e``.  Use ``from None`` only\n"
            "when intentionally hiding the original (requires a comment explaining why).\n"
            "Acceptable patterns: bare ``raise`` (re-raise), ``raise X() from e`` (already\n"
            "chained), ``raise X() from None`` (intentional suppression).\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e016",
    ),
    RuleDefinition(
        id="E017",
        name="SecretNamedEvidenceKey",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="security",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.17.0",
        short_description="Error evidence kwarg ending in _secret/_password/_token — rejected by wire layer at runtime",
        full_description=(
            "An error construction call that passes a keyword argument whose name ends\n"
            "in ``_secret``, ``_password``, or ``_token`` — see\n"
            "``application_sdk.errors.wire`` §6.  The wire layer actively rejects these\n"
            "suffixes at runtime (``ValueError``) to prevent credential leakage into\n"
            "logs, dashboards, and SARIF reports.  Static detection means the bug is\n"
            "caught before any code runs.  Rename the evidence field to a safe key (e.g.\n"
            "``credential_name``, ``token_type``) or pass the value via ``cause=exc``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e017",
    ),
    RuleDefinition(
        id="E018",
        name="BareParentLeafRaise",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="untyped-raise",
        autofixable=False,
        orthogonal_gate="tests",
        since="3.17.0",
        short_description="Raising a bare AppError leaf class without a domain subclass overriding code",
        full_description=(
            "Raising a parent leaf directly (``InternalError(...)``,\n"
            "``InvalidInputError(...)``) without a domain-specific subclass that\n"
            "overrides ``code`` collapses all failure modes for a given category into\n"
            "one dashboard bucket — impossible to route, alert on, or triage\n"
            "individually.  See typed-error-prescription.md §4.  Only sanctioned\n"
            "bare-parent form: ``InternalError(classification_pending=True)`` — used\n"
            "exclusively as a temporary placeholder during migration, never in\n"
            "production-stable code.  For all other sites: define a domain subclass\n"
            "that overrides ``code`` with a specific constant\n"
            "(e.g. ``ENGINE_NOT_INITIALIZED``).\n"
            "Note: this rule is WARN tier because some bare-parent raises may be\n"
            "legitimate in small apps or during active migration.  Review findings\n"
            "before suppressing.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/schema-contract.md#e018",
    ),
)
