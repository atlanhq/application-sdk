"""SqlApp — consolidated SQL metadata extraction template.

Replaces ``SqlMetadataExtractor``, ``SqlQueryExtractor``, and
``BaseMetadataExtractor`` with a single App class that provides:

* ``extract_*`` ``@task`` methods that **stream SQL rows verbatim into
  raw JSONL** under ``raw/<entity>/records.json`` — one pass, no parquet
  intermediate, no pandas/pyarrow.
* ``transform_*`` ``@task`` methods that read the raw JSONL and run each
  record through the connector's ``map_*`` function, writing the result
  to ``transformed/<entity>/entities.json``. The activity boundary keeps
  transform retry-able without re-running the SQL extraction.
* Per-entity asset mappers (``map_database`` / ``map_schema`` /
  ``map_table`` / ``map_column`` / ``map_procedure``) — direct
  pyatlan_v9 ``Asset`` construction, no YAML transformer.
* Per-entity ``FileReference`` emission with pre-set canonical
  ``storage_path`` keys (``<run_prefix>/raw/<entity>/records.json`` /
  ``<run_prefix>/transformed/<entity>/entities.json``) — the activity
  interceptor's persist step uploads each one to that exact key, so
  downstream publish discovers the data at the path it expects with
  no separate upload step needed (BLDX-1281).
* ``build_task_input()`` as public API for ``run()`` overrides (BLDX-1138).
* ``extract_views()`` / ``extract_procedures()`` /
  ``transform_views()`` / ``transform_procedures()`` for the optional
  flows (BLDX-1139).
* Parallel extract + transform via ``asyncio.gather()`` (BLDX-1140).

Usage::

    from application_sdk.templates.sql_app import SqlApp
    from application_sdk.clients.sql import BaseSQLClient
    from application_sdk.clients.models import DatabaseConfig

    class MySQLClient(BaseSQLClient):
        DB_CONFIG = DatabaseConfig(
            template="mysql+pymysql://{username}:{password}@{host}:{port}/{database}",
            required=["username", "password", "host", "port"],
        )

    class MySQLApp(SqlApp):
        sql_client_class = MySQLClient

        fetch_database_sql = "SELECT SCHEMA_NAME as database_name FROM ..."
        fetch_schema_sql = "SELECT SCHEMA_NAME as schema_name FROM ..."
        fetch_table_sql = "SELECT TABLE_NAME as table_name FROM ..."
        fetch_column_sql = "SELECT COLUMN_NAME as column_name FROM ..."

        def map_table(self, record, connection_qn):
            from pyatlan_v9.model.assets import Database, Schema, Table

            # Build assets with the pyatlan_v9 ``.creator()`` factories: each one
            # computes its own qualifiedName from its parent's, so the grammar
            # lives in pyatlan, not in a hand-rolled ``f"{connection_qn}/..."``
            # string per connector (which conformance P028 flags).
            database = Database.creator(
                name=record["database_name"],
                connection_qualified_name=connection_qn,
            )
            schema = Schema.creator(
                name=record["schema_name"],
                database_qualified_name=database.qualified_name,
            )
            return Table.creator(
                name=record["table_name"],
                schema_qualified_name=schema.qualified_name,
            )

        def map_column(self, record, connection_qn):
            from pyatlan_v9.model.assets import Column
            return Column(...)
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, Union

import orjson
from pydantic import ValidationError
from temporalio import workflow as _temporal_workflow

from application_sdk._runtime.offload import run_in_thread
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry
from application_sdk.app.task import task
from application_sdk.common.sql_filters import (
    normalize_filters,
    safe_substitute_placeholders,
)
from application_sdk.constants import TEMPORARY_PATH, WORKFLOW_OUTPUT_PATH_TEMPLATE
from application_sdk.contracts.base import OutputStatus
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.credentials import CredentialResolver, legacy_credential_ref
from application_sdk.credentials.ref import CredentialRef
from application_sdk.errors import (
    AppError,
    FailureDetails,
    redact_secrets,
    redact_wire_value,
    safe_traceback,
    sanitize_cause_repr,
)
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AppTimeoutError,
    AuthError,
    InternalError,
    SourceUnavailableError,
)
from application_sdk.errors.wire import secret_named_evidence_keys
from application_sdk.execution import build_output_path, get_object_store_prefix
from application_sdk.infrastructure.context import get_infrastructure
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
    ExtractionTaskInput,
    ExtractionTaskOutput,
    PrimeAuthOutput,
    TransformInput,
    TransformOutput,
)
from application_sdk.templates.sql_app_errors import SqlProbeTimeoutError

if TYPE_CHECKING:
    from pyatlan_v9.model.assets import Asset

    from application_sdk.clients.sql import BaseSQLClient

logger = get_logger(__name__)

_ET = TypeVar("_ET", bound=ExtractionTaskInput)

#: Mapper-function signature: takes a raw record dict + connection
#: qualified name, returns a pyatlan_v9 Asset (preferred) or a plain dict
#: (legacy / connector-specific shapes the v3 publisher accepts).
_MapperFn = Callable[[dict[str, Any], str], Union["Asset", dict[str, Any]]]

#: SQL row batch size used by ``_extract_entity`` when streaming results
#: through ``client.run_query()``. Caps the in-flight set at
#: ``_EXTRACT_BATCH_SIZE * row_size`` regardless of total result size.
_EXTRACT_BATCH_SIZE: int = 10_000

#: Leaves ``_classify_probe_strings`` can emit, keyed by wire code. Used to
#: rebuild the typed error on the far side of the activity boundary, where only
#: the ``FailureDetails`` envelope survives. The base ``TIMEOUT`` code is
#: accepted alongside the probe-specific one so a verdict from any other
#: producer still rebuilds as a timeout rather than degrading to internal.
_PROBE_FAILURE_LEAVES: dict[str, type[AppError]] = {
    AuthError.code: AuthError,
    SqlProbeTimeoutError.code: SqlProbeTimeoutError,
    AppTimeoutError.code: AppTimeoutError,
    SourceUnavailableError.code: SourceUnavailableError,
    InternalError.code: InternalError,
}


# Field names every ``AppError`` declares on the base dataclass — the evidence
# redactor skips these (they are handled individually) and redacts only the
# per-leaf custom fields, mirroring what ``to_failure_details()`` treats as
# evidence.
_BASE_ERROR_FIELD_NAMES: frozenset[str] = frozenset(
    f.name for f in dataclasses.fields(AppError)
)


@dataclasses.dataclass(kw_only=True)
class _EnvelopeError(AppError):
    """Reconstruction leaf for a wire ``code`` this consumer has no leaf for.

    Routing (``category`` / ``audience``) is taken from the envelope, not from
    class-level constants, so a newer producer's verdict keeps its original
    routing instead of degrading to ``INTERNAL`` / ``APP_OWNER``.

    ``category`` / ``audience`` are instance values here, but the base
    ``to_failure_details()`` reads ``audience`` class-level
    (``type(self).audience``) — which would return the raw ``property`` object
    on this subclass and fail validation. The serializer is therefore
    overridden to read both instance-level, so a reconstructed unknown-code
    error round-trips.

    ``code`` / ``cause_repr`` / ``evidence`` are likewise carried as
    passthrough instance fields so the producer's diagnostic context and
    fine-grained code survive re-serialisation — not just the routing.
    """

    category_override: FailureCategory
    audience_override: Audience
    code_override: str | None = None
    cause_repr: str | None = None
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def category(self) -> FailureCategory:  # type: ignore[override]
        return self.category_override

    @property
    def audience(self) -> Audience:  # type: ignore[override]
        return self.audience_override

    @property
    def code(self) -> str:  # type: ignore[override]
        return self.code_override or super().code

    def to_failure_details(self) -> FailureDetails:
        # ``category_override`` / ``audience_override`` / ``code_override``
        # carry routing, not evidence — exclude them alongside the base
        # fields; the producer's evidence returns as the envelope's evidence.
        # ``cause_repr`` serialises as its own envelope field, not evidence.
        evidence: dict[str, Any] = {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if f.name
            not in _BASE_ERROR_FIELD_NAMES
            | {
                "category_override",
                "audience_override",
                "code_override",
                "cause_repr",
                "evidence",
            }
        }
        evidence.update(self.evidence)
        return FailureDetails(
            category=self.category,
            code=self.code,
            retryable=self.effective_retryable,
            audience=self.audience,
            message=self.message,
            suggested_action=self.suggested_action,
            evidence=evidence,
            app_name=self.app_name,
            run_id=self.run_id,
            cause_repr=self.cause_repr,
        )


def _root_cause(exc: BaseException) -> BaseException:
    """Walk ``__cause__`` to the innermost exception.

    The SDK's SQL client rewraps driver exceptions, so the outermost
    exception describes the wrapper and the innermost describes what
    actually happened. Cycle-guarded — a hand-built ``__cause__`` loop
    would otherwise hang the worker.
    """
    seen = {id(exc)}
    while exc.__cause__ is not None and id(exc.__cause__) not in seen:
        exc = exc.__cause__
        seen.add(id(exc))
    return exc


def _redact_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Redact every string reachable inside an evidence mapping's values.

    Keys are left alone — a secret-*named* key is the wire denylist's job
    (:func:`secret_named_evidence_keys`); this handles secret-*carrying*
    values under any key.
    """
    return {k: redact_wire_value(v) for k, v in evidence.items()}


