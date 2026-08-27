"""Unit tests for the typed budgets and the connector-CI profile.

The profile claims to carry ``BaseE2ETest``'s class attributes and
``testing/e2e/client.py``'s module constants *verbatim*. Nothing consumes it yet
— ``testing/e2e`` is rewired in child H — so until then the only thing that can
keep that claim true is a test that reads both sides and compares. These do,
which is why they import the private constants directly: the point is to fail
when the source of a number moves, not to restate the number.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from application_sdk.testing.e2e.base import BaseE2ETest, _derive_progress_stall_seconds
from application_sdk.testing.harness.automation_engine import cold_start_submit_kwargs
from application_sdk.testing.harness.automation_engine.client import (
    _HTTP_TIMEOUT,
    _REQUEST_BACKOFF_SECONDS,
    _REQUEST_MAX_ATTEMPTS,
    _SUBMIT_TIMEOUT,
)
from application_sdk.testing.harness.automation_engine.retry import (
    MAX_RETRY_AFTER_SECONDS as _MAX_RETRY_AFTER_SECONDS,
)
from application_sdk.testing.harness.automation_engine.retry import (
    RETRY_AFTER_BUDGET_SECONDS as _RETRY_AFTER_BUDGET_SECONDS,
)
from application_sdk.testing.harness.budgets import (
    CONNECTOR_CI,
    Budget,
    BudgetProfile,
    Call,
    RequestBudget,
    Wait,
)

# poll_atlas_for_connection's own default, which is what BaseE2ETest gets by not
# overriding it. Imported as a value rather than retyped so the conversion below
# stays anchored to the signature it converts.
_MAX_NOT_FOUND_ATTEMPTS = 10


def _seconds(value: timedelta | None) -> float | None:
    """Return *value* in seconds, or ``None``."""
    return None if value is None else value.total_seconds()


# ---------------------------------------------------------------------------
# The Budget type itself
# ---------------------------------------------------------------------------


def test_budget_defaults_leave_every_guard_off_except_the_heartbeat() -> None:
    """A guard that is on by default is a guard a caller did not ask for."""
    budget = Budget(timeout=timedelta(seconds=60), poll_interval=timedelta(seconds=5))
    assert budget.start_grace is None
    assert budget.stall_timeout is None
    assert budget.max_transient_failures == 0
    assert budget.retry_after_budget is None
    assert budget.max_retry_after is None
    assert budget.heartbeat == timedelta(seconds=30)


def test_request_budget_defaults_to_a_single_attempt() -> None:
    """A retry nobody asked for is how a non-idempotent call gets sent twice."""
    request = RequestBudget(timeout=timedelta(seconds=60))
    assert request.max_attempts == 1
    assert request.backoff == timedelta(0)
    assert request.max_retry_after is None
    assert request.retry_after_budget is None


def test_a_profile_without_request_budgets_is_still_a_profile() -> None:
    profile = BudgetProfile(
        name="scenario",
        budgets={Wait.AE_RUN: CONNECTOR_CI.budgets[Wait.AE_RUN]},
    )
    assert profile.requests == {}


def test_profile_keys_are_readable_as_plain_strings() -> None:
    """A scenario suite reads its tier's keys out of a config file, so the enum
    has to be usable without importing it."""
    assert CONNECTOR_CI.budgets["ae_run"] is CONNECTOR_CI.budgets[Wait.AE_RUN]
    assert CONNECTOR_CI.requests["submit"] is CONNECTOR_CI.requests[Call.SUBMIT]


# ---------------------------------------------------------------------------
# CONNECTOR_CI is BaseE2ETest's class attributes, verbatim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wait", "timeout_attr", "interval_attr"),
    [
        (
            Wait.WORKER_HEALTH,
            "worker_health_timeout_seconds",
            "worker_health_poll_interval_seconds",
        ),
        (
            Wait.APP_READY,
            "app_ready_timeout_seconds",
            "app_ready_poll_interval_seconds",
        ),
        (
            Wait.DEPLOYED_MANIFEST,
            "deployed_manifest_timeout_seconds",
            "deployed_manifest_poll_interval_seconds",
        ),
        (Wait.AE_RUN, "ae_poll_timeout_seconds", "ae_poll_interval_seconds"),
        (
            Wait.ATLAS_CONNECTION,
            "atlas_poll_timeout_seconds",
            "atlas_poll_interval_seconds",
        ),
        (
            Wait.ATLAS_ASSET_COUNTS,
            "atlas_asset_poll_timeout_seconds",
            "atlas_asset_poll_interval_seconds",
        ),
    ],
)
def test_every_wait_carries_its_class_attribute_pair(
    wait: Wait, timeout_attr: str, interval_attr: str
) -> None:
    budget = CONNECTOR_CI.budgets[wait]
    assert _seconds(budget.timeout) == getattr(BaseE2ETest, timeout_attr)
    assert _seconds(budget.poll_interval) == getattr(BaseE2ETest, interval_attr)


def test_the_profile_covers_every_wait_and_every_call() -> None:
    """A wait missing from the tier is a wait that silently keeps its hard-coded
    number when the class is rewired."""
    assert set(CONNECTOR_CI.budgets) == set(Wait)
    assert set(CONNECTOR_CI.requests) == set(Call)


def test_the_ae_run_stall_guard_is_the_class_grace_and_the_derived_window() -> None:
    budget = CONNECTOR_CI.budgets[Wait.AE_RUN]
    assert _seconds(budget.start_grace) == BaseE2ETest.ae_stall_grace_seconds
    # dag_progress_stall_seconds is None by default, i.e. derived from the
    # ceiling rather than pinned — so the profile has to carry the derivation's
    # answer, not the old absolute it replaced.
    assert BaseE2ETest.dag_progress_stall_seconds is None
    assert _seconds(budget.stall_timeout) == _derive_progress_stall_seconds(
        BaseE2ETest.ae_poll_timeout_seconds
    )


def test_the_ae_run_transient_streak_matches_the_poll_loops_own_default() -> None:
    """``poll_native_status``'s ``max_transient_failures`` keyword default."""
    import inspect

    from application_sdk.testing.e2e.client import AEWorkflowClient

    signature = inspect.signature(AEWorkflowClient.poll_native_status)
    assert (
        CONNECTOR_CI.budgets[Wait.AE_RUN].max_transient_failures
        == signature.parameters["max_transient_failures"].default
    )


