"""Error-handling rule definitions (E-series)."""

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
        id="E001",
        canonical_reference=(
            "atlan-metabase-app app/extracts/questions.py — "
            "`fetch_question_queries_single` catches broadly, logs with exc_info=True and "
            "appends a residual record before returning. Tolerating a failure and "
            "discarding it are different things."
        ),
        scope=RuleScope.BOTH,
        name="BareExceptPass",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "The hardest class of production bug to debug: no stack trace, no log record, "
            "no indication anything failed. Every downstream anomaly (wrong results, missing "
            "records) is diagnosed with no artifact pointing back to the origin. "
            "Customer impact: the failure surfaces as missing assets or wrong metadata in a "
            "tenant with nothing in the logs to trace it back — a one-line bug becomes a "
            "long-running customer escalation that support cannot root-cause."
        ),
        short_description="Bare 'except: pass' silently discards every exception",
        full_description=(
            "A bare ``except: pass`` catches KeyboardInterrupt, SystemExit, and\n"
            "GeneratorExit and discards them with no trace.  This is the hardest class\n"
            "of bugs to debug.  Replace with a typed catch that at minimum logs the\n"
            "error with ``exc_info=True``.  Never acceptable — even cleanup paths\n"
            "should log at DEBUG.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e001",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E002",
        canonical_reference=(
            "atlan-metabase-app app/api_types.py — `_to_millis` catches ValueError around "
            "timestamp parsing and logs the offending value with exc_info=True before "
            "returning None. Naming the exception type does not excuse an empty body."
        ),
        terminal_state=(
            "Control flow must not change — the swallow is existing, deliberate "
            "behaviour and the fix only stops the cause being discarded. Resolve the "
            "module's logger from its own source rather than assuming the name "
            "`logger`, and check it is bound ABOVE the handler: a bootstrap shim "
            "binds its logger below an import-time try/except, so logging there "
            "raises NameError inside the very block being made observable."
        ),
        scope=RuleScope.BOTH,
        name="TypedExceptPass",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "A typed catch that discards silently still destroys the event record. Stack "
            "traces at the point of failure are often the only artifact that survives async "
            "and service boundaries in a distributed system. "
            "Customer impact: a swallowed per-item failure ships an incomplete crawl to the "
            "tenant as a clean success — the customer discovers the gap in their catalog "
            "weeks later, and by then no artifact exists to explain which items were lost or why."
        ),
        short_description="Typed 'except SomeError: pass' discards exception silently",
        full_description=(
            "A typed catch that still discards silently loses the stack trace entirely.\n"
            "Acceptable only for truly trivial best-effort operations where failure is\n"
            "100% expected AND the surrounding code handles the missing result, AND\n"
            "there is a comment explaining the reasoning.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e002",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E003",
        canonical_reference=(
            "atlan-metabase-app app/residuals.py — `record_residual_failure`. No reference "
            "app uses contextlib.suppress; a deliberately tolerated failure is narrowed to "
            "one condition and written to residual/failures.jsonl, so the swallow leaves "
            "evidence a reviewer can find."
        ),
        scope=RuleScope.BOTH,
        name="BroadContextlibSuppress",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "suppress(Exception) is semantically identical to except Exception: pass — it "
            "absorbs every unexpected failure with no trace. A narrow suppress(FileNotFoundError) "
            "is safe; anything broader is a hidden failure sink."
        ),
        short_description="contextlib.suppress() — check whether scope is too broad",
        full_description=(
            "``contextlib.suppress(Exception)`` or ``suppress(BaseException)`` is HIGH\n"
            "severity; narrow ``suppress(FileNotFoundError)`` on a cleanup path is\n"
            "acceptable.  The checker must inspect the suppressed exception type before\n"
            "classifying.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e003",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E004",
        canonical_reference=(
            "atlan-openapi-app app/api_client.py — `_parse_zip` catches Exception per "
            "archive member and logs with exc_info=True. atlan-mysql-app "
            "app/handler.py shows the other two accepted shapes: `_check_connectivity` "
            "converts the caught exception into a typed PreflightCheck row and returns "
            "it, and `fetch_metadata` re-raises it chained as MetadataFetchError — no "
            "suppression needed at any of the three; an unexplained bare breadth is "
            "not accepted."
        ),
        scope=RuleScope.BOTH,
        name="BroadExceptClause",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="overly-broad-catch",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "Without exc_info, the stack trace is gone at the point of capture. A broad "
            "catch without a traceback also masks completely unexpected exceptions from "
            "upstream code, making root-cause analysis impossible."
        ),
        short_description="Overly broad 'except Exception/BaseException' without exc_info",
        full_description=(
            "Catches everything but the specific type is unknown.  HIGH severity when\n"
            "not logged; MEDIUM when logged but missing ``exc_info=True``.  Acceptable\n"
            "only at top-level handlers (worker loops, HTTP handlers) when properly\n"
            "logged with ``exc_info=True``.  A handler that unconditionally re-raises\n"
            "while preserving the cause is exempt — bare ``raise``, ``raise X(...)``,\n"
            "or ``raise X(...) from e`` — because nothing is swallowed; a ``raise\n"
            "X(...) from None`` that drops the cause, and a conditional re-raise that\n"
            "can fall through, still fire.\n"
            "\n\nExempt: handlers whose warning/error/critical log call formats the\n"
            "exception through a recognised redaction helper\n"
            "(redact*/sanitiz*/safe_traceback/safe_exception*/…) — the failure is\n"
            "logged at a\n"
            "deliberate no-traceback boundary.  A sanitized debug/info call does not\n"
            "qualify. A direct call to an undecorated same-module helper also counts\n"
            "when it receives the caught binding as a parameter and every control-flow\n"
            "path through the helper passes that parameter through a recognized sanitizer\n"
            "among the warning/error/critical call arguments. The helper call must be on\n"
            "a straight-line handler path; a prior branch\n"
            "that can bypass it does not prove the failure was logged.\n"
            "\n\nAlso exempt: ``raise X(...) from None`` whose raised error carries the\n"
            "caught exception through such a helper (directly, or via a local assigned\n"
            "from one), or hands it as ``cause=exc`` to an SDK error — a class imported\n"
            "from ``application_sdk.errors``, or an app class whose base chain reaches\n"
            "one — which the SDK renders on the wire through ``sanitize_cause_repr``.\n"
            "An app class merely taking a ``cause`` proves nothing.  Severing the chain is how a frame holding a resolved\n"
            "credential avoids emitting a raw traceback; severed-and-redacted preserves\n"
            "the cause, severed-and-dropped does not.  Without this the only way to\n"
            "clear E004 at such a site is a warning/error log, which is exactly what\n"
            "L009 forbids before a raise.\n"
            "\n\nAlso exempt: a handler that converts the caught exception into typed\n"
            "data and hands it back on *every* path — ``return\n"
            "PreflightCheck(passed=False,\n"
            "error=SourceUnavailableError(cause=exc).to_failure_details())``, or a row\n"
            "staged in a local that the enclosing function returns below the ``try``.\n"
            "The failure leaves the frame in inspectable form, so no log level decides\n"
            "whether it is visible; demanding one is what makes E004 and F005 jointly\n"
            "unsatisfiable at the last-resort arm of a preflight probe.  A ``return\n"
            "None``, a bare sentinel, a swallowing path before the typed return, or an\n"
            "opaque raw hand-off (``failed_check(name, exc, start)``) proves nothing\n"
            "about the type under a broad catch.  A same-module straight-line helper\n"
            "also qualifies, within limits the checker can prove:\n"
            "\n"
            "- the helper is an undecorated, synchronous, top-level function with one\n"
            "  final ``return``, no branching, and no ``*args``/``**kwargs``;\n"
            "- its returned value *is* a class-like constructor built from the\n"
            "  parameter the binding is passed to (``return Outcome(error=err)``), not\n"
            "  an expression that merely contains one (``(Outcome(...), None)[1]``);\n"
            "- the handler returns the helper's result directly, stages it in a local\n"
            "  that the return hands back, or passes it only into class-like\n"
            "  constructors or literal containers; a lowercase wrapper\n"
            "  (``discard(helper(exc))``) keeps it opaque;\n"
            "- the name still resolves to that helper: not rebound in any enclosing\n"
            "  function, not declared ``nonlocal`` or ``global``, and not reassigned\n"
            "  at module level.\n"
            "\n"
            "A helper that branches — a classifier walking the cause chain — is\n"
            "proven from its signature instead: its return annotation names an error\n"
            "class (``-> AppError``, ``-> AppError | None``, ``-> Optional[AppError]``)\n"
            "and the binding is passed to an exception-annotated parameter, it never\n"
            "falls off its end, and every ``return`` in it builds a class-like\n"
            "constructor from that parameter or a local still carrying it there (a\n"
            "local rebound to anything else stops carrying it).  When ``None`` is a\n"
            "possible result, the call must\n"
            "be assigned to a name that the very next statement exits on when it is\n"
            "``None``.\n"
            "\n"
            "``except Exception:`` with no ``as`` binding still fires.\n"
            "\n"
            "The binding itself is typed data too: ``return exc``, or a literal\n"
            "tuple/list holding it (``return (Result(ok=False), exc)``), while the\n"
            "handler never rebinds it.  A local staged through a conditional\n"
            "expression counts when one arm carries the failure and the other is a\n"
            "typed error — an error-class constructor, or a same-module helper that\n"
            "always returns one (``admin_denied(u) if denied(exc) else\n"
            "_check_error(exc)``); a ``None`` or opaque arm still fires.  The\n"
            "annotation route also accepts ``-> PreflightCheck`` / ``->\n"
            "PreflightOutput``, under the same every-return proof.  A class-like name\n"
            "may start with underscores (``_ProbeAnswer(...)``).\n"
            "\n\nAlso exempt — the failure is deferred, not swallowed: a handler that only\n"
            "stashes the exception (``error = exc`` or ``holder.append(exc)``, plus\n"
            "assignments and logs) when the function owning the stashed name — the\n"
            "handler's own, or the enclosing one for a worker-thread target writing a\n"
            "closure list or ``nonlocal`` — reaches ``raise NAME`` / ``raise\n"
            "NAME[i]`` after the ``try`` in its own top-level statements (bare, or\n"
            "under an ``if`` that reads NAME), with no ``return`` and no rebinding of\n"
            "NAME on the way.  E009 uses the same predicate.\n"
            "\n\nAlso exempt, each a shape where a WARNING would be wrong:\n"
            "\n"
            "- best-effort cleanup: the handler is only log calls, one at DEBUG/INFO\n"
            "  with a truthy ``exc_info``, and the ``try`` body is only release calls\n"
            "  (leading verb ``close``/``aclose``/``dispose``/``shutdown``/\n"
            "  ``disconnect``/``cleanup``/``release``/``terminate``/``stop``/``kill``/\n"
            "  ``unlink``/``rmtree``) or metric emits (a ``record_metric`` leaf, or a\n"
            "  ``get_metrics()`` receiver); or a lookup then release — single-name\n"
            "  assignments from a getter or attribute read guarded by ``if NAME`` /\n"
            "  ``if NAME is not None:`` around release calls, or ``result = close()``\n"
            '  then ``if hasattr(result, "__await__"): await result`` — where every\n'
            "  bound name only feeds its guard and at least one release runs; or\n"
            "  only call statements when the ``try`` sits in a ``finally``.  A WARNING\n"
            "  there lands beside the error actually being reported, and narrowing\n"
            "  would let a cleanup failure mask it;\n"
            "- a predicate-gated skip: one or more ``if <test reading the binding>:``\n"
            "  blocks (no ``else``) holding only log calls — one with ``exc_info`` —\n"
            "  and ending in ``return``/``continue``/``pass``, followed by a\n"
            "  cause-preserving re-raise on every other path.  Leading assignments\n"
            "  that only read the binding (``msg = str(exc)``,\n"
            "  ``kind = type(exc).__name__``) may precede them, and the test may read\n"
            "  those aliases instead;\n"
            "- inside F005's preflight scope (the functions F005's\n"
            "  ``reachable_preflight_sites`` inspects, followed across files in a\n"
            "  repo scan), a DEBUG/INFO log carrying the exception (``exc_info``, or\n"
            "  the binding formatted raw, via ``str``/``repr``, an f-string or a\n"
            "  sanitizer, or a local the handler assigned earlier from a sanitizer\n"
            "  applied to the binding and never rebound): F005 bars the WARNING E004\n"
            "  would otherwise demand;\n"
            "- outside it, a DEBUG/INFO log of the binding through a recognised\n"
            "  sanitizer (never raw) when the handler is only log calls and falls\n"
            "  through to a computed fallback: the statement after the ``try``\n"
            "  (climbing out of ``if`` blocks it ends) returns a call or\n"
            "  comprehension that is not an empty sentinel and reads nothing the\n"
            "  ``try`` body assigned (``return [d[0] for d in cursor.description]``).\n"
            "  A ``return None``, an empty literal or a pre-set result still fires;\n"
            "- an app durable failure record handed the binding — a callee whose\n"
            "  name holds ``failure``/``residual`` and a write verb (``write``,\n"
            "  ``record``, ``store``, ``persist``, ``save``) — directly or through a\n"
            '  local built from it (``record = {"message": str(e)}``), in a handler\n'
            "  of assignments, logs, assign-only ``for`` loops and ``if`` blocks whose\n"
            "  every branch records (or follows a record) and returns any value,\n"
            "  ending in a per-item ``return None`` or ``continue``, or in an empty\n"
            "  literal (``return []``) when a WARNING/ERROR also logged the binding\n"
            "  through a sanitizer;\n"
            "- a typed failure recorded as a preflight check row: after optional\n"
            "  constant assignments, the handler's only statement calls a nested\n"
            "  helper of the enclosing function, passing the binding wrapped in a\n"
            "  same-class method annotated to return an error class\n"
            "  (``error=self._typed_error(e, Denied)``), and the helper appends a\n"
            "  ``PreflightCheck``/``PreflightOutput`` built from that argument to an\n"
            "  enclosing list before anything can return or raise;\n"
            "- in a standalone script (L005's ``is_script_file``: a shebang or a\n"
            "  ``__main__`` guard), a ``print(...)`` of the caught exception.\n"
            "\n"
            "The sanitized-helper exemption also resolves a same-class method\n"
            "(``self._report(name, exc)``, static ones too), follows a helper call\n"
            "the handler returns (``return self._failure(check, exc)``, or an element\n"
            "of ``return None, self._failure(check, exc)``), lets the helper assign\n"
            "before it logs and read ``type(exc).__name__``, and counts\n"
            "``safe_exception_frames`` as a sanitizer.  A preceding ``if ...: raise\n"
            "<cause-preserving>`` guard does not count as a bypass, and neither does\n"
            "``if ...: raise helper(..., exc) from None`` when that helper is proven\n"
            "to log the binding through a sanitizer the same way.\n"
            "\n"
            "A same-class method (``self.X(exc)``, ``cls.X(exc)``, or\n"
            "``ClassName.X(exc)`` for a static or class method) whose return\n"
            "annotation is a bare class-like name, returned as the handler's only\n"
            "exit with the bare binding among its arguments, is typed data too\n"
            "(``return self.connectivity_verdict(exc)``).\n"
            "\n"
            "A ``raise NAME from None`` re-raises the failure when NAME is bound once,\n"
            "earlier in the handler, by ``NAME = f(..., exc, ...)`` receiving the bare\n"
            "binding (``typed = to_typed(exc)``): the converted error is built from\n"
            "it.\n"
            "\n\nThe exemptions are one principle: the exception must leave the\n"
            "frame in some inspectable form — re-raised with its trace, re-raised with a\n"
            "redacted cause, returned as typed data, deferred to a proven re-raise, or\n"
            "recorded where a WARNING is barred or would mislead."
            "\n\n**Fixing it well**\n\n"
            "* Before narrowing, read what the ``try`` body and its callees raise today, driver and bridge errors included (JDBC/JPype exceptions reach app code raw unless a client converts them). Narrow to those types and keep handling them the same way: the same classifier, the same check rows, the same log.\n\n"
            "* Never narrow to ``AppError`` at a catch that raw driver errors reach: they would escape and turn a reported failure into a crash.\n\n"
            "* Keep a shared helper a helper: change the helper's ``except``; do not inline it into each caller.\n\n"
            "* Tests keep asserting the old behaviour (a raw driver error still becomes a failed check); never change a fake so it stops raising what the real client raises.\n\n"
        ),
        rule_interactions=(
            "The set of forms that actually clear this rule is narrower than it "
            "looks, and two of the exits are closed by other rules. The checker "
            "accepts logger.exception(), or warning/error/critical carrying "
            "exc_info=True, or warning/error/critical routed through a redaction "
            "helper. logger.exception() is not available: L017 forbids it under "
            "ADR-0011. DEBUG is accepted only in the shapes that prove a WARNING "
            "would be wrong (FND-2542, decided): best-effort cleanup with "
            "exc_info=True, a predicate-gated skip that re-raises everything "
            "else, and anywhere inside F005's preflight scope when the log "
            "carries the exception. Elsewhere a DEBUG line, even with "
            "exc_info=True, still does not clear E004 while satisfying E005. "
            "Inside a preflight_check override and the helpers F005 follows from "
            "it (FND-2569), E004 reuses F005's own scope predicate, so the two "
            "rules agree: DEBUG/INFO carrying the exception clears E004 there, "
            "and WARNING is what F005 forbids. Never raise such a line to "
            "WARNING; logger.error through a redaction helper still clears both, "
            "and returning the failure as typed data remains the preferred "
            "shape for a probe arm."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e004",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E005",
        canonical_reference=(
            "atlan-metabase-app app/api_types.py — `_to_millis` logs an unparseable "
            "timestamp at WARNING with exc_info=True before returning None. The message "
            "says what failed; the traceback says where."
        ),
        scope=RuleScope.BOTH,
        name="ExceptBlockMissingExcInfo",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="missing-traceback",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "Tracebacks are the primary artifact of incident postmortems. A record that "
            "says something failed but carries no stack trace forces engineers to reproduce "
            "the failure — often impossible under production data volumes."
        ),
        rule_interactions=(
            "L009 at a pre-raise log: a warning/error within the three statements "
            "before a raise in the same block is also an L009 site (and usually "
            "L004). Enriching it with exc_info=True or safe_traceback(exc) clears "
            "E005 and L004 but not L009, whose fix then deletes the line. Apply "
            "L009's resolution instead: delete the line when the exception has an "
            "upstream record (which may leave a bare raise), otherwise downgrade it "
            "to a sanitized DEBUG through locals. Never reduce the handler to a bare "
            "raise without an upstream record: that drops the only record of the "
            "failure. That one edit clears E005, L004 and L009 together."
        ),
        short_description="except block logs without exc_info=True — stack trace discarded",
        full_description=(
            "The message is logged but the stack trace is lost.  Add ``exc_info=True``\n"
            "to every ``logger.warning()`` / ``logger.error()`` call inside an except\n"
            "block.  ``logger.exception()`` is exempt (it implies ``exc_info=True``).\n"
            "\n\nExempt: calls whose arguments flow through a recognised redaction\n"
            "helper\n"
            "(redact*/sanitiz*/safe_traceback/safe_exception*/scrub_secret*/mask_secret*)\n"
            "—\n"
            "these mark a deliberate no-traceback boundary where exc_info=True\n"
            "would serialize the raw exception past the sanitizer and can leak\n"
            "credentials (JDBC URLs, Authorization headers, OAuth bodies)."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e005",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E006",
        canonical_reference=(
            "atlan-openapi-app app/api_client.py — every handler in `validate_spec_url` "
            "and `_parse_zip` names a type. A bare `except:` appears nowhere in the three "
            "reference apps, so SystemExit and KeyboardInterrupt still unwind the worker."
        ),
        scope=RuleScope.BOTH,
        name="BareExceptWithBody",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "A bare except: absorbs KeyboardInterrupt and SystemExit even with a handler "
            "body. Process-termination signals are silently intercepted and the process may "
            "continue in an undefined state. "
            "Customer impact: a pod that swallows SIGTERM-driven SystemExit cannot drain "
            "cleanly, so every rolling restart or node upgrade in a tenant risks killing "
            "in-flight customer workflows mid-run instead of handing them off."
        ),
        short_description="Bare 'except:' (no type) — catches SystemExit and KeyboardInterrupt",
        full_description=(
            "Like P001 but the block may have a body.  Still catches KeyboardInterrupt\n"
            "and SystemExit.  Always specify at least ``except Exception:``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e006",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E007",
        canonical_reference=(
            "atlan-openapi-app app/api_client.py — `redact_url` catches the ValueError "
            "from urlsplit, logs it, and only then returns its '<unparseable url>' "
            "sentinel; the sentinel is the function's contract. It needs no "
            "suppression for two reasons, and both are required: the log before the "
            "return clears E007's no-log arm, and because the catch is narrow and the "
            "value is a non-empty placeholder, the logged-sentinel arm does not "
            "apply. Logging alone is not enough: under a broad catch "
            "(`except Exception`), logging before an empty return (None, [], {}) "
            "still fires E007 (FND-4080, which deliberately supersedes the "
            "log-then-sentinel reading in #4428). The one broad-catch exception is "
            "atlan-metabase-app app/extracts/questions.py: a per-item `return None` "
            "after `record_residual_failure(output_path, category, ...)`, which "
            "writes the failure to a durable residual trail rather than only the "
            "log. It is the credential-boundary "
            "form of the fix: the log is `logger.debug` through sanitize_cause_repr with "
            "no exc_info, because the url it guards may be a pre-signed secret held in "
            "that frame. Outside such a boundary, log with exc_info=True."
        ),
        terminal_state=(
            "A justified inline `# conformance: ignore[E007] <reason>` IS the correct "
            "end state where the sentinel genuinely IS the function's contract — the "
            "caller is documented to treat the empty/None return as a normal outcome "
            "rather than as success. The reason must say which contract, as "
            "atlan-openapi-app `redact_url` does. An optional cache whose misses fall "
            "back to another lookup is such a contract: suppress it in the app with "
            "the contract named; the detector does not try to prove it. Where the "
            "sentinel instead stands in "
            "for a failure the caller cannot distinguish from success, the directive "
            "is unremediated: either raise, or record the failure to a durable "
            "evidence trail and declare the gap (see E020). A per-item skip inside a "
            "batch whose empty aggregate (`{}`, `[]`) tests assert is such a contract "
            "when no clearing shape keeps it: suppress it with the per-item skip "
            "contract named rather than raising, which would fail the whole run."
        ),
        rule_interactions=(
            "E007 and E004 judge the same handler shape with one shared predicate "
            "(typed_failure_scope in checks/error_handling/_helpers.py). A return "
            "that hands the caught exception back as typed data already clears "
            "both rules: a call that receives the binding wrapped in a typed "
            "error (`AuthRejectedError(cause=exc)`), including inside a tuple, "
            "or, under a narrow catch, a call that receives the binding "
            "directly (`self._failed(name, started, exc)`). Adding a log there "
            "is a wrong edit, and inside a preflight_check override a "
            "warning/error log trades the E007 for an F005. E007 applies the "
            "predicate per return and E004 applies it to every exit. Bare "
            "sentinels and stringified exceptions (`str(exc)`, `repr(exc)`, an "
            "f-string or `.format(exc)`) still fire, because a string is the "
            "failure laundered into a plain value. Found by a consumer app's "
            "preflight probe arms in FND-2493. Under a broad catch, a log before an "
            "empty-sentinel return does not clear E007 (FND-4080): there the fix is "
            "to raise or return typed data, never to add or move a log line. Only a "
            "per-item `return None` after the imported `residuals."
            "record_residual_failure` clears it (FND-4199). E004 "
            "still applies to the same handler independently, so its exc_info "
            "requirement holds whichever way the E007 is fixed. E020 is the "
            "response-guard variant of the same advice and shares its per-object "
            "caveat: inside a batch or per-item loop, raising where one object was "
            "skipped turns that object's failure into a run-wide one. Keep the "
            "per-item skip (record it with record_residual_failure, or catch the "
            "typed error at the loop), and keep tests that assert the skip passing."
        ),
        scope=RuleScope.BOTH,
        name="ErrorToReturnValue",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="error-to-return-value",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "Converting an exception to a falsy return value (None, [], False) shifts the "
            "failure point: the caller sees a plausible empty result and fails later, often "
            "somewhere unrelated, making the original failure invisible."
        ),
        short_description="except block returns a value that hides the error",
        full_description=(
            "Exception is converted to a return value (None, {}, [], False) with no\n"
            "trace.  Callers see a wrong result with no idea why.  At minimum log\n"
            "before returning; prefer raising a domain-specific exception instead.\n"
            "\n"
            "A return that hands the caught exception back as typed data is not\n"
            "flagged: the failure leaves the frame for the caller to report. This\n"
            "is the same typed-failure predicate E004 uses, so ``return exc`` and a\n"
            "tuple/list holding the binding count too.\n"
            "\n"
            "Also not flagged: under a narrow catch, a return of a same-module\n"
            "helper proven to return a typed result (a class-like return\n"
            "annotation, or a class-like constructor on every path), such as\n"
            "``return _finish(log, Outcome.SKIPPED, ...)`` — an inline constructor\n"
            "still needs a log; a per-item ``return None`` after an app durable\n"
            "failure record handed the binding (``write_failure``,\n"
            "``record_failure``; a name holding ``failure``/``residual`` and a write\n"
            "verb); and a ``return <non-zero int>`` after a top-level\n"
            "``print(..., file=sys.stderr)`` or ``sys.stderr.write(...)`` in the\n"
            "same handler, a CLI surfacing the failure in its exit status.\n"
            "``return 0`` / ``None`` / ``True`` still fire.  A same-class helper\n"
            "that logs the binding counts whatever its parameter is named, static\n"
            "methods included, and so does a helper the return itself calls\n"
            "(``return self._failure(check, exc)``, ``return None,\n"
            "self._failure(check, exc)``).  Under any catch, a same-class method\n"
            "(``self.X`` / ``cls.X`` / ``ClassName.X``, static ones included) whose\n"
            "return annotation is a bare class-like name, called with the bare\n"
            "binding (``return self.connectivity_verdict(exc)``), is typed data.\n"
            "The durable-record arm follows a local built from the binding\n"
            '(``record = {"message": str(e)}``; ``write_failure(..., record)``),\n'
            "and accepts an empty literal (``return []``) when a WARNING/ERROR also\n"
            "logged the binding through a sanitizer before it.\n"
            "\n"
            "Under a broad catch (`Exception`, `BaseException`, bare `except:`)\n"
            "logging first does not clear it: `logger.<level>(...); return None`\n"
            '(or `[]`, `{}`, `()`, `""`, `dict()` ...) leaves the trace in the log\n'
            "only, and the caller still cannot tell the failure from a genuine\n"
            "empty result. Raise a domain error or return the failure as typed\n"
            "data. A per-item `return None` after an imported\n"
            "`residuals.record_residual_failure(output_path, category, ...)` call is\n"
            "not flagged: the failure is recorded as durable data. A narrow catch,\n"
            "a bare `return`, a `return None` inside a\n"
            "`-> None` function, and any file in a wholesale-deprecated module (one\n"
            "whose own top level, or an enclosing package `__init__.py`, emits a\n"
            "module-level `DeprecationWarning`) are left out of this arm.\n"
            "\n"
            "Per-object skips.  Inside a batch or per-item loop, replacing one\n"
            "item's empty return with a raise is a behaviour change: one bad item\n"
            "then fails the whole run.  It is never the default fix.  Keep the skip\n"
            "and make the failure durable instead (the per-item\n"
            "`record_residual_failure` shape above), or raise the typed error and\n"
            "catch it at the loop.  Tests that assert the skip keep passing\n"
            "unchanged.  When the per-item value is an empty aggregate (`{}`, `[]`)\n"
            "and no clearing shape keeps the tested skip, the end state is a\n"
            "justified `# conformance: ignore[E007] <per-item skip contract>` or a\n"
            "judgment residue, never a raise.  E020 gives the same advice for HTTP\n"
            "response guards.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e007",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E008",
        canonical_reference=(
            "application_sdk/clients/ssl_utils.py — `_get_default_ca_bundle_path` catches the "
            "ImportError for the optional certifi dependency and logs that it is falling "
            "back to the system CA paths before continuing, so the degraded path leaves a "
            "trace. None of the three reference apps has an `except ImportError` in the "
            "code E008 scans (app/ and main.py; tests/ is excluded), so the SDK is the "
            "only real compliant site."
        ),
        scope=RuleScope.BOTH,
        name="ImportErrorWithoutLogging",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="optional-import",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "Silent optional-import guards mask environment misconfigurations. If a "
            "preferred module is unexpectedly absent, the fallback runs and produces subtly "
            "wrong results with no signal in the observability stack."
        ),
        short_description="except ImportError without logging — environment issues hidden",
        full_description=(
            "Optional-dependency guard.  Acceptable when the import is genuinely\n"
            "optional AND the fallback path is correct AND there is a comment.  Log at\n"
            "DEBUG if the module is preferred but not required.  Flag if the module is\n"
            "expected to be present (will fail later with a confusing AttributeError).\n"
            "A cause-preserving re-raise on every path (including a typed error raised\n"
            "from the ImportError) surfaces the failure and does not need a duplicate log.\n"
            "An explanatory comment on the ``except`` line or inside the handler is\n"
            "honoured as this text says (tool directives — ``noqa``, ``type: ignore``,\n"
            "``conformance: ignore`` — do not count), and so is a reporter call that\n"
            "states the problem with a message (a callee named for error/warn/report/\n"
            'fail/fatal/abort/die with a string argument: ``_error(path, "...\n'
            'missing")``).\n'
            "\n**Fixing it well**\n\n"
            "* Fix it at the existing ``except ImportError`` site, in the shape this rule describes: "
            "an optional module the code falls back from is logged at ``DEBUG`` with the module "
            "name; a required one is re-raised with its cause on every path "
            "(``raise ... from exc``), which needs no log of its own.\n\n"
            "* Keep the import where it is: moving it to module level or into a function changes when the app loads its drivers, and the repo's ruff rules then reject the new shape.\n\n"
            "* Never add ``noqa`` to get past ruff; if ruff objects, the shape is wrong — log at the original site.\n\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e008",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E009",
        canonical_reference=(
            "atlan-metabase-app app/credentials.py — `build_credential_ref` binds the "
            "routing error, logs it, and then takes the inline-credentials path. A bound "
            "name that is never read is the tell that the handler is a placeholder."
        ),
        scope=RuleScope.BOTH,
        name="ExceptBlockOnlyAssigns",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="error-to-return-value",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "The exception sets a flag or default and the event record is destroyed. "
            "Callers see apparently-normal behaviour until they discover the silent fallback "
            "later — typically under production conditions where it produces wrong results "
            "at scale."
        ),
        short_description="except block only assigns a variable — error hidden with no log",
        full_description=(
            "Exception sets a flag or default value with no trace.  Combines P007's\n"
            "error-hiding with no logging.  Add a ``logger.warning(..., exc_info=True)``\n"
            "before the assignment.\n"
            "\n"
            "Not flagged: a stash the owning function provably re-raises\n"
            "(``error = exc`` ... ``if error is not None: raise error``), the same\n"
            "predicate E004 uses — the failure is deferred, not hidden.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e009",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E010",
        canonical_reference=(
            "No reference app calls asyncio.gather(return_exceptions=True); per-item "
            "failure is decided at the item, as in atlan-metabase-app "
            "app/extracts/dashboards.py, where `fetch_dashboards_details` fetches one "
            "dashboard at a time and `fetch_dashboard_details` logs each failed fetch "
            "and records it as a residual. Where an app genuinely needs concurrency, the "
            "app-facing seam is application_sdk/execution/heartbeat.py — run_in_thread / "
            "run_fault_isolated / run_best_effort, which surface per-unit failures for you "
            "(`_runtime.offload` is the SDK-internal path; importing it from an app is "
            "what P005 exists to catch)."
        ),
        scope=RuleScope.BOTH,
        name="AsyncioGatherExceptionsUnexamined",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="asyncio-unexamined",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "gather(return_exceptions=True) turns exceptions into values, "
            "indistinguishable from normal results in the returned list. Every failed "
            "sub-task silently disappears unless the caller checks each result for "
            "Exception."
        ),
        short_description="asyncio.gather(return_exceptions=True) results not checked for exceptions",
        full_description=(
            "``return_exceptions=True`` returns exception instances as values in the\n"
            "result list.  If the list is not subsequently inspected for ``Exception``\n"
            "instances, errors vanish silently.  The pattern is only a bug when results\n"
            "are not checked; ``return_exceptions=True`` itself is acceptable.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e010",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E011",
        canonical_reference=(
            "No reference app writes a logging.Filter. Filtering and Temporal-context "
            "enrichment belong to application_sdk/observability/logger_adaptor.py, reached "
            "through `get_logger`, and redaction to application_sdk/errors/base.py "
            "(`sanitize_cause_repr` / `safe_traceback`); atlan-mysql-app app/client.py "
            "shows the whole of an app's logging setup — one import and one module-level "
            "logger."
        ),
        scope=RuleScope.BOTH,
        name="LoggingFilterUnsafeBody",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="filter-safety",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "logging.Filter.filter() exceptions propagate to the code that called "
            "logger.info() — not to handleError() like handler exceptions. An unguarded "
            "filter body is a production crash vector hidden inside observability infra."
        ),
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e011",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E012",
        canonical_reference=(
            "atlan-mysql-app app/failures.py — every leaf subclasses an SDK category "
            "(e.g. `InvalidInputError`, `AuthError`, `SourceUnavailableError`) and owns "
            "a `code`. Use one of these for failures that leave the error boundary; a "
            "builtin used only as a sentinel inside a matching typed-translation handler "
            "does not escape as the outward failure."
        ),
        scope=RuleScope.BOTH,
        name="UntypedBuiltinRaise",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="untyped-raise",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "The Automation Engine receives a typed error envelope (category, audience, "
            "retryable, code). A bare ValueError delivers an opaque string with none of "
            "these — dashboards are blind, on-call routing can't branch on it, SLA gates "
            "can't classify it. (per ADR-0013)"
        ),
        short_description="raise ValueError/RuntimeError/... where a typed AppError applies",
        full_description=(
            "SDK code that lets a bare Python builtin reach the Automation Engine sends an\n"
            "opaque string — no category, code, audience, or retryable field.\n"
            "Dashboards are blind; on-call routing is impossible.  Use the typed error\n"
            "leaf from ``application_sdk.errors``.  A builtin used as an internal\n"
            "validation sentinel is acceptable only when a matching ``except`` handler\n"
            "unconditionally translates it into a typed ``AppError`` before it leaves\n"
            "that boundary; swallowing, conditional handling, and re-raising do not qualify.\n"
            "Builtin raises are also acceptable inside dataclass ``__post_init__`` /\n"
            "stdlib validator methods where Python semantics require ``TypeError`` or\n"
            "``ValueError`` for stdlib interoperability.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e012",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E013",
        canonical_reference=(
            "atlan-metabase-app app/errors.py — every error is imported from "
            "`application_sdk.errors`. No app/ module in the three reference apps raises "
            "or imports the deprecated AtlanError stack (ClientError, ApiError, …)."
        ),
        scope=RuleScope.BOTH,
        name="LegacyAtlanErrorRaise",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="legacy-raise",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "AtlanError subclasses produce no typed wire envelope — they reach AE as opaque "
            "strings and emit DeprecationWarning at construction. Every new raise site "
            "deepens the migration debt and blocks the v4.0 removal. "
            "Customer impact: when the run fails in a tenant, the customer sees a generic "
            "unclassified error with no category or suggested action, and on-call cannot "
            "route the incident by failure type — every occurrence needs a human to read the "
            "raw string."
        ),
        short_description="raise ClientError/ApiError/... (deprecated AtlanError stack)",
        full_description=(
            "``AtlanError`` and its subclasses emit a ``DeprecationWarning`` at\n"
            "construction time and reach AE as opaque strings.  They produce no typed\n"
            "wire envelope.  Scheduled for removal in v4.0.  Replace with the\n"
            "appropriate leaf from ``application_sdk.errors``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e013",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E014",
        canonical_reference=(
            "atlan-metabase-app app/utils.py — the expected ValueError for a candidate "
            "date-format mismatch is logged at DEBUG without exc_info before continuing. "
            "For an unparseable QI line, app/lineage/qi_reader.py's `iter_qi_records` "
            "logs at WARNING with exc_info=True before continuing. Reserve tracebacks for "
            "genuine unexpected failures, not routine format probes."
        ),
        scope=RuleScope.BOTH,
        name="ExceptLoopControlSwallow",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="silent-swallow",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "A loop that silently absorbs per-item exceptions can complete with a "
            "full-looking result set that silently omits items. Silent partial failure is "
            "harder to detect than an outright crash — callers may act on the wrong result "
            "for a long time."
        ),
        short_description="except block exits loop silently (continue/break) without logging",
        full_description=(
            "An ``except`` block inside a loop whose body is only ``continue``,\n"
            "``break``, or ``pass`` — with no logging call — silently swallows the\n"
            "exception and resumes or exits the iteration.  This is the loop-body twin\n"
            "of P001/P002 (ruff S112 family).  Explain a tolerated skip before the loop\n"
            "control statement.  For an expected format/probe mismatch, use a concise\n"
            "DEBUG diagnostic without ``exc_info=True``.  If the exception signals a\n"
            "genuine unexpected error, re-raise or log at WARNING/ERROR with\n"
            "``exc_info=True``.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e014",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E015",
        canonical_reference=(
            "atlan-mysql-app app/client.py — `get_iam_role_token` raises "
            "IamTokenGenerationError with a fixed operator-facing message and passes the "
            "original as `cause=e`. The caught exception's text never lands in `message=`."
        ),
        scope=RuleScope.BOTH,
        name="ExceptionTextInErrorMessage",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="error-message-hygiene",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "Embedding str(exc) in message= produces a unique string per failure instance — "
            "each path/value becomes a separate dashboard bucket instead of one countable "
            "signal. It also leaks unsanitised upstream text into a field shown to operators "
            "and indexed by aggregation."
        ),
        short_description="Caught exception text interpolated into typed error message= — leaks unsanitised text",
        full_description=(
            "A typed ``AppError`` raise whose ``message=`` keyword value embeds the\n"
            "caught exception via an f-string (``f'…{exc}…'``), ``str(exc)``,\n"
            "``repr(exc)``, or string concatenation (``'…: ' + str(exc)``) — see\n"
            "typed-error-prescription.md §6.  This leaks\n"
            "unsanitised, potentially user-facing text from an upstream library into a\n"
            "field that is displayed to operators and indexed in dashboards.  It also\n"
            "breaks aggregation by collapsing distinct failure modes into one\n"
            "variable-text bucket.  Place the exception context in a typed evidence\n"
            "field (``cause=exc``, ``network_error=str(exc)``) and keep ``message=`` a\n"
            "stable human summary.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e015",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E016",
        canonical_reference=(
            "atlan-mysql-app app/handler.py — `fetch_metadata` ends `raise "
            "MetadataFetchError(cause=e) from e`. Both halves are there: the SDK-visible "
            "cause and the Python chain."
        ),
        scope=RuleScope.BOTH,
        name="MissingExceptionChaining",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="exception-chaining",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "When the Automation Engine rebuilds the typed wire envelope it walks the "
            ".cause/__cause__ chain, not __context__. A replacing raise that neither "
            "explicitly chains the caught exception nor carries it as the SDK error's cause "
            "leaves the original attached only as __context__; it is visible at the "
            "interpreter level but dropped from the wire envelope and every downstream "
            "system that reads it."
        ),
        short_description=(
            "raise inside except block missing an explicit or SDK-visible cause — breaks "
            "exception chaining"
        ),
        full_description=(
            "A non-bare ``raise`` inside an ``except … as e:`` block that neither\n"
            "includes ``from e`` (or ``from None``) nor carries ``e`` as a proven SDK\n"
            "error's cause.  Without either, Python attaches the original only as\n"
            "``__context__`` — preserved at the interpreter level, but AE's wire\n"
            "serialiser follows the ``__cause__``/``.cause`` chain, so the original\n"
            "exception is lost in dashboards.  Fix: ``raise NewError(...) from e``, and\n"
            "for an SDK ``AppError`` also pass ``cause=e``.  Use ``from None`` only when\n"
            "intentionally hiding the original (requires a comment explaining why).\n"
            "Acceptable patterns: bare ``raise`` (re-raise); ``raise X() from e``\n"
            "(already chained); ``raise X() from None`` (intentional suppression);\n"
            "``raise SdkError(..., cause=e)`` where ``SdkError`` is proven to come from\n"
            "``application_sdk.errors``, is not rebound or shadowed, and the call has no\n"
            "``*args``/``**kwargs`` unpacking; ``raise classify(e)`` where ``classify``\n"
            "is an unshadowed top-level function in this module or imported from the\n"
            "scanned repo whose every return either passes through ``e`` already proven\n"
            "to be an SDK error or builds an SDK error with ``cause=e``.  A cause the\n"
            "rule cannot prove (``cause=str(e)``, an app-defined class, an opaque\n"
            "helper) still needs ``from e``.  ``raise group.exceptions[i]`` inside\n"
            "``except BaseExceptionGroup as group`` (or ``except*``) is accepted: the\n"
            "member keeps its own ``__cause__``, which ``from group`` would overwrite.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e016",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E017",
        canonical_reference=(
            "atlan-openapi-app app/connector.py — `download_cloud_spec` raises with "
            "service / retryable / suggested_action as its evidence. Evidence describes "
            "the failure, never the credential that produced it; "
            "application_sdk/errors/wire.py rejects secret-named keys at runtime."
        ),
        scope=RuleScope.BOTH,
        name="SecretNamedEvidenceKey",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="security",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "Evidence fields with secret-bearing suffixes are rejected by the wire layer at "
            "runtime. Static detection catches the pattern before any code runs, eliminating "
            "the window between deploy and first invocation where live credentials could be "
            "serialised into logs, dashboards, or SARIF. "
            "Customer impact: the runtime rejection fires exactly when a customer run is "
            "already failing, replacing the real error with an opaque secondary crash — and "
            "the near-miss it guards is the customer's live credential landing in log "
            "storage, which is a security incident, not a bug."
        ),
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e017",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E018",
        canonical_reference=(
            "atlan-openapi-app app/errors.py — nineteen connector-specific subclasses, "
            "each with its own `code`; app/connector.py `download_cloud_spec` raises "
            "`TenantObjectStoreUnavailableError` rather than the bare "
            "DependencyUnavailableError leaf, so failures bucket per connector on the "
            "dashboard. The one bare-leaf raise, in `run`, is the sanctioned "
            "InternalError(classification_pending=True) placeholder."
        ),
        scope=RuleScope.BOTH,
        name="BareParentLeafRaise",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="untyped-raise",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.2.0",
        rationale=(
            "Each categorical leaf is a dashboard bucket. Raising the parent directly "
            "collapses all domain failure modes into one bucket — impossible to count "
            "separately, route to the right rotation, or alert on per failure mode."
        ),
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
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e018",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E019",
        canonical_reference=(
            "atlan-mysql-app app/handler.py — `preflight_check`'s probes classify the "
            "caught exception into a typed error and return it on the check's `error=`, "
            "so the rendered message is the error's authored text, never the exception's. "
            "For `test_auth`, return the same typed error as "
            "`AuthOutput(status=FAILED, error=err.to_failure_details())`, as mysql's "
            "own `test_auth` does from atlan-mysql-app#729."
        ),
        scope=RuleScope.BOTH,
        name="ExceptionTextInContractField",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="error-message-hygiene",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.9.0",
        rationale=(
            "E015 stops exception text leaking through a raised AppError's message=, but the "
            "same text leaks just as readily when an except block returns a typed response "
            "contract (AuthOutput, PreflightCheck) with message=str(exc). The returned value "
            "crosses the typed boundary to operators and dashboards, and each distinct str(exc) "
            "becomes its own aggregation bucket instead of one countable signal."
        ),
        short_description="Caught exception text interpolated into a returned contract message= field — leaks unsanitised text",
        full_description=(
            "Inside an ``except … as exc:`` block, a call (typically a typed\n"
            "response/output contract such as ``AuthOutput`` or ``PreflightCheck``)\n"
            "is constructed with a ``message=`` keyword that embeds the caught\n"
            "exception — whether that call is returned\n"
            "(``return AuthOutput(message=str(exc))``), appended/collected for a later\n"
            "return (``checks.append(PreflightCheck(message=f'…{exc}'))``), or simply\n"
            "assigned.  The interpolation may be an f-string (``f'…{exc}…'``),\n"
            "``str(exc)``, ``repr(exc)``, or string concatenation (``'…: ' + str(exc)``).\n"
            "This is the non-``raise`` counterpart of E015: the\n"
            "unsanitised upstream text still crosses the typed boundary into a field\n"
            "shown to operators and indexed in dashboards, and still collapses distinct\n"
            "failure modes into one variable-text bucket.  Classify the exception into\n"
            "the app's typed ``AppError`` and return it as\n"
            "``error=err.to_failure_details()`` on the contract (``AuthOutput.error``\n"
            "/ ``PreflightCheck.error``, typed ``FailureDetails | None``).  A failed\n"
            "result renders ``error.message``, so no separate ``message=`` is needed:\n"
            "the reason stays visible as authored text, one\n"
            "bucket per failure mode.  A fixed string also clears the rule but throws\n"
            "away the reason the caller needs, so it is not the default fix.\n"
            "\n"
            "Detection scope mirrors E015 exactly (they share one matcher): it covers\n"
            "f-string, ``str(exc)``, ``repr(exc)`` and string-concatenation\n"
            "(``'…' + str(exc)``) interpolation of the except binding, but not a bare\n"
            "``message=exc`` or attribute access such as ``message=exc.args[0]``.\n"
            "Extending both rules to those shapes is a deliberate future follow-up kept\n"
            "symmetric across E015/E019.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e019",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E020",
        canonical_reference=(
            "atlan-metabase-app app/extracts/responses.py — `json_or_raise` raises the "
            "typed MetabaseSourceUnavailableError (endpoint=, http_status=) on a failed "
            "response. Where one failure must not abort the crawl, the extract function "
            "catches that typed error: app/extracts/databases.py "
            "`fetch_databases_summaries` logs it with exc_info=True, records a residual "
            "and returns [], and the run declares the gap as PARTIAL_SUCCESS. No site "
            "needs a suppression."
        ),
        terminal_state=(
            "The failed response raises a typed AppError at the guard. That is the whole "
            "fix where the failure of the whole request, or a transient outcome, should "
            "abort the run. A permanent per-object rejection inside a batch (HTTP "
            "400/404/410 on one object's detail fetch) is a different end state: a "
            "warning that carries the status and the source's error reason (never the "
            "body), then the skip, and the batch continues. The detector does not report "
            "that shape, and turning it into a raise is a behaviour change, never the "
            "default fix. Where the app must degrade "
            "instead, the end state keeps the raise and adds an explicit `except "
            "<ThatTypedError>` at the tolerating function. That handler logs with "
            "exc_info=True, records the failure to a durable evidence trail (a residual "
            "file), returns the empty sentinel, and the run declares the gap (e.g. "
            "OutputStatus.PARTIAL_SUCCESS) rather than reporting a complete crawl. Do "
            "NOT apply a raise-only edit to a site that already records and declares "
            "its gap: that deletes the app's ability to degrade, so a single flaky "
            "endpoint aborts the entire crawl. Convert it to the typed catch instead. "
            "An inline ignore[E020] is not the end state, because the typed catch keeps "
            "the same degradation with nothing suppressed."
        ),
        rule_interactions=(
            "E007 gives the same advice for the except-shaped variant (a broad catch "
            "that logs and returns an empty sentinel) and carries the same per-object "
            "caveat: inside a batch, a raise in place of one object's skip fails the "
            "whole extraction. For both rules, raise for a failure of the whole "
            "request or a transient outcome. A permanent per-object rejection stays a "
            "logged skip, and tests that assert the skip (`== {}`) keep passing "
            "unchanged. A lane fix that turned a per-object skip into a raise "
            "converted one bad object into a run-wide failure in production."
        ),
        scope=RuleScope.APP,
        name="HttpFailureToEmptyReturn",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="error-to-return-value",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.9.0",
        rationale=(
            "A guard that checks an HTTP response for failure and then returns an "
            "empty/None sentinel converts a remote API failure into an empty "
            "successful result — the workflow publishes a zero or partial crawl as "
            "if it completed, with no error surfaced. Unlike E001/E002/E007 there is "
            "no except/raise to key on; the failure is swallowed by a plain if-guard."
        ),
        short_description=(
            "Checked HTTP-response failure returns an empty/None sentinel instead of "
            "raising — silently publishes a failure as success"
        ),
        full_description=(
            "An ``if`` whose test inspects an HTTP response for failure (a negation\n"
            "or comparison on ``is_success`` / ``ok`` / ``status_code``) and whose\n"
            "branch ``return``\\ s an empty/None sentinel (``return``, ``None``,\n"
            '``[]``, ``{}``, ``()``, ``""``, ``list()``/``dict()``/``set()``).  The\n'
            "remote failure is coerced into an empty *successful* result, so the\n"
            "workflow reports a zero/partial crawl as complete and no error reaches\n"
            "the operator or the Automation Engine.\n"
            "\n"
            "Both polarities are covered: failure-in-the-test → empty body\n"
            "(``if not resp.is_success: return []``) and its mirror, success-in-the-\n"
            "test → empty ``else`` (``if resp.is_success: … else: return []``).\n"
            "\n"
            "This escapes the rest of the E-series because there is no\n"
            "``except``/``raise`` — it is a plain response guard.  Fix: raise a typed\n"
            "``AppError`` on the failure branch (``SourceUnavailableError`` or an app\n"
            "subclass of it for a customer-controlled source API; see E012).  Where one\n"
            "failure must not abort the crawl, keep the raise and catch that typed\n"
            "error in the tolerating function.  The handler logs with\n"
            "``exc_info=True``, records a residual and returns the empty sentinel, and\n"
            "the run declares the gap as ``PARTIAL_SUCCESS``.  Anchored on HTTP-response\n"
            "markers and a failure-shaped test to avoid flagging ordinary\n"
            "``if x is None: return None`` guards.  ``!=`` against a single non-2xx\n"
            "constant (``status_code != 401``) is not failure-shaped: that branch also\n"
            "takes every 2xx response.  ``!= 200`` and ``>= 400`` still are.\n"
            "\n"
            "``.ok`` and ``.is_success`` are also plain field names on non-HTTP\n"
            "result objects (a ``StageResult`` dataclass), so they count only on an\n"
            "owner that is plausibly an HTTP response: its name says so (``resp``),\n"
            "the same function reads a response-only attribute on it\n"
            "(``.status_code``, ``.json()``, ``.headers``), it is a parameter\n"
            "annotated ``...Response``, or the function binds it (``=``, ``:=``,\n"
            "``with ... as``) from an httpx / requests / aiohttp call, an HTTP verb\n"
            "(``.get`` / ``.post`` / ``.request`` ...) or a callee named for\n"
            "``http`` / ``request``.  ``status_code`` needs no such evidence.  A bare\n"
            "``return`` / ``return None`` inside a function annotated ``-> None`` is\n"
            "not reported either: a procedure has no result to publish, the same\n"
            "carve-out E007 makes.  ``return []`` there still fires.\n"
            "\n"
            "Raise versus skip.  Raise the typed error for a failure of the whole\n"
            "request, and for transient outcomes (no response, 408, 429, 5xx).  A\n"
            "permanent rejection of ONE object inside a batch or iteration (its\n"
            "detail fetch answers 400/404/410 because the object was dropped between\n"
            "listing and fetch, or is invalid) is a skip: log the status and the\n"
            "source's error reason at WARNING, never the response body, and continue.\n"
            "Converting such a skip into a raise is a behaviour change: one bad\n"
            "object then fails the whole extraction.  It is never the default fix,\n"
            "and existing tests that assert the skip (``== {}``) must keep passing\n"
            "unchanged.\n"
            "\n"
            "Exempt: an empty return directly in the body of an ``if`` that pins the\n"
            "status to permanent per-object rejections\n"
            "(``r.status_code in (400, 404)``, ``r.status_code == 404``; codes from\n"
            "{400, 404, 410}; ``and``-ed terms allowed) on the response the failure\n"
            "test is about, when a warning/error-level log call earlier in that body\n"
            "says the object is skipped and passes the status itself as an argument\n"
            "(a status inside another expression does not count).  A local\n"
            "(``status = r.status_code``, or ``... if r is not None else None``)\n"
            "counts when that is its last binding among the guard's earlier siblings\n"
            "and it is not rebound before the log.\n"
            "Still reported: a bare ``if not r.is_success: return {}``; transient\n"
            "codes or a missing response returning empty; a skip with no log, a\n"
            "debug/info log, a log without the status or without saying it skips; a\n"
            "guard on a different response than the one that failed; an ``or``-ed guard; a range\n"
            "such as ``400 <= r.status_code < 500``; a named code constant\n"
            "(``in PERMANENT_CODES``); and 401/407 (a credential rejection hits every\n"
            "object).  A 403\n"
            "skip also still reports: whether a permission denial should skip or\n"
            "raise is a site-level judgment that this rule does not make.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e020",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
    RuleDefinition(
        id="E021",
        canonical_reference=(
            "atlan-mysql-app app/failures.py — `SourceRestartingError` and "
            "`PreflightProbeTimeoutError` subclass `SourceUnavailableError` with "
            "SOURCE_UNAVAILABLE_* codes and no audience override: an unreachable "
            "customer source is SOURCE_UNAVAILABLE / USER by inheritance."
        ),
        scope=RuleScope.BOTH,
        name="DependencyUnavailableOverriddenToUser",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="failure-category",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.47.0",
        rationale=(
            "DEPENDENCY_UNAVAILABLE means an Atlan platform service (Dapr, Temporal, "
            "object store) is down, so a hard preflight gate never blocks on it. "
            "Overriding its audience to USER says the customer's own system is down, "
            "which is SOURCE_UNAVAILABLE, and SOURCE_UNAVAILABLE blocks a hard gate on "
            "its final attempt. "
            "Customer impact: with the wrong category, a hard gate lets a run start "
            "against an unreachable source, and the run then fails on the same fault "
            "after consuming its slot. The failure also routes inconsistently: the "
            "category says PLATFORM while the audience says USER."
        ),
        short_description=(
            "DependencyUnavailableError subclass overrides audience to USER — a "
            "customer-owned system that is down is SourceUnavailableError"
        ),
        full_description=(
            "A class whose category is DEPENDENCY_UNAVAILABLE assigns\n"
            "``audience = Audience.USER`` in its own body.  The category is read the\n"
            "way Python's MRO reads it: from the first base, in declaration order,\n"
            "that resolves to an SDK error, unless the class assigns ``category``\n"
            "itself.  So ``class X(NotFoundError, StorageError)`` is NOT_FOUND and is\n"
            "not flagged, even though ``StorageError`` is a DEPENDENCY_UNAVAILABLE\n"
            "leaf.  SDK subclasses outside ``application_sdk.errors`` (e.g.\n"
            "``StorageError``, ``AzureConnectionError``) count.  An app-defined\n"
            "intermediate is followed when it is defined in the same module or\n"
            "imported by name (``from app.base import AppDependencyError``, absolute\n"
            "or relative); a base reached through a module alias\n"
            "(``import app.base as base`` then ``base.AppDependencyError``) is not\n"
            "followed, and the class is not flagged.\n"
            "\n"
            "Fix: re-base the class on ``SourceUnavailableError`` (or an app\n"
            "subclass of it), rename its code to ``SOURCE_UNAVAILABLE_*``, and delete\n"
            "the audience override, which ``SourceUnavailableError`` already sets.\n"
            "This is a behaviour change for an app running a hard preflight gate:\n"
            "the failure now blocks on the gate's final attempt.  Say so in the PR.\n"
            "\n"
            "Only the class that assigns the override is flagged; subclasses that\n"
            "inherit it are fixed by the same edit.  A DEPENDENCY_UNAVAILABLE leaf\n"
            "raised for a customer source *without* an override is not detected:\n"
            "the rule matches the structural pattern and does not infer intent from\n"
            "names.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/conformance/docs/rules/error-handling.md#e021",
        remediation_reference=RemediationReference(
            kind=RemediationKind.PRESCRIPTION,
            target="programs/areas/error-handling.prose.md",
        ),
    ),
)