def _failure_details_degraded(err: AppError) -> FailureDetails | None:
    """Re-serialise a verdict the wire envelope rejected, keeping its routing.

    Reached when ``to_failure_details()`` raises — in practice a connector leaf
    carrying a secret-named evidence field (``api_key``, ``db_password``, …),
    which the ``FailureDetails`` denylist refuses. Falling straight back to
    ``error_type`` / ``error_message`` would throw away the connector's
    explicit verdict and re-derive it from a class name no string hint matches,
    silently inverting e.g. ``AuthError`` / ``USER`` into ``InternalError`` /
    ``APP_OWNER`` — the exact CNCT-197 misrouting the typed path exists to fix.

    So degrade the *evidence*, never the routing: drop the offending keys, and
    if the envelope still refuses, drop evidence entirely. Only a verdict that
    fails both attempts returns ``None`` (the caller then falls back to
    strings). ``category`` / ``audience`` / ``code`` / ``retryable`` are read
    instance-level so a reconstruction leaf's property overrides are honoured
    alongside a normal leaf's class constants.

    Args:
        err: The classified verdict whose serialisation was rejected.

    Returns:
        The degraded envelope, or ``None`` if it could not be built at all.
    """
    evidence: dict[str, Any] = {
        f.name: getattr(err, f.name)
        for f in dataclasses.fields(err)
        if f.name not in _BASE_ERROR_FIELD_NAMES
    }
    cause_repr = sanitize_cause_repr(err.cause) if err.cause else None
    if isinstance(err, _EnvelopeError):
        # Mirror its ``to_failure_details()``: routing overrides are not
        # evidence, ``cause_repr`` is its own wire field, and the producer's
        # evidence rides in a field rather than as per-leaf dataclass fields.
        for name in (
            "category_override",
            "audience_override",
            "code_override",
            "cause_repr",
            "evidence",
        ):
            evidence.pop(name, None)
        evidence.update(err.evidence)
        cause_repr = err.cause_repr or cause_repr

    dropped = secret_named_evidence_keys(evidence)
    attempts: list[dict[str, Any]] = [
        {k: v for k, v in evidence.items() if k not in dropped}
    ]
    if attempts[0]:
        # Second attempt only differs if the first still carried something.
        attempts.append({})
    for attempt in attempts:
        try:
            return FailureDetails(
                category=err.category,
                code=err.code,
                retryable=err.effective_retryable,
                audience=err.audience,
                message=err.message,
                suggested_action=err.suggested_action,
                evidence=_redact_evidence(attempt),
                app_name=err.app_name,
                run_id=err.run_id,
                cause_repr=cause_repr,
            )
        except ValidationError as degrade_error:
            logger.warning(
                "Degraded prime failure verdict (%s) still would not serialise "
                "with %d evidence key(s); retrying with less. %s",
                type(err).__name__,
                len(attempt),
                _redacted_validation_reason(degrade_error),
            )
    return None


def _redacted_validation_reason(exc: ValidationError) -> str:
    """Return the validator messages only, dropping the rejected input.

    ``str(ValidationError)`` embeds ``input_value=…`` — for a rejected
    ``evidence`` dict that is the secret-named payload the denylist exists to
    keep off the wire, so neither the exception nor ``exc_info`` may be logged.
    The ``msg`` fields name the offending keys and nothing else.
    """
    return "; ".join(str(err.get("msg", "")) for err in exc.errors())


