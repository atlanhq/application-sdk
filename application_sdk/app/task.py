"""Task decorator for defining activities within Apps.

Tasks are operations with external side effects that need to be
executed as Temporal activities for durability and retry support.

Like Apps, Tasks follow the single-dataclass contract pattern:
- One Input dataclass parameter (extending Input base class)
- One Output dataclass return value (extending Output base class)

This ensures type safety, proper serialization, and backwards compatibility.

Tasks support heartbeating for long-running operations:
- Auto-heartbeating sends periodic signals to Temporal (default: every 10s)
- Manual heartbeating allows progress tracking for resume on retry
- If heartbeats stop, Temporal restarts the activity (default: after 60s)
"""

import inspect
import math
import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    NoReturn,
    TypeVar,
    cast,
    get_type_hints,
    overload,
)

from application_sdk.common._env import env_int
from application_sdk.contracts.base import Input, Output
from application_sdk.errors import CONTRACT_VALIDATION, ErrorCode
from application_sdk.errors.leaves import InvalidInputError
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.execution.progress import ProgressWatchdogMode
    from application_sdk.execution.retry import RetryPolicy

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Sentinel for "use default" - allows None to mean "disable"
_USE_DEFAULT = object()

#: The ``start_to_close`` backstop, in seconds (ADR-0018 → *Rollout* step 3).
#:
#: 24 hours, and deliberately not a number anyone is meant to tune. Before this,
#: the default was 600s and every app overrode it with a guess — 2h, 6h, a
#: "weight class" — because the question it asks (*how long should this whole
#: activity take?*) scales with tenant size and is unanswerable at the desk. A
#: backstop asks nothing: it is the last-resort bound for an attempt that
#: something else should already have caught.
#:
#: What makes it safe to raise is the stall watchdog landing in the same
#: release: a wedged-but-alive attempt is now *detected* one
#: ``max_no_progress_seconds`` in, and killed there wherever an app enforces.
#: The regression this accepts, in warn mode, is that a wedge is contained by an
#: alert and a human rather than by the small ceiling that used to kill it
#: accidentally — see ADR-0018 → *Migration*.
_START_TO_CLOSE_BACKSTOP_SECONDS = 86_400

# Env-var-driven defaults for @task timeouts. Read once at import time so the
# value is stable for the process lifetime (same pattern as constants.py).
# Apps that need a different per-task value pass it explicitly to @task().
_DEFAULT_HEARTBEAT_TIMEOUT_SECONDS: int = env_int("ATLAN_HEARTBEAT_TIMEOUT_SECONDS", 60)
_DEFAULT_TIMEOUT_SECONDS: int = env_int(
    "ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS", _START_TO_CLOSE_BACKSTOP_SECONDS
)


