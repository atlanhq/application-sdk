"""``SqlApp``'s auth-cache prime: the probe task and its failure classifier.

``prime_sql_auth`` runs one ``SELECT 1`` before the parallel extract burst
(BLDX-1295) and classifies a failure against the live exception, while its
cause chain is still there; ``_classify_prime_failure`` rebuilds that verdict
on the workflow side. Split out of ``templates/sql_app.py`` (FND-4267):
:class:`~application_sdk.templates.sql_app.SqlApp` inherits it, so every name
here is reached as ``SqlApp.<name>``.
"""

from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING

from pydantic import ValidationError

from application_sdk.app.task import task
from application_sdk.clients._sql_failure_classifier import (
    SqlFailureKind,
    classify_sql_failure,
    exception_class_names,
    root_cause,
)
from application_sdk.contracts.base import OutputStatus
from application_sdk.errors import (
    AppError,
    redact_secrets,
    safe_traceback,
)

# ``AppTimeoutError`` is re-exported: importable from here before FND-4687 moved
# the wire-envelope rebuild into ``errors.wire`` (Symbol Removal Check).
from application_sdk.errors.leaves import AppTimeoutError as AppTimeoutError
from application_sdk.errors.leaves import (
    AuthError,
    InternalError,
    SourceUnavailableError,
)
from application_sdk.errors.wire import (
    FailureDetails,
    degraded_failure_details,
    error_from_failure_details,
    redact_evidence_value,
    redacted_validation_reason,
    register_wire_leaf,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionTaskInput,
    PrimeAuthOutput,
)
from application_sdk.templates.sql_app_errors import (
    SqlProbeTimeoutError,
)

if TYPE_CHECKING:
    pass


from application_sdk.templates._sql_app_entity import _SqlEntityIOMixin

logger = get_logger(__name__)

# ``_classify_probe_strings`` emits this leaf; registering it lets the verdict
# rebuild as itself on the far side of the activity boundary, where only the
# ``FailureDetails`` envelope survives. The generic leaves it can also emit
# (``AuthError``, ``SourceUnavailableError``, ``InternalError``) are registered
# by ``errors.wire``. A verdict recorded before FND-4298 carries
# ``TIMEOUT_SQL_PROBE``, which has no leaf any more; it rebuilds on the generic
# envelope-routed leaf with the category the envelope recorded.
register_wire_leaf(SqlProbeTimeoutError)

# Field names every ``AppError`` declares on the base dataclass — the typed-error
# redactor skips these (they are handled individually) and redacts only the
# per-leaf custom fields, mirroring what ``to_failure_details()`` treats as
# evidence.
_BASE_ERROR_FIELD_NAMES: frozenset[str] = frozenset(
    f.name for f in dataclasses.fields(AppError)
)

# The one implementation lives with the shared SQL failure classifier; bound
# under the private name the probe code has always used.
_root_cause = root_cause