def test_the_atlas_connection_grace_is_the_not_found_cap_as_a_duration() -> None:
    """Every probe that reaches the cap is an empty search (a hit returns
    immediately), so attempt N fires at (N-1) intervals elapsed."""
    budget = CONNECTOR_CI.budgets[Wait.ATLAS_CONNECTION]
    expected = (_MAX_NOT_FOUND_ATTEMPTS - 1) * BaseE2ETest.atlas_poll_interval_seconds
    assert _seconds(budget.start_grace) == expected


def test_the_app_ready_budget_still_divides_into_todays_submit_retry() -> None:
    """``cold_start_submit_kwargs`` turns the same two numbers back into the
    submit loop's ``retries`` / ``retry_sleep_seconds``, so the pair here has to
    be the pair it divides."""
    budget = CONNECTOR_CI.budgets[Wait.APP_READY]
    assert cold_start_submit_kwargs(
        int(budget.timeout.total_seconds()),
        int(budget.poll_interval.total_seconds()),
    ) == {
        "retries": BaseE2ETest.app_ready_timeout_seconds
        // BaseE2ETest.app_ready_poll_interval_seconds,
        "retry_sleep_seconds": BaseE2ETest.app_ready_poll_interval_seconds,
    }


@pytest.mark.parametrize(
    "wait",
    [
        Wait.APP_READY,
        Wait.DEPLOYED_MANIFEST,
        Wait.AE_RUN,
        Wait.ATLAS_CONNECTION,
        Wait.ATLAS_ASSET_COUNTS,
    ],
)
def test_the_loops_that_narrate_themselves_keep_the_heartbeat_off(wait: Wait) -> None:
    """Each of these call sites passes ``heartbeat_seconds=0`` today, because it
    logs its own richer per-poll line and a second one would be duplicate noise."""
    assert CONNECTOR_CI.budgets[wait].heartbeat is None


def test_the_only_loop_that_says_nothing_keeps_the_default_heartbeat() -> None:
    assert CONNECTOR_CI.budgets[Wait.WORKER_HEALTH].heartbeat == timedelta(seconds=30)


# ---------------------------------------------------------------------------
# CONNECTOR_CI is client.py's module constants, verbatim
# ---------------------------------------------------------------------------


def test_the_http_call_budget_is_the_client_constants() -> None:
    request = CONNECTOR_CI.requests[Call.HTTP]
    assert _seconds(request.timeout) == _HTTP_TIMEOUT
    assert request.max_attempts == _REQUEST_MAX_ATTEMPTS
    assert _seconds(request.backoff) == _REQUEST_BACKOFF_SECONDS
    assert _seconds(request.max_retry_after) == _MAX_RETRY_AFTER_SECONDS
    assert _seconds(request.retry_after_budget) == _RETRY_AFTER_BUDGET_SECONDS


def test_the_submit_keeps_its_own_longer_per_attempt_ceiling() -> None:
    """Long enough that a Cloudflare 504 arrives as an HTTP error the retry loop
    understands, rather than as a raw TimeoutError."""
    submit = CONNECTOR_CI.requests[Call.SUBMIT]
    assert _seconds(submit.timeout) == _SUBMIT_TIMEOUT
    assert submit.timeout > CONNECTOR_CI.requests[Call.HTTP].timeout


@pytest.mark.parametrize("wait", [Wait.APP_READY, Wait.AE_RUN])
def test_the_waits_that_honour_origin_backoff_carry_the_same_ceiling(
    wait: Wait,
) -> None:
    """Both spend their extra waiting through the client's retry-after budget, so
    a slow origin cannot stretch either past what that constant allows."""
    budget = CONNECTOR_CI.budgets[wait]
    assert _seconds(budget.retry_after_budget) == _RETRY_AFTER_BUDGET_SECONDS


def test_the_ae_run_carries_the_per_wait_ceiling_its_loop_already_applies() -> None:
    """``_retry_gap`` clamps every single honoured wait inside ``poll_native_status``
    at ``_MAX_RETRY_AFTER_SECONDS``. That bound had no home on ``Budget`` until
    child C needed to honour origin backoff from inside the primitive, so it is
    the one number in this profile that arrived after the original lift — read
    back off the same constant as the rest."""
    budget = CONNECTOR_CI.budgets[Wait.AE_RUN]
    assert _seconds(budget.max_retry_after) == _MAX_RETRY_AFTER_SECONDS