def _validate_watchdog_declaration(
    progress_watchdog: "ProgressWatchdogMode | str | None",
    max_no_progress_seconds: float | None,
) -> "tuple[ProgressWatchdogMode | None, float | None]":
    """Validate an explicitly declared watchdog mode and allowance.

    Only reached when the author passed one, which is what lets this module stay
    free of an ``application_sdk.execution`` import at module scope: ``app/``
    sits *below* ``execution/`` in the import graph (``execution/__init__`` →
    ``_temporal`` → ``app.registry`` → ``app.base`` → this module), so importing
    the enum eagerly here deadlocks the very first import of the SDK. Undeclared
    tasks carry ``None`` all the way to the activity, which resolves the
    fleet-wide default in a layer that *can* see the enum — see
    :func:`~application_sdk.execution.progress.resolve_watchdog_mode`.

    Args:
        progress_watchdog: The declared mode — a :class:`ProgressWatchdogMode`,
            or one of its string values.
        max_no_progress_seconds: The declared allowance in seconds.

    Returns:
        The pair, with the mode coerced to a :class:`ProgressWatchdogMode`.

    Raises:
        TaskContractError: If either value is unusable. Raised at *decoration*
            time rather than swallowed, unlike the env-var readers next door: a
            typo in one deployment manifest must not stop a worker booting, but
            a typo in an app's own source is a bug its author should see at
            import.
    """
    from application_sdk.execution.progress import (  # noqa: PLC0415 — app/ cannot import execution/ at module scope; see the docstring
        ProgressWatchdogMode,
    )

    mode: ProgressWatchdogMode | None = None
    if progress_watchdog is not None:
        try:
            mode = ProgressWatchdogMode(progress_watchdog)
        except ValueError:
            raise TaskContractError(
                f"progress_watchdog={progress_watchdog!r} is not a valid mode. "
                f"Use one of {', '.join(repr(m.value) for m in ProgressWatchdogMode)} "
                "(or the ProgressWatchdogMode enum). Omit it to inherit the "
                "fleet-wide default from ATLAN_PROGRESS_WATCHDOG."
            ) from None

    budget: float | None = None
    if max_no_progress_seconds is not None:

        def _reject_allowance() -> NoReturn:
            """Both ways an allowance can be unusable get the same message.

            A closure rather than a pre-built exception because
            ``TaskContractError.__init__`` warns on construction, and rather than
            a NaN sentinel threaded to a later branch because a non-numeric
            allowance should visibly raise where it is detected.
            """
            raise TaskContractError(
                f"max_no_progress_seconds={max_no_progress_seconds!r} must be a "
                "finite positive number of seconds. A zero or negative allowance "
                "makes every attempt stall on its first watchdog tick; pass "
                "progress_watchdog='off' to disable the watchdog deliberately, or "
                "omit this to inherit the fleet-wide allowance."
            ) from None

        try:
            budget = float(max_no_progress_seconds)
        except (TypeError, ValueError):
            _reject_allowance()
        if not math.isfinite(budget) or budget <= 0:
            _reject_allowance()

    return mode, budget


def _load_default_schedule_to_close_seconds() -> int | None:
    """Read the fleet-wide total-time ceiling. ``None`` when unset or unusable.

    ``0`` reads as unset, so a deployment can clear an inherited value the same
    way it sets one. Never raises, and never lets a bad value reach a
    decoration: a negative ceiling would make every ``@task`` in the process
    raise at import, so a typo in one deployment manifest would stop the worker
    booting rather than cost one config value.
    """
    raw = env_int("ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS", 0)
    if raw > 0:
        return raw
    if raw < 0:
        logger.warning(
            "Ignoring ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS=%d: a total-time "
            "ceiling must be positive. Leaving the retry product unbounded "
            "(start_to_close bounds one attempt, the retry policy multiplies it)",
            raw,
        )
    return None


#: The fleet-wide ceiling on a task's *total* time across every retry (ADR-0018
#: → *Bounding total time*). ``None`` — the SDK default — leaves the retry
#: product unbounded, exactly as before this knob existed: ``start_to_close``
#: bounds one attempt and the retry policy multiplies it.
#:
#: An env var first and a decorator argument second, on purpose. ADR-0018's
#: accepted position is to *start* unbounded and size the ceiling from warn-mode
#: data rather than guess it up front; making the fleet-wide decision an env var
#: is what keeps that decision a config change instead of a redesign.
_DEFAULT_SCHEDULE_TO_CLOSE_SECONDS: int | None = (
    _load_default_schedule_to_close_seconds()
)

# Type alias for methods with single Input param returning Output
TaskMethod = Callable[..., Any]


@dataclass(kw_only=True)
class UnresolvableTaskAnnotationsError(InvalidInputError):
    """Task has string annotations that cannot be resolved at decoration time."""

    code: ClassVar[str] = "INVALID_INPUT_TASK_UNRESOLVABLE_ANNOTATIONS"
    field: str | None = "annotations"