class _SqlPrimeAuthMixin(_SqlEntityIOMixin):
    """The prime task and its classifier, mixed into ``SqlApp``."""

    # retry_max_attempts=1 is load-bearing: the body's try/except only
    # catches Python exceptions, NOT Temporal-level failures (start-to-close
    # timeout, worker eviction, heartbeat timeout, OOM). Any of those
    # triggers activity-level retry, which re-runs
    # ``_init_sql_client → load → auth handshake`` — re-stacking
    # ``failed_login_attempts`` on the source, i.e. the exact lockout-cycle
    # this task exists to prevent. ``@task()`` defaults to
    # ``retry_max_attempts=3`` (application_sdk/app/task.py), so we MUST
    # override explicitly. See application-sdk#1835 mothership review
    # comment-3287629972.
    # The budget is 120s because the activity has to cover credential
    # resolution (which can be slow on some deployments) as well as the probe,
    # and retry_max_attempts=1 makes a timeout terminal for the workflow attempt.
    @task(timeout_seconds=120, retry_max_attempts=1)
    async def prime_sql_auth(self, input: ExtractionTaskInput) -> PrimeAuthOutput:
        """Single sequential probe that primes the SQL server's auth cache
        before the parallel ``extract_entity`` burst.

        Background — why this exists (BLDX-1295):
            v3's parallel-activity execution model fans out
            ``extract_databases / extract_schemas / extract_tables /
            extract_columns`` (and on subclasses, ``extract_procedures``)
            as concurrent Temporal activities. Each one creates its own
            ``SQLClient`` instance and opens its own connections to the
            source — roughly simultaneously when the workflow first
            starts.

            MySQL 8's default auth plugin ``caching_sha2_password`` has a
            server-side cache keyed by ``(user, password_hash)``. The
            *first* auth for a given user is cache-cold and requires
            either (a) a TLS-encrypted connection or (b) an RSA key
            exchange between client and server. Without either — which
            is the default for ``aiomysql`` against a typical customer
            MySQL — a cache-cold auth attempt fails with
            ``Access denied (1045)``. When N parallel activities all hit
            cold-cache simultaneously, each independently tries full
            auth, each fails, and those failures stack on the server's
            ``failed_login_attempts`` counter — tripping the
            ``FAILED_LOGIN_ATTEMPTS`` lockout policy if the customer has
            one set (most do). The lockout then poisons every subsequent
            scheduled run until it auto-expires, at which point a manual
            "Test Authentication" works briefly, the next cron burst
            re-trips it, repeat.

            The Argo-era extract ran phases serially — only one
            connection at a time — so the cache had time to prime
            between phases and the race never occurred. v3's parallel
            model exposes it.

            This task is the Argo-parity fix: ``run()`` awaits it
            *once*, sequentially, before the ``asyncio.gather(...)`` of
            extract activities. It opens one connection, runs
            ``SELECT 1``, closes. That populates MySQL's
            ``caching_sha2_password`` cache; subsequent parallel
            connections take the fast path and never need full auth.

            For SQL servers without this auth-cache concept (Postgres,
            MSSQL, Snowflake on most setups), this is harmless overhead
            — a single ~100ms probe round-trip per workflow run.

        Failure semantics (return-not-raise + zero Temporal retries):
            If the probe fails (auth rejected, network unreachable,
            wrong host, etc.) this task catches the exception, classifies
            it *here* — while the live exception and its ``__cause__``
            chain are still available — and returns the verdict as
            ``failure`` (plus ``success=False`` and the root cause's
            ``error_type`` / ``error_message``) on the returned
            ``PrimeAuthOutput``. ``run()`` rebuilds that typed error
            (``AuthError``, ``SqlProbeTimeoutError`` or
            ``SourceUnavailableError``) and raises it.

            Classification has to happen here, not in ``run()``: only two
            strings cross the activity boundary, and
            ``BaseSQLClient.get_results`` rewraps every driver exception as
            ``SqlPandasResultError`` with one fixed message, so those two
            strings are identical for every failure mode (CNCT-197).

            Why return failure instead of raising and letting Temporal
            retry: the failure mode this task was added to prevent is
            cache-cold auth rejection. Retrying it via Temporal's
            activity-level retry stacks the source's
            ``failed_login_attempts`` counter on each attempt — i.e. it
            accelerates the very lockout cycle the prime exists to
            avoid. The ``retry_max_attempts=1`` override on the ``@task``
            decorator above is load-bearing: the body try/except only
            catches Python exceptions, not Temporal-level failures
            (start-to-close timeout, worker eviction, heartbeat timeout,
            OOM) — those would re-run the activity even with this body
            structure, re-stacking the counter. Workflow-level retry
            (re-running the whole extraction after operator action)
            remains available and is the correct retry layer for
            transient blips.
        """
        start = time.perf_counter()
        try:
            client = await self._init_sql_client(input)
            try:
                # ``SELECT 1`` is a no-op for query semantics; what we
                # actually care about is that the client opened a
                # connection + completed the auth handshake. The cache
                # is populated as a side effect of that handshake.
                await client.get_results("SELECT 1")
            finally:
                await client.close()
        except Exception as exc:  # noqa: BLE001 — exc_info=True would embed the SQLAlchemy connection string (incl. password) in structured logs; safe_traceback with secrets redacted is logged instead
            duration_ms = (time.perf_counter() - start) * 1000.0
            # Report the ROOT cause, not whatever wrapper reached this
            # clause. ``BaseSQLClient.get_results`` rewraps every driver
            # exception as ``SqlPandasResultError`` with one fixed message, so
            # the wrapper's class name and message are identical for a DNS
            # failure, a TLS error and a credential rejection — nothing
            # downstream can tell them apart. See CNCT-197.
            root = _root_cause(exc)
            # Redact here, not downstream: the root driver message can embed a
            # connection string, and this value lands in the activity result,
            # the classified error's evidence, and the logs.
            error_message = redact_secrets(str(root))
            if len(error_message) > 500:
                error_message = error_message[:500] + "…"
            classified = self._classify_probe_exception(exc)
            # Serialising the verdict must never turn a probe failure into a
            # crashed activity — a crash is retried, and retrying this task is
            # what stacks failed_login_attempts on the source. A
            # connector-defined AppError can carry an evidence key the
            # FailureDetails denylist rejects, so degrade the evidence while
            # keeping the typed verdict: dropping to the
            # error_type / error_message path instead would re-derive routing
            # from a class name no string hint matches, inverting the
            # connector's explicit verdict (CNCT-197 all over again).
            failure: FailureDetails | None
            try:
                failure = classified.to_failure_details()
            except ValidationError as verdict_error:
                try:
                    failure = degraded_failure_details(classified)
                    outcome = "kept the typed verdict with degraded evidence"
                except ValidationError as degrade_error:
                    failure = None
                    outcome = (
                        "falling back to error_type/error_message classification "
                        "(degraded verdict also refused: %s)"
                        % redacted_validation_reason(degrade_error)
                    )
                logger.warning(
                    "Could not serialise the prime failure verdict (%s) as-is; %s. %s",
                    type(classified).__name__,
                    outcome,
                    redacted_validation_reason(verdict_error),
                )
            # We want the traceback frames in worker logs — the long-tail
            # (TLS negotiation, driver bugs, version skew) is only diagnosable
            # from the original frames. But exc_info=True would render the
            # SQLAlchemy cause's message verbatim, and that embeds the full
            # connection string incl. password. safe_traceback preserves the
            # frames and strips credentials before logging.
            logger.error(  # conformance: ignore[E005,L004] exc_info would expose SQLAlchemy password in traceback; safe_traceback redacts secrets from the frames
                "SQL auth cache prime FAILED after %.1fms (%s → %s) — short-circuiting "
                "before parallel extract burst to avoid stacking failed_login_attempts "
                "on the source.\n%s",
                duration_ms,
                type(root).__name__,
                classified.qualified_code,
                safe_traceback(exc),
            )
            return PrimeAuthOutput(
                status=OutputStatus.FAILURE,
                duration_ms=duration_ms,
                success=False,
                failure=failure,
                error_type=type(root).__name__,
                error_message=error_message,
            )
        duration_ms = (time.perf_counter() - start) * 1000.0
        logger.info(
            "SQL auth cache primed in %.1fms before parallel extract fan-out",
            duration_ms,
        )
        return PrimeAuthOutput(duration_ms=duration_ms, success=True)

    @staticmethod
    def _classify_prime_failure(prime_result: PrimeAuthOutput) -> Exception:
        """Rebuild the typed error for a failed ``PrimeAuthOutput``.

        ``prime_sql_auth`` already classified the failure against the live
        exception, so the work here is reconstruction, not classification —
        one classifier, one implementation, no chance of the two diverging.

        The string-matching fallback stays for outputs that carry no
        ``failure``: activity results produced by an older SDK and replayed
        from Temporal history, and connectors that build a
        ``PrimeAuthOutput`` by hand.
        """
        if prime_result.failure is not None:
            return error_from_failure_details(prime_result.failure)
        # Defence-in-depth: the fallback trusts a hand-built or pre-redaction-era
        # replayed output whose producer may not have redacted. Re-redact at this
        # trust boundary so a secret in error_message never reaches the verdict.
        return _SqlPrimeAuthMixin._classify_probe_strings(
            prime_result.error_type or "",
            redact_secrets(prime_result.error_message or ""),
        )

    @staticmethod
    def _classify_probe_exception(exc: BaseException) -> AppError:
        """Classify a live probe exception, cause chain included.

        Classifying here rather than at the activity boundary is the whole
        point: ``BaseSQLClient.get_results`` rewraps every driver exception as
        ``SqlPandasResultError``, so by the time only a class name and a
        message survive there is nothing left to discriminate on (CNCT-197).

        An already-typed root cause keeps its verdict — if a connector took
        the trouble to raise a typed ``AppError``, that beats anything string
        matching can infer. But the typed path reaches ``to_failure_details()``
        without the redaction the plain-driver path gets, so a connection
        string in the message or a custom evidence field would serialise onto
        the wire verbatim. Redact before returning, as the sibling string path
        already does.
        """
        root = _root_cause(exc)
        if isinstance(root, AppError):
            return _SqlPrimeAuthMixin._redact_typed_error(root)
        # Classify on the base class names too, as ``load()`` does: a driver's
        # per-SQLSTATE subclass is named for the code, not the failure kind.
        return _SqlPrimeAuthMixin._classify_probe_strings(
            type(root).__name__,
            redact_secrets(str(root)),
            cause=root,
            class_names=exception_class_names(type(root)),
        )

    @staticmethod
    def _redact_typed_error(err: AppError) -> AppError:
        """Redact everything on a typed error that serialises onto the wire.

        Only ``cause_repr`` is sanitised by ``to_failure_details()``; the
        ``message``, ``suggested_action`` and per-leaf evidence fields are
        not. Redact them in place so the wire envelope (and the Temporal
        payload) never carries a secret the producer embedded in any field —
        a base wire field or a nested evidence structure alike.
        """
        err.message = redact_secrets(err.message)
        if err.suggested_action is not None:
            err.suggested_action = redact_secrets(err.suggested_action)
        for f in dataclasses.fields(err):
            if f.name in _BASE_ERROR_FIELD_NAMES:
                continue
            setattr(err, f.name, redact_evidence_value(getattr(err, f.name)))
        return err

    @staticmethod
    def _classify_probe_strings(
        error_type: str,
        error_message: str,
        cause: BaseException | None = None,
        *,
        class_names: str | None = None,
    ) -> AppError:
        """Map a probe failure's class name and message to a typed error.

        Painting every probe failure as ``AuthError`` would mis-direct
        on-call: a DBA who follows the lockout-specific
        ``suggested_action`` for a DNS misconfiguration wastes time on
        a non-existent account-lockout. We discriminate based on
        ``error_type`` / ``error_message`` and return:

        - ``AuthError`` for actual credential rejections
          (``Access denied``, MySQL code 1045,
          ``AuthenticationError`` class names).
        - ``SqlProbeTimeoutError`` for probe timeouts (``TimeoutError`` /
          ``asyncio.TimeoutError`` / message contains ``timed out``, and the
          ODBC login-timeout shape ``HYT00`` / ``login timeout`` /
          ``timeout expired``). A ``SourceUnavailableError`` (USER): the
          only timeouts that can reach here are the driver's and the
          socket's, so the clock always ran out waiting for the customer's
          source.
        - ``SourceUnavailableError`` for network / DNS / TLS /
          connection-refused failures — the customer-owned source is
          presumed reachable but didn't respond cleanly, so the failure
          routes to the connector owner (USER), not Atlan infra ops.
        - ``InternalError`` as the last-resort fallback so we never
          return ``None`` and never accidentally swallow an unknown
          driver exception class.

        ``error_message`` must already be secret-redacted — it is copied
        verbatim into the returned error's evidence fields.

        ``class_names`` is what the classifier matches when the live exception
        is at hand (its class and non-builtin base names); ``error_type`` stays
        the single name shown in messages. Without it, ``error_type`` is matched.
        """
        kind = classify_sql_failure(class_names or error_type, error_message)
        if kind is SqlFailureKind.TIMEOUT:
            return SqlProbeTimeoutError(
                message=(
                    "SQL auth-cache prime probe timed out before parallel "
                    f"extract burst ({error_type}): {error_message}"
                ),
                cause=cause,
                suggested_action=(
                    "Check network reachability and source health: the probe "
                    "did not get an auth response within the 120s task "
                    "deadline. Verify the source listener is up and the "
                    "worker → source network path is healthy before "
                    "retrying the workflow."
                ),
            )

        if kind is SqlFailureKind.AUTH:
            return AuthError(
                message=(
                    "SQL auth-cache prime rejected by source before parallel "
                    f"extract burst ({error_type}). Skipping extract fan-out "
                    "to avoid stacking failed_login_attempts on the source's "
                    "lockout counter."
                ),
                auth_method="sql_client",
                cause=cause,
                failure_reason=error_message,
                suggested_action=(
                    "Verify the SQL connection credentials. If the source is "
                    "MySQL 8 and credentials are correct, also confirm the "
                    "user is not currently locked out by "
                    "FAILED_LOGIN_ATTEMPTS policy — wait for auto-unlock or "
                    "have a DBA run ALTER USER … ACCOUNT UNLOCK before "
                    "retrying the workflow."
                ),
            )

        # The customer owns the source system, and the actionable guidance is
        # "fix the network path, the source itself may be fine."
        if kind is SqlFailureKind.SOURCE_UNAVAILABLE:
            return SourceUnavailableError(
                message=(
                    "SQL auth-cache prime could not reach the source before "
                    f"parallel extract burst ({error_type})."
                ),
                source_type="sql",
                cause=cause,
                network_error=error_message,
                suggested_action=(
                    "Check DNS resolution, NAT/firewall rules, TLS "
                    "configuration, and that the source listener is "
                    "accepting connections from the worker network. The "
                    "credentials path was not exercised, so this is not a "
                    "lockout situation — do not run ACCOUNT UNLOCK."
                ),
            )

        # Last-resort: never swallow an unknown driver error class.
        # ``classification_pending=True`` flags this to wire/log
        # consumers that the on-call should help refine the classifier
        # rather than treat the category at face value.
        return InternalError(
            message=(
                "SQL auth-cache prime failed before parallel extract burst "
                f"with an unclassified error ({error_type}): {error_message}"
            ),
            component="sql_app.prime_sql_auth",
            cause=cause,
            classification_pending=True,
            suggested_action=(
                "Inspect the worker logs (the prime task logs the original "
                "traceback, with secrets redacted, via safe_traceback). The "
                "error type was not recognised as auth / timeout / network — "
                "please file a ticket so the classifier in "
                "SqlApp._classify_probe_strings can be extended."
            ),
        )