def _error_from_failure_details(details: FailureDetails) -> AppError:
    """Rebuild a typed error from its wire envelope.

    ``evidence`` keys are the producing dataclass's field names, so they map
    straight back onto the leaf's constructor. Keys the leaf does not declare
    are dropped rather than raising — a newer producer must not break an older
    consumer replaying its history.

    An unknown ``code`` (a newer producer than this consumer) has no registered
    leaf. Rebuilding it as ``InternalError`` would force ``INTERNAL`` /
    ``APP_OWNER`` routing and silently re-route a customer-owned failure to
    Atlan, so the unknown-code path instead preserves the envelope's own
    ``category`` / ``audience`` / ``retryable`` on a generic reconstruction
    leaf.

    Reconstruction is an unredacted trust boundary, so every field copied off
    the envelope is re-redacted here. The ``FailureDetails`` denylist rejects
    secret-named evidence *keys* only — never a nested value, and never the
    ``message`` / ``suggested_action`` / ``cause_repr`` strings — so a
    pre-redaction-era envelope replayed from Temporal history, a hand-built
    ``PrimeAuthOutput``, or a future producer that forgets to redact would
    otherwise put a live DSN back onto the wire when this error re-serialises.
    """
    message = redact_secrets(details.message)
    suggested_action = (
        redact_secrets(details.suggested_action)
        if details.suggested_action is not None
        else None
    )
    leaf = _PROBE_FAILURE_LEAVES.get(details.code)
    if leaf is None:
        return _EnvelopeError(
            category_override=details.category,
            audience_override=details.audience,
            code_override=details.code,
            cause_repr=(
                redact_secrets(details.cause_repr)
                if details.cause_repr is not None
                else None
            ),
            evidence=_redact_evidence(dict(details.evidence)),
            message=message,
            retryable=details.retryable,
            suggested_action=suggested_action,
            app_name=details.app_name,
            run_id=details.run_id,
        )
    # ``declared`` must exclude the base ``AppError`` fields: they are passed
    # as explicit constructor arguments below, so an evidence key that shadows
    # one (``message``, ``retryable``, …) would splat alongside the explicit
    # arg and crash reconstruction with ``TypeError: got multiple values``.
    # Mirrors the serializer, which never serialises base fields as evidence.
    declared = {f.name for f in dataclasses.fields(leaf)} - _BASE_ERROR_FIELD_NAMES
    evidence = _redact_evidence(
        {k: v for k, v in details.evidence.items() if k in declared}
    )
    return leaf(
        message=message,
        retryable=details.retryable,
        suggested_action=suggested_action,
        app_name=details.app_name,
        run_id=details.run_id,
        **evidence,
    )