class TaskContractError(InvalidInputError):
    """Deprecated: use ``application_sdk.errors.InvalidInputError`` — removed in v4.0."""

    code: ClassVar[str] = "INVALID_INPUT_TASK_CONTRACT"

    def __init__(self, message: str, *, error_code: ErrorCode | None = None) -> None:
        warnings.warn(
            "TaskContractError is deprecated; use application_sdk.errors.InvalidInputError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        InvalidInputError.__init__(self, message=message)
        self._legacy_error_code = error_code or CONTRACT_VALIDATION

    @property
    def error_code(self) -> ErrorCode:
        return self._legacy_error_code

    def __str__(self) -> str:
        return f"[{self._legacy_error_code}] {self.message}"


@dataclass
class TaskMetadata:
    """Metadata about a registered task.

    Tasks are private to their parent App and become
    Temporal activities with simple names (just the task name).
    """

    name: str
    """Name of the task (method name by default)."""

    func: Callable[..., Any]
    """The original function/method."""

    input_type: type[Input]
    """The Input dataclass type for this task."""

    output_type: type[Output]
    """The Output dataclass type for this task."""

    app_name: str = ""
    """Parent app name (set by @app decorator)."""

    description: str = ""
    """Human-readable description."""

    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
    """Default timeout for this task. Defaults to ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS
    env var, or 86400s (24 hours) if unset.

    A **backstop**, not a duration budget (ADR-0018): the last-resort bound on an
    attempt that the stall watchdog should already have caught. Do not tune it —
    the question it asks scales with tenant size, which is what made it
    unguessable. To bound a wedge in *minutes* rather than at the backstop,
    declare holds and set :attr:`progress_watchdog` to ``enforce``.

    Bounds ONE attempt. The retry policy multiplies it — see
    :attr:`schedule_to_close_seconds` for the ceiling on the product."""

    schedule_to_close_seconds: int | None = _DEFAULT_SCHEDULE_TO_CLOSE_SECONDS
    """Ceiling on this task's total time across every retry, in seconds.

    ``None`` leaves the retry product unbounded: ``max_attempts ×
    timeout_seconds`` is the real worst case. Defaults to
    ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS, and to ``None`` when that env var is
    unset or ``0``.

    Resolved against :attr:`timeout_seconds` by
    :func:`~application_sdk.execution.retry.resolve_activity_time_bounds`, which
    both dispatch paths share."""

    pool: str | None = None
    """Logical worker-pool name for this task.
    When set, the framework routes this activity to the task queue registered
    for that pool via ``ATLAN_POOL_<POOL>_QUEUE``. Tasks without a pool run
    on the workflow's own task queue (default behavior). Must match a key
    declared in the app's pkl contract ``pools { ["name"] = new Pool { … } }``.
    """

    retry_policy: "RetryPolicy | None" = field(default=None, compare=False)
    """Full retry policy for this task. When provided, takes precedence over
    retry_max_attempts and retry_max_interval_seconds."""

    retry_max_attempts: int = 3
    """Maximum retry attempts for this task. Ignored when retry_policy is set."""

    retry_max_interval_seconds: int = 30
    """Maximum interval between retries in seconds. Caps exponential backoff
    to prevent very long waits between retries. Default: 30 seconds.
    Ignored when retry_policy is set."""

    heartbeat_timeout_seconds: int | None = _DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
    """Heartbeat timeout in seconds. If no heartbeat is received within this
    window, Temporal will consider the activity dead and restart it.
    Set to None to disable heartbeating entirely (legacy behavior).
    Defaults to ATLAN_HEARTBEAT_TIMEOUT_SECONDS env var, or 60 seconds if unset."""

    auto_heartbeat_seconds: int | None = 10
    """Auto-heartbeat interval in seconds. The framework will automatically
    send heartbeats at this interval in a background task.
    Set to None to disable auto-heartbeating (use manual heartbeats only).
    Should be less than heartbeat_timeout_seconds (recommended: 1/6 of timeout).
    Default: 10 seconds."""

    progress_watchdog: "ProgressWatchdogMode | None" = None
    """How the stall watchdog reacts to a no-progress gap on this task.

    ``None`` — the default — means *this task declares nothing*, and the
    fleet-wide default applies: ``warn`` unless ``ATLAN_PROGRESS_WATCHDOG`` says
    otherwise. It is deliberately not ``ProgressWatchdogMode.WARN`` here: the
    absence of a declaration and a declaration of ``warn`` are different facts,
    and only the first one follows an operator turning the fleet ``off``.

    Resolved against the process-wide setting by
    :func:`~application_sdk.execution.progress.resolve_watchdog_mode` in the
    activity, which is the layer that runs the watchdog and therefore the layer
    whose env the kill-switch has to read."""

    max_no_progress_seconds: float | None = None
    """How long this task may go without an observable progress signal, in seconds.

    ``None`` inherits the fleet-wide allowance (``ATLAN_MAX_NO_PROGRESS_SECONDS``,
    900s by default). Roughly app-independent on purpose: it answers *"how long
    may this attempt be silent?"*, not *"how much data does this tenant have?"* —
    so a task that legitimately goes quiet for a long time wants a hold at that
    call site (:func:`~application_sdk.execution.progress.holding_progress`),
    not a bigger number here."""


def _validate_task_signature(
    fn: Callable[..., Any],
) -> tuple[type[Input], type[Output]]:
    """Validate and extract Input/Output types from a task method.

    Tasks must follow the single-dataclass contract pattern:
    - Exactly one parameter (besides self) extending Input
    - Return type extending Output

    Args:
        fn: The task function to validate.

    Returns:
        Tuple of (input_type, output_type).

    Raises:
        TaskContractError: If the signature is invalid.
    """
    # Get function name safely (Callable doesn't guarantee __name__)
    fn_name = getattr(fn, "__name__", repr(fn))

    # Get function signature
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())

    # Remove 'self' parameter if present (method)
    if params and params[0].name == "self":
        params = params[1:]

    # Must have exactly one parameter
    if len(params) != 1:
        raise TaskContractError(
            f"Task '{fn_name}' must have exactly one parameter (extending Input), "
            f"got {len(params)} parameters. "
            f"Wrap multiple values in a single Input dataclass."
        )

    # Get type hints.
    # get_type_hints() resolves string annotations (from 'from __future__ import
    # annotations' or explicit string literals) using the function's module globals.
    # Fall back to fn.__annotations__ directly when that resolution fails — this
    # handles the common case where Input/Output types are locally-scoped (e.g. inside
    # a test function) and were never string-ified because 'from __future__' was NOT
    # used; in that case __annotations__ already holds the real type objects.
    # If the annotations are strings that cannot be resolved, raise a clear error.
    try:
        hints = get_type_hints(fn)
    except NameError:
        raw: dict[str, Any] = getattr(fn, "__annotations__", {})
        unresolvable = [k for k, v in raw.items() if isinstance(v, str)]
        if unresolvable:
            raise UnresolvableTaskAnnotationsError(
                message=(
                    f"Task '{fn_name}' has unresolvable annotations for {unresolvable}. "
                    "This usually happens when 'from __future__ import annotations' is "
                    "used alongside Input/Output types that are not defined at module "
                    "level. Move the type definitions to module scope (before the App "
                    "class) or remove 'from __future__ import annotations'."
                ),
            ) from None
        hints = raw

    # Validate input type
    param = params[0]
    input_type = hints.get(param.name)
    if input_type is None:
        raise TaskContractError(
            f"Task '{fn_name}' parameter '{param.name}' must have a type annotation "
            f"extending Input."
        )

    # Check input extends Input base class
    if not (isinstance(input_type, type) and issubclass(input_type, Input)):
        raise TaskContractError(
            f"Task '{fn_name}' parameter '{param.name}' must extend Input base class, "
            f"got {input_type}. Define a dataclass that extends Input."
        )

    # Validate return type
    output_type = hints.get("return")
    if output_type is None:
        raise TaskContractError(
            f"Task '{fn_name}' must have a return type annotation extending Output."
        )

    # Check output extends Output base class
    if not (isinstance(output_type, type) and issubclass(output_type, Output)):
        raise TaskContractError(
            f"Task '{fn_name}' return type must extend Output base class, "
            f"got {output_type}. Define a dataclass that extends Output."
        )

    return input_type, output_type


