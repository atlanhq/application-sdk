"""Tests for every categorical leaf — category, default_retryable, code, audience, and fields."""

import pytest

from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AlreadyExistsError,
    AppPermissionDeniedError,
    AppTimeoutError,
    AuthError,
    CancelledError,
    DataIntegrityError,
    DependencyUnavailableError,
    InternalError,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
    RateLimitedError,
    ResourceExhaustedError,
    SourceUnavailableError,
    TaskStalledError,
    UnimplementedError,
)

_LEAVES = [
    (
        CancelledError,
        FailureCategory.CANCELLED,
        False,
        "CANCELLED",
        Audience.APP_OWNER,
        ["cancelled_by", "reason"],
    ),
    (
        AppTimeoutError,
        FailureCategory.TIMEOUT,
        True,
        "TIMEOUT",
        Audience.APP_OWNER,
        ["operation", "timeout_seconds", "elapsed_seconds"],
    ),
    (
        RateLimitedError,
        FailureCategory.RATE_LIMITED,
        True,
        "RATE_LIMITED",
        Audience.APP_OWNER,
        ["limit_type", "retry_after_seconds", "quota_name"],
    ),
    (
        AuthError,
        FailureCategory.AUTH,
        False,
        "AUTH",
        Audience.USER,
        ["auth_method", "principal", "failure_reason"],
    ),
    (
        AppPermissionDeniedError,
        FailureCategory.PERMISSION,
        False,
        "PERMISSION",
        Audience.USER,
        ["principal", "resource", "required_action"],
    ),
    (
        NotFoundError,
        FailureCategory.NOT_FOUND,
        False,
        "NOT_FOUND",
        Audience.USER,
        ["resource_type", "resource_identifier"],
    ),
    (
        AlreadyExistsError,
        FailureCategory.ALREADY_EXISTS,
        False,
        "ALREADY_EXISTS",
        Audience.USER,
        ["resource_type", "resource_identifier"],
    ),
    (
        InvalidInputError,
        FailureCategory.INVALID_INPUT,
        False,
        "INVALID_INPUT",
        Audience.USER,
        ["field", "constraint", "value_summary"],
    ),
    (
        PreconditionError,
        FailureCategory.PRECONDITION,
        False,
        "PRECONDITION",
        Audience.USER,
        ["resource", "expected_state", "actual_state"],
    ),
    (
        DependencyUnavailableError,
        FailureCategory.DEPENDENCY_UNAVAILABLE,
        True,
        "DEPENDENCY_UNAVAILABLE",
        Audience.PLATFORM,
        ["service", "target", "network_error"],
    ),
    (
        SourceUnavailableError,
        FailureCategory.SOURCE_UNAVAILABLE,
        True,
        "SOURCE_UNAVAILABLE",
        Audience.USER,
        ["source_type", "endpoint", "http_status", "network_error"],
    ),
    (
        ResourceExhaustedError,
        FailureCategory.RESOURCE_EXHAUSTED,
        True,
        "RESOURCE_EXHAUSTED",
        Audience.PLATFORM,
        ["resource", "limit", "observed"],
    ),
    (
        DataIntegrityError,
        FailureCategory.DATA_INTEGRITY,
        False,
        "DATA_INTEGRITY",
        Audience.APP_OWNER,
        ["expectation", "observed", "location"],
    ),
    (
        InternalError,
        FailureCategory.INTERNAL,
        False,
        "INTERNAL",
        Audience.APP_OWNER,
        ["component", "invariant", "classification_pending"],
    ),
    (
        UnimplementedError,
        FailureCategory.UNIMPLEMENTED,
        False,
        "UNIMPLEMENTED",
        Audience.APP_OWNER,
        ["operation", "reason"],
    ),
]


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_audience,expected_fields",
    _LEAVES,
)
def test_leaf_metadata(
    cls,
    expected_cat,
    expected_retryable,
    expected_code,
    expected_audience,
    expected_fields,
) -> None:
    assert cls.category is expected_cat
    assert cls.default_retryable is expected_retryable
    assert cls.code == expected_code
    assert cls.audience is expected_audience


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_audience,expected_fields",
    _LEAVES,
)
def test_leaf_has_expected_fields(
    cls,
    expected_cat,
    expected_retryable,
    expected_code,
    expected_audience,
    expected_fields,
) -> None:
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(cls)}
    for field in expected_fields:
        assert field in field_names, f"{cls.__name__} missing field '{field}'"


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_audience,expected_fields",
    _LEAVES,
)
def test_leaf_constructs_with_message_only(
    cls,
    expected_cat,
    expected_retryable,
    expected_code,
    expected_audience,
    expected_fields,
) -> None:
    e = cls(message="test")
    assert str(e) == "test"
    assert e.category is expected_cat


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_audience,expected_fields",
    _LEAVES,
)
def test_leaf_audience_in_failure_details(
    cls,
    expected_cat,
    expected_retryable,
    expected_code,
    expected_audience,
    expected_fields,
) -> None:
    fd = cls(message="test").to_failure_details()
    assert fd.audience is expected_audience


