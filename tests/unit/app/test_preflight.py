"""Unit tests for PublishPreflightMixin — covers all skip paths and the
PreflightOutput construction edge cases.

Regression guard for HYP-829: the live Hive worker crashed at runtime with
``TypeError: PreflightOutput.__init__() missing 1 required positional argument:
'checks'`` because ``field(default_factory=list)`` is not honored when combined
with ``@dataclass`` + Pydantic ``Output`` + ``Annotated[..., MaxItems(20)]``.
The fix is to pass ``checks=[]`` explicitly at every skip-path call site; these
tests lock that in so the bug cannot recur.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.app.preflight import (
    PreflightOutput,
    PublishPreflightMixin,
)
from application_sdk.contracts.base import Input

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MinimalInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    user_id: str = ""
    connection_qualified_name: str = ""


class _Host(PublishPreflightMixin):
    """Bare host for testing the mixin without an App framework."""


# ---------------------------------------------------------------------------
# PreflightOutput construction — regression guard
# ---------------------------------------------------------------------------


class TestPreflightOutputConstruction:
    """The skip paths in run_publish_preflight construct PreflightOutput without
    explicit ``checks=`` (relying on the default).  Verify that pattern works.
    """

    def test_construct_with_only_passed_and_message(self):
        """Mirror the skip-path call site exactly: passed + message, no checks."""
        out = PreflightOutput(
            passed=True,
            checks=[],
            message="skipped",
        )
        assert out.passed is True
        assert out.checks == []
        assert out.message == "skipped"

    def test_construct_with_default_checks_factory(self):
        """When checks is omitted entirely, default_factory must produce []."""
        # If this raises TypeError(missing 1 required positional argument: 'checks'),
        # the skip-path code in run_publish_preflight will crash at runtime.
        out = PreflightOutput(passed=True, message="ok")
        assert out.checks == []

    def test_construct_full(self):
        out = PreflightOutput(
            passed=False,
            checks=[{"name": "UserEnabled", "passed": False, "message": "disabled"}],
            message="failed",
        )
        assert out.passed is False
        assert len(out.checks) == 1
        assert out.checks[0]["name"] == "UserEnabled"


# ---------------------------------------------------------------------------
# Skip paths — must not crash
# ---------------------------------------------------------------------------


class TestRunPublishPreflightSkipPaths:
    """Regression: skip paths previously raised TypeError because PreflightOutput
    was being constructed without ``checks=[]``.  Verify each skip path returns
    a valid PreflightOutput with passed=True and an explanatory message.
    """

    async def test_skip_when_user_id_absent(self):
        """No user_id in input → returns passed=True, skipped message, empty checks."""
        host = _Host()
        # No user_id field — getattr() returns "".
        out = await host.run_publish_preflight(_MinimalInput())

        assert isinstance(out, PreflightOutput)
        assert out.passed is True
        assert out.checks == []
        assert "skipped" in out.message.lower()
        assert "user_id" in out.message.lower()

    async def test_skip_when_user_id_empty_string(self):
        """Explicit empty user_id → same skip path."""
        host = _Host()
        out = await host.run_publish_preflight(_MinimalInput(user_id=""))

        assert out.passed is True
        assert out.checks == []

    async def test_skip_when_service_token_unavailable(self):
        """user_id present but _get_service_token returns "" → skip with message.

        _get_service_token is async — patch with AsyncMock so the await call
        gets a real coroutine that resolves to "".
        """
        host = _Host()
        with patch(
            "application_sdk.app.preflight._get_service_token",
            new=AsyncMock(return_value=""),
        ):
            out = await host.run_publish_preflight(_MinimalInput(user_id="user-abc"))

        assert isinstance(out, PreflightOutput)
        assert out.passed is True
        assert out.checks == []
        assert "skipped" in out.message.lower()
        assert "service" in out.message.lower() or "credentials" in out.message.lower()


# ---------------------------------------------------------------------------
# Active paths — confirm checks list is populated
# ---------------------------------------------------------------------------


class TestRunPublishPreflightActivePaths:
    """When the gate runs to completion, PreflightOutput.checks contains the
    serialised check results — verify the list is populated and well-shaped.
    """

    async def test_passes_when_all_checks_pass(self):
        """user_id + token + connection → runs check_atlan_publish_permission;
        when all checks pass, returns PreflightOutput(passed=True, checks=...).
        """
        from application_sdk.handler.contracts import PreflightCheck

        host = _Host()
        fake_checks = [
            PreflightCheck(name="UserEnabled", passed=True, message="active"),
            PreflightCheck(
                name="AtlanPublishPermission", passed=True, message="granted"
            ),
        ]
        with (
            patch(
                "application_sdk.app.preflight._get_service_token",
                new=AsyncMock(return_value="svc-tok"),
            ),
            patch(
                "application_sdk.app.preflight.check_atlan_publish_permission",
                new=AsyncMock(return_value=fake_checks),
            ),
            patch(
                "application_sdk.infrastructure.secrets.get_deployment_secret",
                new=AsyncMock(return_value=""),
            ),
        ):
            out = await host.run_publish_preflight(
                _MinimalInput(
                    user_id="user-abc",
                    connection_qualified_name="default/hive/123",
                )
            )

        assert out.passed is True
        assert len(out.checks) == 2
        assert out.message == "All publish preflight checks passed."

    async def test_raises_when_any_check_fails(self):
        """Failed check → AppPermissionDeniedError with the first failure message."""
        from application_sdk.errors.leaves import AppPermissionDeniedError
        from application_sdk.handler.contracts import PreflightCheck

        host = _Host()
        fake_checks = [
            PreflightCheck(
                name="UserEnabled", passed=False, message="User bob is disabled"
            ),
        ]
        with (
            patch(
                "application_sdk.app.preflight._get_service_token",
                new=AsyncMock(return_value="svc-tok"),
            ),
            patch(
                "application_sdk.app.preflight.check_atlan_publish_permission",
                new=AsyncMock(return_value=fake_checks),
            ),
            patch(
                "application_sdk.infrastructure.secrets.get_deployment_secret",
                new=AsyncMock(return_value=""),
            ),
            pytest.raises(AppPermissionDeniedError) as exc_info,
        ):
            await host.run_publish_preflight(
                _MinimalInput(
                    user_id="user-bob",
                    connection_qualified_name="default/hive/123",
                )
            )

        assert "bob" in str(exc_info.value)
        assert "disabled" in str(exc_info.value).lower()