@overload
def task(func: F) -> F: ...


@overload
def task(
    func: None = None,
    *,
    name: str | None = None,
    description: str = "",
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    schedule_to_close_seconds: int | None | object = _USE_DEFAULT,
    retry_policy: "RetryPolicy | None" = None,
    retry_max_attempts: int = 3,
    retry_max_interval_seconds: int = 30,
    heartbeat_timeout_seconds: int | None | object = _USE_DEFAULT,
    auto_heartbeat_seconds: int | None | object = _USE_DEFAULT,
    pool: str | None = None,
    progress_watchdog: "ProgressWatchdogMode | str | None" = None,
    max_no_progress_seconds: float | None = None,
) -> Callable[[F], F]: ...


def task(
    func: F | None = None,
    *,
    name: str | None = None,
    description: str = "",
    timeout_seconds: int | object = _USE_DEFAULT,
    schedule_to_close_seconds: int | None | object = _USE_DEFAULT,
    retry_policy: "RetryPolicy | None" = None,
    retry_max_attempts: int = 3,
    retry_max_interval_seconds: int = 30,
    heartbeat_timeout_seconds: int | None | object = _USE_DEFAULT,
    auto_heartbeat_seconds: int | None | object = _USE_DEFAULT,
    pool: str | None = None,
    progress_watchdog: "ProgressWatchdogMode | str | None" = None,
    max_no_progress_seconds: float | None = None,
) -> F | Callable[[F], F]:
    """Decorator to mark a method as a task (Temporal activity).

    Tasks follow the single-dataclass contract pattern (like Apps):
    - Exactly one Input parameter (dataclass extending Input)
    - Exactly one Output return type (dataclass extending Output)

    This ensures type safety, proper serialization, and backwards compatibility.

    Tasks are PRIVATE to the app and cannot be called from other apps.
    They are only callable via `self.task_name()` within the app's methods.

    Each task becomes a distinct named Temporal activity for observability.

    Heartbeating:
        Tasks support heartbeating for long-running operations. By default,
        the framework sends heartbeats every 10 seconds, and Temporal will
        restart the activity if no heartbeat is received for 60 seconds.

        IMPORTANT: Auto-heartbeats only work when the event loop yields.
        For blocking operations (requests.get, file I/O, pandas operations),
        use self.task_context.run_in_thread() to keep heartbeats alive.

    Example::

        @dataclass
        class FetchInput(Input):
            endpoint: str
            timeout: int = 30

        @dataclass
        class FetchOutput(Output):
            data: dict[str, Any]
            status_code: int

        class MyPipeline(App):

            @task
            async def read_from_api(self, input: FetchInput) -> FetchOutput:
                '''Fetch data from external API.'''
                response = await http_client.get(input.endpoint)
                return FetchOutput(data=response.json(), status_code=response.status)

            @task(timeout_seconds=1800)  # 30 min timeout, uses default heartbeat
            async def write_to_database(self, input: WriteInput) -> WriteOutput:
                '''Write records to database.'''
                count = await db.bulk_insert(input.records)
                return WriteOutput(count=count)

            async def run(self, input: PipelineInput) -> PipelineOutput:
                fetch_result = await self.read_from_api(
                    FetchInput(endpoint=input.endpoint)
                )
                return PipelineOutput(count=fetch_result.status_code)

    Args:
        func: The function to decorate (when used without parentheses).
        name: Override the task name (defaults to function name).
        description: Human-readable description.
        timeout_seconds: Activity ``start_to_close`` bound in seconds — the
            **backstop** on one attempt, which the retry policy then multiplies.
            Defaults to ``ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS`` env var, or
            86400 s (24 h) if unset. Explicit values take precedence over the
            env var, but ADR-0018's position is that this is no longer a number
            worth tuning: a wedged-but-alive attempt is caught by the stall
            watchdog in minutes, and a legitimately long one should not die at a
            guessed ceiling.
        schedule_to_close_seconds: Ceiling on the task's **total** time across
            every attempt, in seconds. ``None`` — the default — leaves the retry
            product unbounded, so the real worst case is
            ``retry_max_attempts × timeout_seconds``; see
            :func:`~application_sdk.execution.retry.retry_product_seconds` to
            declare exactly that product, and ``retry_max_attempts=1`` to bound
            a backstop-class task by dropping retries instead. A ceiling below
            ``timeout_seconds`` caps one attempt too, so declaring only a total
            works while inheriting a generous per-attempt backstop. Defaults to
            ``ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS`` when that env var is set
            to a positive value; passing ``None`` explicitly opts this task out
            of that fleet-wide ceiling.
        retry_policy: Full retry policy. When provided, takes precedence over
            retry_max_attempts and retry_max_interval_seconds.
        retry_max_attempts: Maximum retry attempts (default 3). Ignored when
            retry_policy is provided.
        retry_max_interval_seconds: Maximum interval between retries in seconds.
            Caps exponential backoff to prevent very long waits. Default: 30 seconds.
            Ignored when retry_policy is provided.
        heartbeat_timeout_seconds: Heartbeat timeout in seconds — if no heartbeat
            is received within this window, Temporal restarts the activity. Set to
            None to disable heartbeating entirely. Defaults to
            ``ATLAN_HEARTBEAT_TIMEOUT_SECONDS`` env var, or 60 s if unset.
            Explicit values take precedence over the env var.
        auto_heartbeat_seconds: Auto-heartbeat interval - framework sends
            heartbeats at this rate in a background task. Set to None to disable
            auto-heartbeating (manual only). Default: 10 seconds (~1/6 of timeout).
        pool: Logical worker-pool name for this task. When set, the framework
            routes this activity to the task queue registered for that pool via
            ``ATLAN_POOL_<POOL>_QUEUE``. Must match a key in the app's pkl
            contract ``pools { … }``. Must be a non-empty lowercase kebab-case
            string (e.g. ``"heavy"``, ``"cold-tier"``). The env-var key is
            derived by uppercasing the pool name and replacing hyphens with
            underscores (``"cold-tier"`` → ``ATLAN_POOL_COLD_TIER_QUEUE``), so
            mixed or upper case would create a lookup mismatch. Unset tasks run
            on the workflow's own task queue (default, backward-compatible
            behavior).
        progress_watchdog: How the stall watchdog reacts to a no-progress gap on
            this task — ``"off"``, ``"warn"`` or ``"enforce"``, or the
            :class:`~application_sdk.execution.progress.ProgressWatchdogMode`
            enum. ``None`` — the default — inherits the fleet-wide setting
            (``ATLAN_PROGRESS_WATCHDOG``, ``warn`` when unset). Declare
            ``"enforce"`` once this task's remaining gaps are inside declared
            holds or under budget, **verified against a large-tenant profile**:
            a small run hides the tail gaps, and a stall kill retries, so an
            under-instrumented task burns the same wasted work up to three times
            before failing.
        max_no_progress_seconds: How long this task may be silent before the
            watchdog calls it stalled. ``None`` inherits the fleet-wide
            allowance (``ATLAN_MAX_NO_PROGRESS_SECONDS``, 900 s when unset).
            This is not the beat interval (that is ``auto_heartbeat_seconds``,
            and it is unconditional) and not a duration budget — it measures the
            *gap* between progress signals, so a nine-hour task writing a batch
            every thirty seconds never approaches it. A single step that goes
            quiet for longer wants
            :func:`~application_sdk.execution.progress.holding_progress` at that
            call site, not a bigger number here.

    Returns:
        The decorated function with task metadata attached.

    Raises:
        TaskContractError: If the method doesn't follow the contract pattern.
    """
    if pool is not None:
        if not pool or not pool.strip():
            raise TaskContractError(
                "pool must not be empty or whitespace-only. "
                "Use a lowercase kebab-case string (e.g. 'heavy', 'cold-tier') "
                "matching a key in pools { ... } in your app contract."
            )
        if not re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", pool):
            raise TaskContractError(
                f"pool={pool!r} must be lowercase kebab-case "
                "(e.g. 'heavy', 'cold-tier'). "
                "Pool keys must match entries in pools { ... } in App.pkl; "
                "the ATLAN_POOL_<POOL>_QUEUE env-var key is derived by uppercasing "
                "the pool name, so mixed or upper case creates a lookup mismatch."
            )

    # Resolve sentinels at decoration time so test-side monkeypatching of
    # _DEFAULT_* constants takes effect on subsequent @task uses; env-var values
    # themselves are read once at module import via env_int().
    resolved_timeout: int = (
        _DEFAULT_TIMEOUT_SECONDS
        if timeout_seconds is _USE_DEFAULT
        else cast("int", timeout_seconds)
    )
    resolved_heartbeat_timeout: int | None = (
        _DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
        if heartbeat_timeout_seconds is _USE_DEFAULT
        else cast("int | None", heartbeat_timeout_seconds)
    )
    resolved_auto_heartbeat: int | None = (
        10
        if auto_heartbeat_seconds is _USE_DEFAULT
        else cast("int | None", auto_heartbeat_seconds)
    )
    resolved_schedule_to_close: int | None = (
        _DEFAULT_SCHEDULE_TO_CLOSE_SECONDS
        if schedule_to_close_seconds is _USE_DEFAULT
        else cast("int | None", schedule_to_close_seconds)
    )
    if resolved_schedule_to_close is not None and resolved_schedule_to_close <= 0:
        raise TaskContractError(
            f"schedule_to_close_seconds={resolved_schedule_to_close!r} must be a "
            "positive number of seconds. Pass None to leave the retry product "
            "unbounded (the default); a ceiling of zero would fail every attempt "
            "before it started."
        )

    # Only touched when the author declared one of the two, which keeps the
    # `application_sdk.execution` import out of the SDK's own import-time
    # decorations in `app/base.py` — see _validate_watchdog_declaration.
    resolved_watchdog: "ProgressWatchdogMode | None" = None
    resolved_no_progress: float | None = None
    if progress_watchdog is not None or max_no_progress_seconds is not None:
        resolved_watchdog, resolved_no_progress = _validate_watchdog_declaration(
            progress_watchdog, max_no_progress_seconds
        )

    def decorator(fn: F) -> F:
        task_name = name or getattr(fn, "__name__", repr(fn))

        # Validate signature and extract types
        input_type, output_type = _validate_task_signature(fn)

        # Store metadata on the function
        fn._task_metadata = TaskMetadata(  # type: ignore[attr-defined]
            name=task_name,
            func=fn,
            input_type=input_type,
            output_type=output_type,
            app_name="",  # Will be set by App registration
            description=description or fn.__doc__ or "",
            timeout_seconds=resolved_timeout,
            schedule_to_close_seconds=resolved_schedule_to_close,
            pool=pool,
            retry_policy=retry_policy,
            retry_max_attempts=retry_max_attempts,
            retry_max_interval_seconds=retry_max_interval_seconds,
            heartbeat_timeout_seconds=resolved_heartbeat_timeout,
            auto_heartbeat_seconds=resolved_auto_heartbeat,
            progress_watchdog=resolved_watchdog,
            max_no_progress_seconds=resolved_no_progress,
        )

        return fn

    # Support both @task and @task() syntax
    if func is not None:
        return decorator(func)
    return decorator


def is_task(obj: Any) -> bool:
    """Check if an object is decorated with @task."""
    return hasattr(obj, "_task_metadata")


def get_task_metadata(obj: Any) -> TaskMetadata | None:
    """Get task metadata from a decorated function."""
    return getattr(obj, "_task_metadata", None)