def _orjson_default(obj: Any) -> Any:
    """Fallback serialiser for orjson — covers types it doesn't handle natively.

    orjson natively serialises ``str``, ``int``, ``float``, ``bool``, ``None``,
    ``list``, ``dict``, ``datetime``, ``date``, ``time``, ``UUID`` and
    ``dataclass`` instances. SQL drivers commonly return ``Decimal`` for
    numeric columns and occasionally ``bytes`` for blob columns; both fall
    back to a JSON-safe representation here.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    # conformance: ignore[E012] orjson default= protocol contractually requires TypeError to signal non-serialisable; replacing with AppError would break serialisation
    raise TypeError(  # orjson default= protocol requires TypeError to signal non-serializable
        f"Object of type {type(obj).__name__} is not JSON-serializable"
    )


class SqlApp(App):
    """Consolidated SQL metadata extraction App.

    Subclass and set:
    - ``sql_client_class``: Your ``BaseSQLClient`` subclass
    - ``fetch_database_sql``, ``fetch_schema_sql``, etc.: SQL templates
    - ``map_table()``, ``map_column()``, etc.: Asset mapper functions

    The base ``run()`` orchestrates: extract (parallel) → transform
    (parallel) → upload. ``extract_*`` activities stream SQL rows into
    ``raw/<entity>/records.json``; ``transform_*`` activities read that
    raw JSONL and run records through the matching ``map_*`` function,
    writing Atlan assets to ``transformed/<entity>/entities.json``.

    **Parallel-extract assumption:** the default ``run()`` issues all four
    extract tasks (databases, schemas, tables, columns) concurrently via
    ``asyncio.gather()``. Each SQL template must therefore be fully
    self-contained — no cross-entity parameterisation. Connectors that
    need to iterate tables per-schema, paginate by database, or otherwise
    sequence fetches must override ``run()`` and call the ``extract_*``
    / ``transform_*`` activities directly in the order they need.
    """

    _app_registered: ClassVar[bool] = True  # abstract template, not concrete

    # ── SQL client ──────────────────────────────────────────────────────
    sql_client_class: ClassVar[type[BaseSQLClient] | None] = None

    # ── SQL templates (set in subclass) ─────────────────────────────────
    fetch_database_sql: ClassVar[str] = ""
    fetch_schema_sql: ClassVar[str] = ""
    fetch_table_sql: ClassVar[str] = ""
    fetch_column_sql: ClassVar[str] = ""
    fetch_view_sql: ClassVar[str] = ""
    fetch_procedure_sql: ClassVar[str] = ""

    # ── Temp table regex SQL fragments ──────────────────────────────────
    extract_temp_table_regex_table_sql: ClassVar[str] = ""
    extract_temp_table_regex_column_sql: ClassVar[str] = ""

    # ── Column name mappings (override if connector aliases differently) ─
    database_name_column: ClassVar[str] = "database_name"
    schema_name_column: ClassVar[str] = "schema_name"
    table_name_column: ClassVar[str] = "table_name"

    # =====================================================================
    # Public API: build_task_input (BLDX-1138)
    # =====================================================================

    @staticmethod
    def build_task_input(
        input_cls: type[_ET],
        src: ExtractionInput,
        *,
        cred_ref: CredentialRef | None = None,
    ) -> _ET:
        """Build a typed task input from the top-level extraction input.

        Public API — use this when overriding ``run()`` to construct typed
        inputs for individual ``@task`` methods.

        Args:
            input_cls: The task input class (e.g. ``ExtractionTaskInput``).
            src: The top-level ``ExtractionInput`` from the workflow.
            cred_ref: Optional credential reference for secret resolution.

        Returns:
            An instance of *input_cls* populated from *src*.
        """
        return input_cls(
            workflow_id=src.workflow_id,
            connection=src.connection,
            credential_guid=src.credential_guid,
            credential_ref=cred_ref,
            output_prefix=src.output_prefix,
            output_path=src.output_path,
            exclude_filter=src.exclude_filter,
            include_filter=src.include_filter,
            temp_table_regex=src.temp_table_regex,
            source_tag_prefix=getattr(src, "source_tag_prefix", ""),
        )

    # =====================================================================
    # @task: SQL auth cache pre-warm — Argo parity (BLDX-1295)
    # =====================================================================

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
        before the parallel ``_extract_entity`` burst.

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
        # conformance: ignore[E004] exc_info=True would embed the SQLAlchemy connection string (incl. password) in structured logs; safe_traceback with secrets redacted is logged instead
        except Exception as exc:
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
            try:
                failure = classified.to_failure_details()
            except ValidationError as verdict_error:
                failure = _failure_details_degraded(classified)
                logger.warning(
                    "Could not serialise the prime failure verdict (%s) as-is; "
                    "%s. %s",
                    type(classified).__name__,
                    (
                        "kept the typed verdict with degraded evidence"
                        if failure is not None
                        else "falling back to error_type/error_message classification"
                    ),
                    _redacted_validation_reason(verdict_error),
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

    # =====================================================================
    # prime_sql_auth failure classifier (BLDX-1295)
    # =====================================================================

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
            return _error_from_failure_details(prime_result.failure)
        # Defence-in-depth: the fallback trusts a hand-built or pre-redaction-era
        # replayed output whose producer may not have redacted. Re-redact at this
        # trust boundary so a secret in error_message never reaches the verdict.
        return SqlApp._classify_probe_strings(
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
            return SqlApp._redact_typed_error(root)
        return SqlApp._classify_probe_strings(
            type(root).__name__, redact_secrets(str(root)), cause=root
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
            setattr(err, f.name, redact_wire_value(getattr(err, f.name)))
        return err

    @staticmethod
    def _classify_probe_strings(
        error_type: str,
        error_message: str,
        cause: BaseException | None = None,
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
          ``timeout expired``). It overrides ``AppTimeoutError``'s
          ``APP_OWNER`` default to USER: the only timeouts that can reach here
          are the driver's and the socket's, so the clock always ran out
          waiting for the customer's source.
        - ``SourceUnavailableError`` for network / DNS / TLS /
          connection-refused failures — the customer-owned source is
          presumed reachable but didn't respond cleanly, so the failure
          routes to the connector owner (USER), not Atlan infra ops.
        - ``InternalError`` as the last-resort fallback so we never
          return ``None`` and never accidentally swallow an unknown
          driver exception class.

        ``error_message`` must already be secret-redacted — it is copied
        verbatim into the returned error's evidence fields.
        """
        msg_lower = error_message.lower()

        # ODBC renders SQLSTATE HYT00 as "Login timeout expired" — no "timeout"
        # in the class name and no "timed out" in the message, so a generic
        # substring check misses the production CNCT-197 shape. Match the
        # driver phrasings explicitly.
        is_timeout = (
            "timeout" in error_type.lower()
            or "timed out" in msg_lower
            or "login timeout" in msg_lower
            or "timeout expired" in msg_lower
            or "hyt00" in msg_lower
        )
        if is_timeout:
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

        # Auth-class indicators: explicit class name OR driver-specific
        # message tokens (MySQL "Access denied" / code 1045, generic
        # "authentication failed"). Driver error_type for MySQL
        # ``Access denied`` is ``OperationalError`` — that class is
        # overloaded across both auth and network failures, so message
        # content has to be the tiebreaker.
        auth_class_hints = ("authentication", "accessdenied", "authfail")
        auth_msg_hints = (
            "access denied",
            "1045",
            "authentication failed",
            "authentication error",
            "login failed",
            "password authentication failed",
            "invalid credentials",
        )
        is_auth = any(h in error_type.lower() for h in auth_class_hints) or any(
            h in msg_lower for h in auth_msg_hints
        )
        if is_auth:
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

        # Network / DNS / TLS / connection-refused / generic
        # OperationalError without an auth-keyword message. Classify as
        # SourceUnavailableError (audience=USER) — the customer owns the
        # source system, and the actionable guidance is "fix the network
        # path, the source itself may be fine."
        network_class_hints = (
            "connectionerror",
            "connectionrefused",
            "gaierror",
            "sslerror",
            "oserror",
            "operationalerror",
            "interfaceerror",
            # Match wrapped SDK error classnames surfaced as error_type.
            # "dependencyunavailable" is retained for legacy wrappers that
            # predate the SourceUnavailableError migration.
            "sourceunavailable",
            "dependencyunavailable",
            "socketerror",
        )
        if any(h in error_type.lower() for h in network_class_hints):
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

    # =====================================================================
    # @task: Metadata extraction — SQL stream → raw JSONL
    # =====================================================================
    #
    # Each ``extract_*`` task streams its SQL result through
    # ``client.run_query`` (server-side cursor, ``_EXTRACT_BATCH_SIZE`` rows
    # per batch) and writes rows verbatim to ``raw/<entity>/records.json``
    # — one JSON object per line, no mapping. Mapping happens in
    # ``transform_*`` so the activity boundary is a durable retry point
    # and so future change-detection can compare raw streams between runs
    # before paying mapper cost.

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_databases(
        self, input: ExtractionTaskInput
    ) -> ExtractionTaskOutput:
        """Stream database/catalog rows from SQL into raw JSONL."""
        return await self._extract_entity(
            entity_type="database",
            sql_template=self.fetch_database_sql,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_schemas(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream schema rows from SQL into raw JSONL."""
        return await self._extract_entity(
            entity_type="schema",
            sql_template=self.fetch_schema_sql,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_tables(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream table rows from SQL into raw JSONL."""
        return await self._extract_entity(
            entity_type="table",
            sql_template=self.fetch_table_sql,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_columns(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream column rows from SQL into raw JSONL."""
        return await self._extract_entity(
            entity_type="column",
            sql_template=self.fetch_column_sql,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_views(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream view rows from SQL into raw JSONL."""
        return await self._extract_entity(
            entity_type="view",
            sql_template=self.fetch_view_sql,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_procedures(
        self, input: ExtractionTaskInput
    ) -> ExtractionTaskOutput:
        """Stream stored-procedure rows from SQL into raw JSONL.

        Writes to the ``extras-procedure`` entity type so the publish-app
        can parse the SQL ``definition`` field and derive lineage
        (Process / ColumnProcess entities) — matches the legacy Argo
        crawler output layout.
        """
        return await self._extract_entity(
            entity_type="extras-procedure",
            sql_template=self.fetch_procedure_sql,
            input=input,
        )

    # =====================================================================
    # @task: Per-entity transform — raw JSONL → mapper → transformed JSONL
    # =====================================================================
    #
    # Each ``transform_*`` task reads ``raw/<entity>/records.json`` line by
    # line, runs the dict through the connector's ``map_*`` function, and
    # writes the resulting Atlan asset to ``transformed/<entity>/entities.json``.
    # Activity boundary keeps transform retry-able without re-running the
    # SQL extraction.

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_databases(self, input: TransformInput) -> TransformOutput:
        """Map raw database records to Atlan assets via ``map_database``."""
        return await self._transform_entity(
            entity_type="database",
            mapper_fn=self.map_database,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_schemas(self, input: TransformInput) -> TransformOutput:
        """Map raw schema records to Atlan assets via ``map_schema``."""
        return await self._transform_entity(
            entity_type="schema",
            mapper_fn=self.map_schema,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_tables(self, input: TransformInput) -> TransformOutput:
        """Map raw table records to Atlan assets via ``map_table``."""
        return await self._transform_entity(
            entity_type="table",
            mapper_fn=self.map_table,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_columns(self, input: TransformInput) -> TransformOutput:
        """Map raw column records to Atlan assets via ``map_column``."""
        return await self._transform_entity(
            entity_type="column",
            mapper_fn=self.map_column,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_views(self, input: TransformInput) -> TransformOutput:
        """Map raw view records to Atlan assets via ``map_table``.

        Views go through ``map_table`` because Atlan models View as a
        Table specialisation (typeName ``View``) — connectors differentiate
        via the ``table_kind`` field on the record.
        """
        return await self._transform_entity(
            entity_type="view",
            mapper_fn=self.map_table,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_procedures(self, input: TransformInput) -> TransformOutput:
        """Map raw procedure records to Atlan assets via ``map_procedure``."""
        return await self._transform_entity(
            entity_type="extras-procedure",
            mapper_fn=self.map_procedure,
            input=input,
        )

    # =====================================================================
    # Asset mapper stubs — connectors override these
    # =====================================================================

    def map_database(
        self, record: dict[str, Any], connection_qn: str
    ) -> Asset | dict[str, Any]:
        """Map a raw database record to a pyatlan_v9 Asset. Override in subclass."""
        from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
            MapDatabaseUnimplementedError,
        )

        raise MapDatabaseUnimplementedError()

    def map_schema(
        self, record: dict[str, Any], connection_qn: str
    ) -> Asset | dict[str, Any]:
        """Map a raw schema record to a pyatlan_v9 Asset. Override in subclass."""
        from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
            MapSchemaUnimplementedError,
        )

        raise MapSchemaUnimplementedError()

    def map_table(
        self, record: dict[str, Any], connection_qn: str
    ) -> Asset | dict[str, Any]:
        """Map a raw table record to a pyatlan_v9 Asset. Override in subclass."""
        from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
            MapTableUnimplementedError,
        )

        raise MapTableUnimplementedError()

    def map_column(
        self, record: dict[str, Any], connection_qn: str
    ) -> Asset | dict[str, Any]:
        """Map a raw column record to a pyatlan_v9 Asset. Override in subclass."""
        from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
            MapColumnUnimplementedError,
        )

        raise MapColumnUnimplementedError()

    def map_procedure(
        self, record: dict[str, Any], connection_qn: str
    ) -> Asset | dict[str, Any]:
        """Map a raw procedure record to a pyatlan_v9 Asset. Override in
        subclass if your connector emits procedures via the
        ``transform_procedures()`` task. Without an override the task
        raises ``MapProcedureUnimplementedError`` rather than ``AttributeError``.
        """
        from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
            MapProcedureUnimplementedError,
        )

        raise MapProcedureUnimplementedError()

    # =====================================================================
    # run() — default orchestration
    # =====================================================================

    async def run(  # type: ignore[override]
        self, input: ExtractionInput
    ) -> ExtractionOutput:
        """Default extraction orchestration.

        1. Resolve credentials
        2. Extract metadata in parallel (databases, schemas, tables,
           columns) — each task streams SQL rows into raw JSONL under
           ``raw/<entity>/records.json``.
        3. Transform per-entity in parallel — each task reads the raw
           JSONL and runs records through the matching ``map_*`` function
           into ``transformed/<entity>/entities.json``.
        4. Upload to Atlan.

        Override for custom orchestration (e.g. sequential fetches, multi-DB).
        Use ``build_task_input()`` to construct typed inputs.
        """
        cred_ref = self._resolve_credential_ref(input)

        task_input = self.build_task_input(
            ExtractionTaskInput, input, cred_ref=cred_ref
        )

        # ── Phase 0: Pre-warm SQL auth cache (BLDX-1295) ────────────────
        # Argo parity fix. One serial probe connection BEFORE the
        # parallel ``extract_*`` burst so MySQL 8's
        # ``caching_sha2_password`` server-side cache is populated. The
        # subsequent parallel connections then take the fast-auth path
        # and never trip ``FAILED_LOGIN_ATTEMPTS`` lockouts.
        #
        # Failure handling: prime_sql_auth catches probe exceptions and
        # returns them as ``success=False`` on its output, carrying the
        # verdict it classified against the live exception. We rebuild that
        # typed error and raise it, so operators get actionable guidance for
        # the *actual* failure mode (not a one-size-fits-all auth-lockout
        # message — which would mislead an on-call investigating a DNS or TLS
        # misconfiguration). The parallel extract burst still never
        # runs on prime failure regardless of which error type we raise.
        prime_result = await self.prime_sql_auth(task_input)
        if not prime_result.success:
            raise self._classify_prime_failure(prime_result)

        # ── Phase 1: Extract — SQL rows → raw JSONL ─────────────────────
        db_result, schema_result, table_result, column_result = await asyncio.gather(
            self.extract_databases(task_input),
            self.extract_schemas(task_input),
            self.extract_tables(task_input),
            self.extract_columns(task_input),
        )

        logger.info(
            "Extraction complete: databases=%d, schemas=%d, tables=%d, columns=%d",
            db_result.total_record_count,
            schema_result.total_record_count,
            table_result.total_record_count,
            column_result.total_record_count,
        )

        # ── Phase 2: Transform — raw JSONL → mapper → transformed JSONL ──
        # Thread each extract's ``raw_file`` ``FileReference`` into the
        # matching transform's input. The activity interceptor:
        #   1. uploaded each ephemeral raw_file ref after the extract
        #      activity completed (now durable, with an object-store
        #      storage_path);
        #   2. will download the durable ref onto whichever worker pod
        #      runs the matching transform (with SHA-256 sidecar
        #      verification — handles the case where transform lands on
        #      a different pod than extract).
        # This is the BLDX-1281 cross-worker fix: no manual download_file
        # plumbing inside the transform, the framework does it.
        await asyncio.gather(
            self.transform_databases(
                self._build_transform_input(task_input, db_result.raw_file)
            ),
            self.transform_schemas(
                self._build_transform_input(task_input, schema_result.raw_file)
            ),
            self.transform_tables(
                self._build_transform_input(task_input, table_result.raw_file)
            ),
            self.transform_columns(
                self._build_transform_input(task_input, column_result.raw_file)
            ),
        )

        # ── Phase 3: ExtractionOutput ───────────────────────────────────
        # No explicit upload step — every ``raw_file`` / ``transformed_file``
        # ``FileReference`` emitted by the extract/transform tasks above
        # carries a pre-set canonical ``storage_path``
        # (``<run_prefix>/raw/<entity>/records.json`` /
        # ``<run_prefix>/transformed/<entity>/entities.json``), and the
        # activity interceptor's persist step has already uploaded each
        # one to that key. Publish discovers transformed assets by
        # walking ``transformed/<entity>/`` prefixes, so all the data
        # the downstream pipeline needs is already in object store
        # before this method returns. Subclasses that produce
        # side-files outside the FileReference contract should issue
        # their own ``App.upload(...)`` call (or override ``run()`` to
        # do so) — the template no longer auto-walks ``output_path``
        # because the walk was redundant with the canonical-key
        # uploads and didn't cross pod boundaries safely (BLDX-1281).

        connection_qn = ""
        if input.connection and input.connection.attributes:
            connection_qn = input.connection.attributes.qualified_name or ""

        # Derive the output path using the workflow context (run() is a Temporal
        # workflow method, not an activity — build_output_path() would fail with
        # "Not in activity context"). workflow.info() is always available here.
        # get_object_store_prefix() strips TEMPORARY_PATH to yield the S3 key.
        if input.output_path:
            resolved_base = input.output_path
        else:
            info = _temporal_workflow.info()
            resolved_base = os.path.join(
                TEMPORARY_PATH,
                WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                    application_name=AppRegistry.resolve_running_app_name(
                        prefer=self._app_name
                    ),
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                ),
            )

        return ExtractionOutput(
            databases_extracted=db_result.total_record_count,
            schemas_extracted=schema_result.total_record_count,
            tables_extracted=table_result.total_record_count,
            columns_extracted=column_result.total_record_count,
            connection_qualified_name=connection_qn,
            transformed_data_prefix=get_object_store_prefix(
                os.path.join(resolved_base, "transformed")
            ),
            # Expose the resolved local base path so subclasses can derive
            # additional prefixes (e.g. lineage-specific dirs) without calling
            # workflow.info() a second time.
            output_path=resolved_base,
        )

    # =====================================================================
    # Internal helpers
    # =====================================================================

    @staticmethod
    def _build_transform_input(
        base: ExtractionTaskInput,
        raw_file: FileReference | None,
    ) -> TransformInput:
        """Build a ``TransformInput`` from ``base`` carrying ``raw_file``.

        Called by ``run()`` to thread the durable ``FileReference``
        returned by each ``extract_*`` activity (as
        ``ExtractionTaskOutput.raw_file``) into the matching
        ``transform_*`` activity's input. The resulting ``TransformInput``
        inherits every field from ``base`` plus the new ``raw_file``
        ref; the activity interceptor will materialise the ref on
        whichever worker pod ends up running the transform, with
        SHA-256 sidecar verification — see ``storage.file_ref_sync``.
        """
        return TransformInput(
            **base.model_dump(exclude_none=False),
            raw_file=raw_file,
        )

    @staticmethod
    def _resolve_output_path(input: ExtractionTaskInput) -> str:
        """Resolve output_path — auto-set from build_output_path() in activity context."""
        if not input.output_path:
            resolved = build_output_path()
            logger.info("Auto-resolved output_path: %s", resolved)
            return os.path.join(TEMPORARY_PATH, resolved)
        return input.output_path

    async def _init_sql_client(self, input: ExtractionTaskInput) -> BaseSQLClient:
        """Initialize and return a SQL client from credentials.

        ``ExtractionTaskInput`` only carries ``credential_ref`` / ``credential_guid``
        (no ``agent_json`` / ``extraction_method``). The SDR/direct routing
        decision happens upstream in ``_resolve_credential_ref(ExtractionInput)``
        which populates ``credential_ref`` on the task input via
        ``build_task_input``. So here we just consume that ref.
        """
        if self.sql_client_class is None:
            from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
                SqlClientClassNotSetError,
            )

            raise SqlClientClassNotSetError()

        client = self.sql_client_class()
        creds: dict[str, Any] = {}

        ref: CredentialRef | None = input.credential_ref
        if ref is None and input.credential_guid:
            ref = legacy_credential_ref(input.credential_guid)

        if ref is not None:
            infra = get_infrastructure()
            secret_store = infra.secret_store if infra else None
            if secret_store:
                resolver = CredentialResolver(secret_store)
                creds = await resolver.resolve_raw(ref) or {}

        await client.load(credentials=creds)
        return client

    def _prepare_sql(self, sql: str, input: ExtractionTaskInput) -> str:
        """Substitute filter placeholders in SQL template."""
        exclude_filter = input.exclude_filter or ""
        include_filter = input.include_filter or ""

        # Handle dict filters (from AE) — normalize to regex
        if isinstance(exclude_filter, dict):
            normalized = normalize_filters(exclude_filter, False)
            exclude_regex = "|".join(normalized) if normalized else "^$"
        else:
            exclude_regex = exclude_filter or "^$"

        if isinstance(include_filter, dict):
            normalized = normalize_filters(include_filter, True)
            include_regex = "|".join(normalized) if normalized else ".*"
        else:
            include_regex = include_filter or ".*"

        # Temp table regex
        temp_table_sql = ""
        if hasattr(input, "temp_table_regex") and input.temp_table_regex:
            if self.extract_temp_table_regex_table_sql:
                temp_table_sql = self.extract_temp_table_regex_table_sql.replace(
                    "{exclude_table_regex}", input.temp_table_regex
                )

        # Single-pass substitution via safe_substitute_placeholders prevents
        # cascading: three chained str.replace() calls processed the mutating
        # string, so a replacement value containing another placeholder's text
        # would be re-substituted by the next call.  safe_substitute_placeholders
        # scans the original positions exactly once — replacement values are
        # never re-scanned (APP-2291).
        return safe_substitute_placeholders(
            sql,
            {
                "{normalized_exclude_regex}": exclude_regex,
                "{normalized_include_regex}": include_regex,
                "{temp_table_regex_sql}": temp_table_sql,
            },
        )

    def _resolve_credential_ref(self, input: ExtractionInput) -> CredentialRef | None:
        """Resolve credential ref from extraction input.

        Delegates to :meth:`CredentialRef.resolve_or_none` — prefers the input's
        own ``credential_ref``, routes direct (credential_guid) / agent
        (agent_json) modes, and degrades to a legacy GUID ref or ``None`` on a
        routing edge case. Shared with the injected preflight gate so both paths
        route identically.
        """
        return CredentialRef.resolve_or_none(input)

    async def _extract_entity(
        self,
        *,
        entity_type: str,
        sql_template: str,
        input: ExtractionTaskInput,
    ) -> ExtractionTaskOutput:
        """Stream SQL rows verbatim into ``raw/<entity>/records.json`` JSONL.

        1. Open SQL client, render the template with the input filters.
        2. Stream rows in batches of ``_EXTRACT_BATCH_SIZE`` via the
           server-side cursor that ``client.run_query`` exposes — each
           batch is a ``list[dict[str, Any]]`` keyed by lower-cased column
           names. Memory stays bounded regardless of total result size.
        3. Write each row to JSONL. Mapping is deliberately *not* done
           here — it runs in the matching ``transform_*`` activity so the
           activity boundary stays a durable retry point and so future
           change-detection can compare raw streams between runs.
        """
        if not sql_template:
            logger.warning("No SQL configured for %s — skipping", entity_type)
            return ExtractionTaskOutput(typename=entity_type, total_record_count=0)

        output_path = self._resolve_output_path(input)
        if not output_path:
            return ExtractionTaskOutput(typename=entity_type, total_record_count=0)

        output_dir = Path(output_path) / "raw" / entity_type
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "records.json"

        client = await self._init_sql_client(input)
        count = 0
        try:
            sql = self._prepare_sql(sql_template.strip(), input)

            def _serialize_batch(batch: list[dict[str, Any]]) -> bytes:
                return b"".join(
                    orjson.dumps(
                        record,
                        default=_orjson_default,
                        option=orjson.OPT_APPEND_NEWLINE,
                    )
                    for record in batch
                )

            with open(output_file, "wb") as f:
                async for batch in client.run_query(
                    sql, batch_size=_EXTRACT_BATCH_SIZE
                ):
                    # Only the serialisation is offloaded — 10k orjson.dumps
                    # calls per batch is the part that outlasts the heartbeat
                    # interval on wide rows (ADR-0010). The write stays on the
                    # loop as one buffered call, which costs microseconds.
                    #
                    # Deliberately NOT passing `f` into the thread: if the
                    # activity is cancelled at this await, the `with` block
                    # unwinds and closes the handle while a worker thread is
                    # still writing to it — and per ADR-0010 that thread cannot
                    # be killed, so it would write on into a closed (or
                    # retry-reopened) file. Serialise off-loop, write on-loop:
                    # the handle never crosses the thread boundary, so the
                    # single-handle streaming loop stays cancellation-safe.
                    payload = await run_in_thread(_serialize_batch, batch)
                    f.write(payload)
                    count += len(batch)
                    # One fetched page serialised and written is one observable
                    # unit (ADR-0018). Marked here rather than per record: one
                    # page is already the unit, and a mark per record would be a
                    # hot-path cost for no extra signal.
                    #
                    # This is the whole reason the read side needs no hold — the
                    # fetch → serialise → write cycle marks on every page, so
                    # only a *single* cycle slower than max_no_progress_seconds
                    # needs an explicit hold from the app. (FND-290's auto-hold
                    # around the `run_in_thread` above already covers the
                    # serialise leg of that cycle.)
                    current_progress_tracker().mark_progress("extract.page")
        finally:
            await client.close()

        logger.info("Extracted %s: %d raw records", entity_type, count)
        # Emit a ``FileReference`` so the activity interceptor uploads the
        # raw JSONL to the object store after the extract activity
        # completes and marks the ref durable. ``run()`` then threads
        # this durable ref into the matching transform's
        # ``TransformInput`` — the interceptor re-downloads it onto
        # whichever worker pod picks up the transform (with SHA-256
        # sidecar verification, so a transform that lands on a
        # different pod than the extract still sees a verified-fresh
        # local copy). This is how cross-worker fault tolerance for
        # the raw → transform handoff is provided without any explicit
        # ``download_file`` plumbing in the template (BLDX-1281).
        #
        # ``storage_path`` is pinned to the canonical run-scoped key
        # ``<run_prefix>/raw/<entity>/records.json`` rather than the
        # default UUID-named ``<run_prefix>/file_refs/<uuid>.json``.
        # ``get_object_store_prefix`` strips ``TEMPORARY_PATH`` from the
        # local path to derive the matching object-store key — same
        # shape ``upload_to_atlan``'s legacy directory walk would have
        # produced. Pinning the key on the ref itself makes the upload
        # work even when ``upload_to_atlan`` runs on a different pod
        # than this extract and finds the local FS empty (the
        # cross-pod failure mode that caused silent asset archival in
        # production — publish reads from canonical paths and would
        # miss any entity whose transformed-side upload only landed
        # under ``file_refs/<uuid>.json``).
        #
        # TRANSIENT tier — the raw file is purely intermediate
        # (extract → transform within the same run); ``cleanup_storage``
        # auto-deletes tracked TRANSIENT refs at run end.
        raw_ref = (
            FileReference(
                local_path=str(output_file),
                storage_path=get_object_store_prefix(str(output_file)),
                tier=StorageTier.TRANSIENT,
            )
            if count > 0
            else None
        )
        return ExtractionTaskOutput(
            typename=entity_type,
            total_record_count=count,
            raw_file=raw_ref,
        )

    async def _transform_entity(
        self,
        *,
        entity_type: str,
        mapper_fn: _MapperFn,
        input: TransformInput,
    ) -> TransformOutput:
        """Read raw JSONL → mapper → transformed JSONL.

        Reads the raw records.json via the ``FileReference`` threaded
        through from the matching ``extract_*`` activity, runs each
        record through ``mapper_fn``, and writes the resulting Atlan
        asset to ``transformed/<entity>/entities.json``.

        Cross-worker contract (BLDX-1281 / PR #1787):
            ``run()`` populates ``input.raw_file`` with the durable
            ``FileReference`` returned by the matching extract activity.
            The activity interceptor materialises that ref onto this
            worker's local FS BEFORE this method runs (SHA-256 sidecar
            verification → fresh local copy even if extract and
            transform landed on different pods). The method below just
            opens ``input.raw_file.local_path`` directly.

            When ``input.raw_file is None`` (extract returned zero rows
            or the caller didn't thread a ref), this is a clean no-op
            returning ``total_record_count=0`` — matches the historical
            contract that the publish step relies on.
        """
        output_path = self._resolve_output_path(input)
        if not output_path:
            return TransformOutput(typename=entity_type, total_record_count=0)

        # Resolve the raw file via the threaded FileReference. The
        # interceptor has already materialised it; we just consume the
        # local_path. Fall back to the legacy local path lookup so
        # callers that don't thread a ref (e.g. unit tests that seed
        # raw files directly under tmp_path) still work.
        if input.raw_file is not None and input.raw_file.local_path:
            raw_file = Path(input.raw_file.local_path)
        else:
            raw_file = Path(output_path) / "raw" / entity_type / "records.json"
        if not raw_file.exists() or raw_file.stat().st_size == 0:
            return TransformOutput(typename=entity_type, total_record_count=0)

        connection_qn = ""
        connection_name = ""
        if hasattr(input, "connection") and input.connection:
            attrs = getattr(input.connection, "attributes", None)
            if attrs:
                connection_qn = getattr(attrs, "qualified_name", "") or ""
                connection_name = getattr(attrs, "name", "") or ""

        output_dir = Path(output_path) / "transformed" / entity_type
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "entities.json"

        def _map_records() -> int:
            written = 0
            with open(raw_file, "rb") as r, open(output_file, "wb") as w:
                for line in r:
                    line = line.strip()
                    if not line:
                        continue
                    record = orjson.loads(line)
                    asset = mapper_fn(record, connection_qn)
                    # Inject connectionName if the mapper returned a dict
                    if isinstance(asset, dict) and connection_name:
                        asset.setdefault("attributes", {}).setdefault(
                            "connectionName", connection_name
                        )
                    if hasattr(asset, "to_nested_dict"):
                        payload = asset.to_nested_dict()
                    elif hasattr(asset, "model_dump"):
                        payload = asset.model_dump()
                    elif isinstance(asset, dict):
                        payload = asset
                    else:
                        payload = record
                    w.write(orjson.dumps(payload, default=_orjson_default))
                    w.write(b"\n")
                    written += 1
            return written

        # Offloaded as one hop: this maps every raw record for the entity —
        # JSON parse, the connector's mapper_fn (which builds pyatlan asset
        # objects), serialise, write — with no await anywhere in the loop.
        # A large table's records.json therefore holds the event loop for the
        # whole transform, which stops the auto-heartbeat and gets a healthy
        # activity killed on heartbeat_timeout. Every SQL connector inherits
        # this method, so the fix applies fleet-wide (ADR-0010).
        #
        # No framework progress hook inside `_map_records`, deliberately
        # (ADR-0018). The loop is line-by-line with no batch boundary to mark
        # at, and the never-per-record rule forbids the only boundary it has.
        # It does not need one: the whole map is a single `run_in_thread` hop,
        # which FND-290 wraps in an unbounded auto-hold — so the stall watchdog
        # is vouched for across the entire transform by mechanism 2 rather than
        # mechanism 1. That leaves the 24h backstop as its only bound, which is
        # the ADR's accepted residual for opaque offloaded work and is exactly
        # what warn mode is meant to surface with an observed duration.
        count = await run_in_thread(_map_records)

        logger.info("Transformed %s: %d records", entity_type, count)
        # Emit a FileReference to entities.json so the activity
        # interceptor uploads it after the transform activity finishes.
        # Downstream publish / upload tasks then consume the durable ref
        # via the same materialise contract — no local-FS coupling.
        #
        # ``storage_path`` is pinned to the canonical
        # ``<run_prefix>/transformed/<entity>/entities.json`` so the
        # downstream publish step finds the file at the entity-typed
        # path it expects. Without this pin the interceptor would
        # auto-generate a ``file_refs/<uuid>.json`` key — publish
        # discovers transformed assets by walking
        # ``transformed/<entity>/`` prefixes, so anything that only
        # lands under ``file_refs/`` is invisible to it and the
        # entity gets archived as "removed from source" on the next
        # publish run. ``get_object_store_prefix`` strips
        # ``TEMPORARY_PATH`` from the local path to derive the
        # matching object-store key; see ``_extract_entity`` for the
        # corresponding canonical key on the raw side.
        #
        # Tier = RETAINED (vs. TRANSIENT on raw_file): the
        # transform → publish handoff can span an SDR → in-tenant
        # deployment boundary, so the ref must survive the SDR-side
        # workflow's auto-cleanup at run end. RETAINED keeps the
        # object-store key under the run-scoped ``artifacts/`` prefix
        # but skips ``cleanup_storage``'s tracked-TRANSIENT sweep.
        transformed_ref = (
            FileReference(
                local_path=str(output_file),
                storage_path=get_object_store_prefix(str(output_file)),
                tier=StorageTier.RETAINED,
            )
            if count > 0
            else None
        )
        return TransformOutput(
            typename=entity_type,
            total_record_count=count,
            transformed_file=transformed_ref,
        )