class TestTaskStalledError:
    """The stall subtype of the TIMEOUT leaf (ADR-0018).

    Not in ``_LEAVES``: that table is one entry per ``FailureCategory``, and a
    stall shares TIMEOUT with :class:`AppTimeoutError` by design rather than
    claiming a sixteenth category.
    """

    def test_is_an_app_timeout_error(self) -> None:
        # `except AppTimeoutError:` in an app must keep catching a stall kill —
        # that is the whole reason this is a subtype and not a new leaf.
        assert issubclass(TaskStalledError, AppTimeoutError)
        assert isinstance(TaskStalledError(message="stalled"), AppTimeoutError)

    def test_metadata(self) -> None:
        assert TaskStalledError.category is FailureCategory.TIMEOUT
        assert TaskStalledError.code == "TIMEOUT_TASK_STALLED"
        assert TaskStalledError.audience is Audience.APP_OWNER
        assert (
            TaskStalledError(message="stalled").qualified_code
            == "TIMEOUT.TIMEOUT_TASK_STALLED"
        )

    def test_is_retryable_by_default(self) -> None:
        """The deliberate half of the ADR-0018 argument, pinned.

        The dominant cause of a stall is a transient source-side hang that
        self-heals; flipping this to non-retryable would convert that majority
        into failed runs needing a manual re-run from zero.
        """
        assert TaskStalledError.default_retryable is True
        assert TaskStalledError(message="stalled").effective_retryable is True

    def test_distinguishable_from_a_plain_timeout_on_the_wire(self) -> None:
        """Same category, different code — so a stall kill is countable on its own.

        Rolling a stall into ``TIMEOUT.TIMEOUT`` would make the M2 warn data
        unreadable: the fleet-wide numbers could not separate stall kills from
        StartToClose and heartbeat timeouts.
        """
        stalled = TaskStalledError(message="stalled").to_failure_details()
        plain = AppTimeoutError(message="timed out").to_failure_details()

        assert stalled.category is plain.category
        assert stalled.code != plain.code

    def test_evidence_carries_the_gap_and_the_label(self) -> None:
        fd = TaskStalledError(
            message="stalled",
            operation="fetch_tables",
            stalled_for_seconds=915.0,
            last_progress_label="writer.flush_buffer",
        ).to_failure_details()

        assert fd.evidence["stalled_for_seconds"] == 915.0
        assert fd.evidence["last_progress_label"] == "writer.flush_buffer"
        assert fd.evidence["operation"] == "fetch_tables"
        assert fd.retryable is True
        assert fd.audience is Audience.APP_OWNER

    def test_the_inherited_bounded_wait_fields_stay_unset(self) -> None:
        """A stall has no bounded wait that elapsed — that absence is the point.

        Populating ``timeout_seconds`` / ``elapsed_seconds`` with the stall gap
        would claim a limit was configured and hit, when what happened is that
        nothing bounded the quiet at all.
        """
        fd = TaskStalledError(
            message="stalled", stalled_for_seconds=915.0
        ).to_failure_details()

        assert fd.evidence["timeout_seconds"] is None
        assert fd.evidence["elapsed_seconds"] is None

    def test_context_fields_survive_to_the_envelope(self) -> None:
        fd = TaskStalledError(
            message="stalled",
            app_name="_app",
            run_id="run-1",
            suggested_action="declare an allowance",
        ).to_failure_details()

        assert (fd.app_name, fd.run_id) == ("_app", "run-1")
        assert fd.suggested_action == "declare an allowance"


def test_internal_error_classification_pending_default() -> None:
    e = InternalError(message="bug")
    assert e.classification_pending is False


def test_internal_error_classification_pending_set() -> None:
    e = InternalError(message="unclassified failure", classification_pending=True)
    fd = e.to_failure_details()
    assert fd.evidence["classification_pending"] is True
