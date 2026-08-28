"""Unit tests for SDR Temporal workflows and activity wiring."""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from application_sdk.app.base import AppContextError
from application_sdk.credentials.agent import SecretStoreCheckResult
from application_sdk.execution._temporal.sdr import (
    SDR_FETCH_METADATA_ACTIVITY,
    SDR_PREFLIGHT_ACTIVITY,
    SDR_TEST_AUTH_ACTIVITY,
    SDR_WORKFLOWS,
    SdrFetchMetadataWorkflow,
    SdrPreflightCheckWorkflow,
    SdrTestAuthWorkflow,
    _secret_store_check_row,
    build_sdr_activities,
)
from application_sdk.handler.base import Handler
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)


class _StubHandler(Handler):
    """Handler that records context at call time and returns canned outputs."""

    def __init__(self) -> None:
        super().__init__()
        self.auth_input: AuthInput | None = None
        self.preflight_input: PreflightInput | None = None
        self.metadata_input: MetadataInput | None = None
        self.context_during_call: object | None = None

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        self.auth_input = input
        self.context_during_call = self.context
        return AuthOutput(status=AuthStatus.SUCCESS, message="ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.preflight_input = input
        self.context_during_call = self.context
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        self.metadata_input = input
        self.context_during_call = self.context
        return SqlMetadataOutput(objects=[])


# resolved=None (the default) so the preflight activity falls back to the
# resolver mock these tests assert on; the resolved-reuse path is covered
# separately in test_preflight_reuses_resolved_credentials.
_PASS_SECRET_STORE = SecretStoreCheckResult(
    passed=True,
    store_down=False,
    fatal=False,
    substituted=1,
    message="Secret store reachable",
)


class TestSdrWorkflows:
    """SDR workflow classes themselves are static — verify naming and membership."""

    def test_workflow_names(self) -> None:
        # Temporal stores the registered name under __temporal_workflow_definition.name
        defn = getattr(SdrTestAuthWorkflow, "__temporal_workflow_definition")
        assert defn.name == "sdr:test_auth"
        defn = getattr(SdrPreflightCheckWorkflow, "__temporal_workflow_definition")
        assert defn.name == "sdr:preflight_check"
        defn = getattr(SdrFetchMetadataWorkflow, "__temporal_workflow_definition")
        assert defn.name == "sdr:fetch_metadata"

    def test_sdr_workflows_constant(self) -> None:
        assert len(SDR_WORKFLOWS) == 3
        assert SdrTestAuthWorkflow in SDR_WORKFLOWS
        assert SdrPreflightCheckWorkflow in SDR_WORKFLOWS
        assert SdrFetchMetadataWorkflow in SDR_WORKFLOWS


class TestSdrTimeoutsAndRetries:
    """Guard the UI-facing wall-clock caps and retry policy.

    These are user-visible: a regression that bumps auth schedule_to_close to
    minutes would silently turn a "worker offline" error into an indefinite
    spinner. Explicit assertions catch that.
    """

    def test_schedule_to_close_caps(self) -> None:
        from datetime import timedelta

        from application_sdk.execution._temporal import sdr

        # Flat 300s cap per activity (env-tunable) — generous enough for a slow
        # customer secret store + source check. See the module-level comment.
        assert sdr._AUTH_SCHEDULE_TO_CLOSE == timedelta(seconds=300)
        assert sdr._PREFLIGHT_SCHEDULE_TO_CLOSE == timedelta(seconds=300)
        assert sdr._METADATA_SCHEDULE_TO_CLOSE == timedelta(seconds=300)
        assert sdr._AUTH_START_TO_CLOSE == timedelta(seconds=300)
        assert sdr._PREFLIGHT_START_TO_CLOSE == timedelta(seconds=300)
        assert sdr._METADATA_START_TO_CLOSE == timedelta(seconds=300)

    def test_start_to_close_not_above_schedule_to_close(self) -> None:
        from application_sdk.execution._temporal import sdr

        # Invariant: start_to_close <= schedule_to_close in each pair (Temporal
        # rejects a larger start_to_close). Equal is the deliberate flat cap.
        assert sdr._AUTH_START_TO_CLOSE <= sdr._AUTH_SCHEDULE_TO_CLOSE
        assert sdr._PREFLIGHT_START_TO_CLOSE <= sdr._PREFLIGHT_SCHEDULE_TO_CLOSE
        assert sdr._METADATA_START_TO_CLOSE <= sdr._METADATA_SCHEDULE_TO_CLOSE

    def test_inverted_timeout_pair_warns_at_module_load(self, monkeypatch) -> None:
        import importlib

        from application_sdk.execution._temporal import sdr

        # Invert the auth pair: start_to_close >= schedule_to_close leaves no room
        # for a retry attempt inside the schedule cap. Expect a WARNING at load.
        monkeypatch.setenv("ATLAN_SDR_AUTH_SCHEDULE_TO_CLOSE_SECONDS", "30")
        monkeypatch.setenv("ATLAN_SDR_AUTH_START_TO_CLOSE_SECONDS", "60")
        fake_logger = mock.MagicMock()
        try:
            with mock.patch(
                "application_sdk.observability.logger_adaptor.get_logger",
                return_value=fake_logger,
            ):
                importlib.reload(sdr)
            assert fake_logger.warning.called
            warned = " ".join(str(c) for c in fake_logger.warning.call_args_list)
            assert "AUTH" in warned and "test_auth" in warned
        finally:
            monkeypatch.undo()
            importlib.reload(sdr)

    def test_timeouts_are_env_overridable(self, monkeypatch) -> None:
        import importlib

        from application_sdk.execution._temporal import sdr

        monkeypatch.setenv("ATLAN_SDR_PREFLIGHT_SCHEDULE_TO_CLOSE_SECONDS", "200")
        monkeypatch.setenv("ATLAN_SDR_PREFLIGHT_START_TO_CLOSE_SECONDS", "190")
        # Non-integer values fall back to the default rather than raising.
        monkeypatch.setenv("ATLAN_SDR_AUTH_SCHEDULE_TO_CLOSE_SECONDS", "not-an-int")
        # Non-positive values also fall back — a 0/negative timeout would be
        # rejected by Temporal at schedule time.
        monkeypatch.setenv("ATLAN_SDR_METADATA_SCHEDULE_TO_CLOSE_SECONDS", "0")
        monkeypatch.setenv("ATLAN_SDR_METADATA_START_TO_CLOSE_SECONDS", "-5")
        reloaded = importlib.reload(sdr)
        try:
            from datetime import timedelta

            assert reloaded._PREFLIGHT_SCHEDULE_TO_CLOSE == timedelta(seconds=200)
            assert reloaded._PREFLIGHT_START_TO_CLOSE == timedelta(seconds=190)
            # bad/non-positive overrides fall back to the flat 300s default
            assert reloaded._AUTH_SCHEDULE_TO_CLOSE == timedelta(seconds=300)
            assert reloaded._METADATA_SCHEDULE_TO_CLOSE == timedelta(seconds=300)
            assert reloaded._METADATA_START_TO_CLOSE == timedelta(seconds=300)
        finally:
            # Restore module-level defaults for the rest of the suite.
            monkeypatch.undo()
            importlib.reload(sdr)

    def test_auth_retry_is_fail_fast(self) -> None:
        from application_sdk.execution._temporal import sdr

        # test_auth must not retry on bad credentials -- one attempt, period.
        assert sdr._AUTH_RETRY.maximum_attempts == 1

    def test_default_retry_is_bounded(self) -> None:
        from application_sdk.execution._temporal import sdr

        # preflight + fetch_metadata: at most 2 attempts so retries never
        # blow past the schedule_to_close cap.
        assert sdr._DEFAULT_RETRY.maximum_attempts == 2


class TestBuildSdrActivities:
    """Tests for build_sdr_activities()."""

    def test_returns_three_activities_with_sdr_names(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        assert len(activities) == 3
        names = [getattr(a, "__temporal_activity_definition").name for a in activities]
        assert set(names) == {
            SDR_TEST_AUTH_ACTIVITY,
            SDR_PREFLIGHT_ACTIVITY,
            SDR_FETCH_METADATA_ACTIVITY,
        }

    async def test_test_auth_activity_dispatches_and_sets_context(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        input_obj = AuthInput(credentials=[], connection_id="c1")
        result = await test_auth(input_obj)

        assert result.status == AuthStatus.SUCCESS
        assert handler.auth_input is input_obj
        # Context was set during the call, and cleared afterwards.
        assert handler.context_during_call is not None
        with pytest.raises(AppContextError):
            _ = handler.context

    async def test_preflight_activity_dispatches(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        preflight = by_name[SDR_PREFLIGHT_ACTIVITY]

        input_obj = PreflightInput(credentials=[], connection_config={"host": "x"})
        result = await preflight(input_obj)

        assert result.status == PreflightStatus.READY
        assert handler.preflight_input is input_obj
        with pytest.raises(AppContextError):
            _ = handler.context

    async def test_preflight_activity_emits_outcome_row(self) -> None:
        # FND-901: the SDR surface was silent — a failed test-connection left no
        # log evidence. It now emits the shared "Preflight check outcome" row.
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        preflight = by_name[SDR_PREFLIGHT_ACTIVITY]
        with mock.patch("application_sdk.execution._temporal.sdr.logger") as ml:
            await preflight(PreflightInput(credentials=[]))
        event = next(
            c.kwargs
            for c in ml.info.call_args_list
            if c.args and c.args[0] == "Preflight check outcome"
        )
        assert event["preflight_surface"] == "sdr"
        assert event["outcome"] == "ready"
        assert event["app_name"] == "myapp"

    async def test_preflight_fatal_secret_store_emits_outcome_row(self) -> None:
        # The fatal short-circuit return path must emit the same row — its
        # checks are the synthetic secret-store row, so the shape differs.
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        preflight = by_name[SDR_PREFLIGHT_ACTIVITY]
        fatal = SecretStoreCheckResult(
            passed=False,
            store_down=True,
            fatal=True,
            substituted=0,
            message="Secret store unreachable",
        )
        fake_infra = mock.MagicMock()
        fake_infra.secret_store = mock.MagicMock(name="SecretStore")
        with (
            mock.patch(
                "application_sdk.execution._temporal.sdr.get_infrastructure",
                return_value=fake_infra,
            ),
            mock.patch(
                "application_sdk.execution._temporal.sdr.check_secret_store_access",
                new=mock.AsyncMock(return_value=fatal),
            ),
            mock.patch("application_sdk.execution._temporal.sdr.logger") as ml,
        ):
            result = await preflight(
                PreflightInput(agent_json={"agent-name": "acme", "secret-path": "p"})
            )
        assert result.status == PreflightStatus.NOT_READY
        assert handler.preflight_input is None  # short-circuited, handler never ran
        # A failed SDR test-connection surfaces through a run log read at the
        # default ERROR filter, so the row mirrors the gate and lands at ERROR.
        event = next(
            c.kwargs
            for c in ml.error.call_args_list
            if c.args and c.args[0] == "Preflight check outcome"
        )
        assert event["preflight_surface"] == "sdr"
        assert event["outcome"] == "not_ready"
        assert "Secret store" in event["check_matrix"]

    async def test_fetch_metadata_activity_dispatches(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        fetch_metadata = by_name[SDR_FETCH_METADATA_ACTIVITY]

        input_obj = MetadataInput(credentials=[], connection_config={})
        result = await fetch_metadata(input_obj)

        assert result.objects == []
        assert handler.metadata_input is input_obj
        with pytest.raises(AppContextError):
            _ = handler.context

    async def test_context_app_name_and_credentials_are_populated(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        from application_sdk.handler.contracts import HandlerCredential

        creds = [HandlerCredential(key="api_key", value="secret123")]
        await test_auth(AuthInput(credentials=creds))

        captured = handler.context_during_call
        assert captured is not None
        assert captured.app_name == "myapp"  # type: ignore[attr-defined]
        assert len(captured.credentials) == 1  # type: ignore[attr-defined]
        assert captured.get_credential("api_key") == "secret123"  # type: ignore[attr-defined]

    async def test_context_clears_even_when_handler_raises(self) -> None:
        class _FailingHandler(Handler):
            async def test_auth(self, input: AuthInput) -> AuthOutput:
                raise RuntimeError("boom")

            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                return PreflightOutput(status=PreflightStatus.READY, checks=[])

            async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
                return SqlMetadataOutput(objects=[])

        handler = _FailingHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        with pytest.raises(RuntimeError, match="boom"):
            await test_auth(AuthInput(credentials=[]))

        with pytest.raises(AppContextError):
            _ = handler.context

    async def test_secret_store_pulled_from_infrastructure_context(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        fake_secret_store = mock.MagicMock(name="SecretStore")
        fake_infra = mock.MagicMock()
        fake_infra.secret_store = fake_secret_store

        with mock.patch(
            "application_sdk.infrastructure.context.get_infrastructure",
            return_value=fake_infra,
        ):
            await test_auth(AuthInput(credentials=[]))

        assert handler.context_during_call is not None
        assert (
            handler.context_during_call._secret_store is fake_secret_store  # type: ignore[attr-defined]
        )

    async def test_secret_store_is_none_when_no_infrastructure(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        with mock.patch(
            "application_sdk.infrastructure.context.get_infrastructure",
            return_value=None,
        ):
            await test_auth(AuthInput(credentials=[]))

        assert handler.context_during_call is not None
        assert handler.context_during_call._secret_store is None  # type: ignore[attr-defined]

    async def test_concurrent_activities_see_independent_contexts(self) -> None:
        """Regression: concurrent SDR activities on a shared handler must not
        overwrite each other's context (ContextVar isolation)."""
        from application_sdk.handler.contracts import HandlerCredential

        # Each invocation records the credential it sees mid-call.
        seen_credentials: list[str | None] = []
        barrier = asyncio.Event()

        class _SlowHandler(Handler):
            async def test_auth(self, input: AuthInput) -> AuthOutput:
                # Both tasks reach here before either records — forces overlap.
                barrier.set()
                await asyncio.sleep(0)  # yield to let the other task run
                seen_credentials.append(self.context.get_credential("api_key"))
                return AuthOutput(status=AuthStatus.SUCCESS, message="ok")

            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                return PreflightOutput(status=PreflightStatus.READY, checks=[])

            async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
                return SqlMetadataOutput(objects=[])

        handler = _SlowHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        creds_a = [HandlerCredential(key="api_key", value="user-A")]
        creds_b = [HandlerCredential(key="api_key", value="user-B")]

        await asyncio.gather(
            test_auth(AuthInput(credentials=creds_a)),
            test_auth(AuthInput(credentials=creds_b)),
        )

        # Each concurrent call must have seen only its own credential.
        assert set(seen_credentials) == {"user-A", "user-B"}


class TestSdrAgentJsonResolution:
    """Worker-side resolution of the SDR ``agent_json`` reference.

    Mirrors ``test_preflight_gate_activity`` — the SDR activities resolve an
    agent-json reference to concrete credentials before binding the handler
    context, so a ``secret-path`` reference never reaches the handler as a
    literal string.
    """

    @staticmethod
    def _by_name(handler: Handler) -> dict[str, object]:
        activities = build_sdr_activities(handler, app_name="myapp")
        return {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }

    async def test_agent_json_resolved_and_populates_credentials(self) -> None:
        handler = _StubHandler()
        preflight = self._by_name(handler)[SDR_PREFLIGHT_ACTIVITY]

        resolver = mock.MagicMock()
        resolver.resolve_raw = mock.AsyncMock(
            return_value={"host": "db", "username": "u", "extra": {"role": "r"}}
        )
        fake_infra = mock.MagicMock()
        fake_infra.secret_store = mock.MagicMock(name="SecretStore")

        with (
            mock.patch(
                "application_sdk.execution._temporal.sdr.get_infrastructure",
                return_value=fake_infra,
            ),
            mock.patch(
                "application_sdk.execution._temporal.sdr.CredentialResolver",
                return_value=resolver,
            ),
            mock.patch(
                "application_sdk.execution._temporal.sdr.check_secret_store_access",
                new=mock.AsyncMock(return_value=_PASS_SECRET_STORE),
            ),
        ):
            result = await preflight(
                PreflightInput(agent_json={"agent-name": "acme", "secret-path": "p"})
            )

        assert result.status == PreflightStatus.READY
        assert handler.preflight_input is not None
        seen = {c.key: c.value for c in handler.preflight_input.credentials}
        assert seen == {"host": "db", "username": "u", "extra.role": "r"}
        resolver.resolve_raw.assert_awaited_once()
        # Resolved credentials are what the handler context saw.
        captured = handler.context_during_call
        assert captured is not None
        assert captured.get_credential("host") == "db"  # type: ignore[attr-defined]

    async def test_agent_json_resolution_on_all_three_activities(self) -> None:
        resolver = mock.MagicMock()
        resolver.resolve_raw = mock.AsyncMock(return_value={"username": "u"})
        fake_infra = mock.MagicMock()
        fake_infra.secret_store = mock.MagicMock(name="SecretStore")

        agent_json = {"agent-name": "acme", "secret-path": "p"}
        cases = (
            (SDR_TEST_AUTH_ACTIVITY, AuthInput(agent_json=agent_json)),
            (SDR_PREFLIGHT_ACTIVITY, PreflightInput(agent_json=agent_json)),
            (SDR_FETCH_METADATA_ACTIVITY, MetadataInput(agent_json=agent_json)),
        )
        for name, input_obj in cases:
            handler = _StubHandler()
            activity = self._by_name(handler)[name]
            with (
                mock.patch(
                    "application_sdk.execution._temporal.sdr.get_infrastructure",
                    return_value=fake_infra,
                ),
                mock.patch(
                    "application_sdk.execution._temporal.sdr.CredentialResolver",
                    return_value=resolver,
                ),
                mock.patch(
                    "application_sdk.execution._temporal.sdr.check_secret_store_access",
                    new=mock.AsyncMock(return_value=_PASS_SECRET_STORE),
                ),
            ):
                await activity(input_obj)
            captured = handler.context_during_call
            assert captured is not None
            assert captured.get_credential("username") == "u"  # type: ignore[attr-defined]

    async def test_no_agent_json_skips_resolution(self) -> None:
        from application_sdk.handler.contracts import HandlerCredential

        handler = _StubHandler()
        preflight = self._by_name(handler)[SDR_PREFLIGHT_ACTIVITY]

        creds = [HandlerCredential(key="api_key", value="secret123")]
        with mock.patch(
            "application_sdk.execution._temporal.sdr.CredentialResolver",
        ) as resolver_cls:
            await preflight(PreflightInput(credentials=creds))

        # No reference => no resolver instantiation; creds pass through as-is.
        resolver_cls.assert_not_called()
        assert handler.preflight_input is not None
        assert handler.preflight_input.credentials == creds

    async def test_unpopulated_agent_json_skips_resolution(self) -> None:
        """A present-but-unpopulated spec (no ``agent-name``) is truthy as a
        pydantic model but must NOT resolve — otherwise an empty spec resolves to
        a bundle of empty strings and overwrites real credentials. Matches the
        ``is_populated()`` population gate in ``CredentialRef.resolve``."""
        from application_sdk.handler.contracts import HandlerCredential

        handler = _StubHandler()
        preflight = self._by_name(handler)[SDR_PREFLIGHT_ACTIVITY]

        creds = [HandlerCredential(key="api_key", value="secret123")]
        # secret-path but no agent-name => is_populated() is False.
        with mock.patch(
            "application_sdk.execution._temporal.sdr.CredentialResolver",
        ) as resolver_cls:
            await preflight(
                PreflightInput(credentials=creds, agent_json={"secret-path": "p"})
            )

        resolver_cls.assert_not_called()
        assert handler.preflight_input is not None
        assert handler.preflight_input.credentials == creds

    async def test_preflight_no_secret_store_yields_not_ready(self) -> None:
        # Preflight surfaces "no secret store" as a failed check row (NOT_READY),
        # not a raised error — the interactive UI shows the reason cleanly.
        handler = _StubHandler()
        preflight = self._by_name(handler)[SDR_PREFLIGHT_ACTIVITY]

        fake_infra = mock.MagicMock()
        fake_infra.secret_store = None
        with mock.patch(
            "application_sdk.execution._temporal.sdr.get_infrastructure",
            return_value=fake_infra,
        ):
            result = await preflight(
                PreflightInput(agent_json={"agent-name": "acme", "secret-path": "p"})
            )
        assert result.status == PreflightStatus.NOT_READY
        assert handler.preflight_input is None  # handler never ran
        secret_check = next(c for c in result.checks if c.name == "Secret store")
        assert secret_check.passed is False

    async def test_preflight_reachable_but_nothing_resolved_still_runs_checks(
        self,
    ) -> None:
        # A reachable store that resolved nothing is NOT fatal: every field falls
        # back to its literal value, so a customer who put raw secrets directly in
        # the config can still connect. Preflight keeps the failed secret-store row
        # but still runs the handler's connectivity/schema/tables checks (unlike an
        # unreachable store, which short-circuits).
        handler = _StubHandler()
        preflight = self._by_name(handler)[SDR_PREFLIGHT_ACTIVITY]

        resolver = mock.MagicMock()
        resolver.resolve_raw = mock.AsyncMock(return_value={"username": "literal-user"})
        fake_infra = mock.MagicMock()
        fake_infra.secret_store = mock.MagicMock(name="SecretStore")
        # Non-fatal: the probe already substituted (falling back to literals), so
        # ``resolved`` carries those credentials and the activity reuses them
        # rather than re-fetching from the store.
        zero_resolved = SecretStoreCheckResult(
            passed=False,
            store_down=False,
            fatal=False,
            substituted=0,
            message="Secret store is reachable, but no secret was resolved.",
            resolved={"username": "literal-user"},
        )
        with (
            mock.patch(
                "application_sdk.execution._temporal.sdr.get_infrastructure",
                return_value=fake_infra,
            ),
            mock.patch(
                "application_sdk.execution._temporal.sdr.CredentialResolver",
                return_value=resolver,
            ),
            mock.patch(
                "application_sdk.execution._temporal.sdr.check_secret_store_access",
                new=mock.AsyncMock(return_value=zero_resolved),
            ),
        ):
            result = await preflight(
                PreflightInput(agent_json={"agent-name": "acme", "secret-path": "p"})
            )

        # Handler ran (connectivity attempted with literal values) — NOT short-circuited.
        assert handler.preflight_input is not None
        # Credentials came from the reused ``resolved`` dict, so the probe's
        # already-fetched bundle was NOT re-resolved from the store.
        resolver.resolve_raw.assert_not_awaited()
        assert {c.key: c.value for c in handler.preflight_input.credentials} == {
            "username": "literal-user"
        }
        # The failed secret-store row is still surfaced for visibility...
        secret_check = next(c for c in result.checks if c.name == "Secret store")
        assert secret_check.passed is False
        # ...but it does not force the gate: the handler's verdict stands.
        assert result.status == PreflightStatus.READY

    async def test_auth_raises_when_secret_store_unavailable(self) -> None:
        from application_sdk.errors.leaves import DependencyUnavailableError

        # test_auth (unlike preflight) has no check-row path, so a missing secret
        # store still raises — surfaced as a classified error by the dispatcher.
        handler = _StubHandler()
        auth = self._by_name(handler)[SDR_TEST_AUTH_ACTIVITY]

        fake_infra = mock.MagicMock()
        fake_infra.secret_store = None
        with mock.patch(
            "application_sdk.execution._temporal.sdr.get_infrastructure",
            return_value=fake_infra,
        ):
            with pytest.raises(DependencyUnavailableError):
                await auth(
                    AuthInput(agent_json={"agent-name": "acme", "secret-path": "p"})
                )
        assert handler.auth_input is None

    async def test_resolver_returning_none_yields_empty_credentials(self) -> None:
        """``resolve_raw`` → ``None`` (nothing at the secret path) resolves to an
        empty credential list, not a crash — guarding the ``or {}`` fallback so a
        future refactor dropping it is caught."""
        handler = _StubHandler()
        preflight = self._by_name(handler)[SDR_PREFLIGHT_ACTIVITY]

        resolver = mock.MagicMock()
        resolver.resolve_raw = mock.AsyncMock(return_value=None)
        fake_infra = mock.MagicMock()
        fake_infra.secret_store = mock.MagicMock(name="SecretStore")

        with (
            mock.patch(
                "application_sdk.execution._temporal.sdr.get_infrastructure",
                return_value=fake_infra,
            ),
            mock.patch(
                "application_sdk.execution._temporal.sdr.CredentialResolver",
                return_value=resolver,
            ),
            mock.patch(
                "application_sdk.execution._temporal.sdr.check_secret_store_access",
                new=mock.AsyncMock(return_value=_PASS_SECRET_STORE),
            ),
        ):
            result = await preflight(
                PreflightInput(agent_json={"agent-name": "acme", "secret-path": "p"})
            )

        assert result.status == PreflightStatus.READY
        resolver.resolve_raw.assert_awaited_once()
        assert handler.preflight_input is not None
        assert handler.preflight_input.credentials == []


class TestSdrPreflightObjectStoreChecks:
    """The SDR preflight_check activity folds the customer object-store access
    check into the handler's PreflightOutput as extra UI check rows, and
    downgrades the verdict on failure. Only the preflight path gets this."""

    @staticmethod
    def _preflight(handler: Handler):
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        return by_name[SDR_PREFLIGHT_ACTIVITY]

    async def test_no_object_store_checks_when_sdr_off(self) -> None:
        """check_object_store_access returns [] (SDR off) → output unchanged."""
        handler = _StubHandler()
        preflight = self._preflight(handler)

        with mock.patch(
            "application_sdk.execution._temporal.sdr.check_object_store_access",
            mock.AsyncMock(return_value=[]),
        ):
            result = await preflight(PreflightInput(credentials=[]))

        assert result.status == PreflightStatus.READY
        assert result.checks == []

    async def test_passed_object_store_check_appended_names(self) -> None:
        """Passing probes append two named check rows and keep status READY."""
        from application_sdk.storage.preflight import ObjectStoreCheckResult

        handler = _StubHandler()
        preflight = self._preflight(handler)

        results = [
            ObjectStoreCheckResult(
                label="deployment", binding_name="objectstore", passed=True
            ),
            ObjectStoreCheckResult(
                label="upstream", binding_name="atlan-objectstore", passed=True
            ),
        ]
        with mock.patch(
            "application_sdk.execution._temporal.sdr.check_object_store_access",
            mock.AsyncMock(return_value=results),
        ):
            result = await preflight(PreflightInput(credentials=[]))

        assert result.status == PreflightStatus.READY
        names = [c.name for c in result.checks]
        assert names == [
            "Deployment reachability",
            "Deployment object store",
            "Metadata upload connectivity",
        ]
        # Names avoid the "SDR" acronym so the frontend title-caser doesn't
        # render it as "S D R"; the SDR context lives in the messages.
        assert not any("SDR" in n for n in names)
        messages = [c.message for c in result.checks]
        assert messages == [
            "The SDR worker is reachable.",
            "Configured object store is accessible. Read and write access confirmed.",
            "SDR worker can upload metadata to Atlan.",
        ]
        assert all(c.passed for c in result.checks)
        # The probe's elapsed time is folded into the output's duration
        # (output.total_duration_ms += elapsed_ms). Assert the accumulation is
        # observable; a strict > 0 would be clock-flaky on a fast probe.
        assert result.total_duration_ms >= 0.0

    async def test_failed_object_store_check_downgrades_ready(self) -> None:
        """A failed probe appends a typed-error row and downgrades READY→NOT_READY."""
        from application_sdk.errors.categories import FailureCategory
        from application_sdk.storage.preflight import ObjectStoreCheckResult

        handler = _StubHandler()
        preflight = self._preflight(handler)

        results = [
            ObjectStoreCheckResult(
                label="deployment",
                binding_name="objectstore",
                passed=False,
                error_class="permission denied",
                cause="403 Forbidden",
                hint="grant get/put",
                failed_operation="write",
            ),
        ]
        with mock.patch(
            "application_sdk.execution._temporal.sdr.check_object_store_access",
            mock.AsyncMock(return_value=results),
        ):
            result = await preflight(PreflightInput(credentials=[]))

        assert result.status == PreflightStatus.NOT_READY
        # Row 0 is the "SDR deployment reachable" marker; row 1 is the failed probe.
        assert len(result.checks) == 2
        assert result.checks[0].passed is True
        assert result.checks[0].message == "The SDR worker is reachable."
        check = result.checks[1]
        assert check.passed is False
        assert check.error is not None
        # Permission bucket → AppPermissionDeniedError (USER, not retryable).
        from application_sdk.errors.categories import Audience

        assert check.error.category == FailureCategory.PERMISSION
        assert check.error.code == "PERMISSION"
        assert check.error.audience == Audience.USER
        assert check.error.retryable is False
        # The class-specific remediation is carried, not discarded.
        assert check.error.suggested_action is not None
        assert "read and write access" in check.error.suggested_action
        # Probe context rides along as evidence via to_failure_details().
        assert check.error.evidence.get("resource") == "objectstore"
        assert check.error.evidence.get("required_action") == "read and write"
        # Simple, non-technical failure copy (no probe internals in the UI).
        assert "not accessible" in check.resolved_message
        assert "403" not in check.resolved_message
        # The banner carries the real reason (typed + string), not "not_ready".
        assert result.message == "Configured object store is not accessible."
        assert result.error is not None
        assert result.error.category == FailureCategory.PERMISSION
        assert result.resolved_message == "Configured object store is not accessible."

    async def test_failed_object_store_maps_auth_and_availability_leaves(self) -> None:
        """Classifier buckets map onto existing USER/PLATFORM leaves, not one retryable leaf."""
        from application_sdk.errors.categories import Audience, FailureCategory
        from application_sdk.storage.preflight import ObjectStoreCheckResult

        handler = _StubHandler()
        preflight = self._preflight(handler)

        results = [
            ObjectStoreCheckResult(
                label="deployment",
                binding_name="objectstore",
                passed=False,
                error_class="invalid credentials",
            ),
            ObjectStoreCheckResult(
                label="upstream",
                binding_name="atlan-objectstore",
                passed=False,
                error_class="connectivity / unknown",
            ),
        ]
        with mock.patch(
            "application_sdk.execution._temporal.sdr.check_object_store_access",
            mock.AsyncMock(return_value=results),
        ):
            result = await preflight(PreflightInput(credentials=[]))

        auth_row, upstream_row = result.checks[1], result.checks[2]
        assert auth_row.error is not None
        assert auth_row.error.category == FailureCategory.AUTH
        assert auth_row.error.audience == Audience.USER
        assert auth_row.error.retryable is False
        assert auth_row.error.suggested_action is not None
        assert "expired" in auth_row.error.suggested_action

        assert upstream_row.error is not None
        assert upstream_row.error.category == FailureCategory.DEPENDENCY_UNAVAILABLE
        assert upstream_row.error.audience == Audience.PLATFORM
        assert upstream_row.error.retryable is True
        assert upstream_row.error.evidence.get("target") == "atlan-objectstore"

        # Banner is pinned to the first object-store failure (deployment/auth),
        # not rescanned from all checks.
        assert result.error is not None
        assert result.error.category == FailureCategory.AUTH

    async def test_deployment_connectivity_is_source_unavailable(self) -> None:
        """Customer-owned store outage stays SOURCE_UNAVAILABLE / USER."""
        from application_sdk.errors.categories import Audience, FailureCategory
        from application_sdk.storage.preflight import ObjectStoreCheckResult

        handler = _StubHandler()
        preflight = self._preflight(handler)
        results = [
            ObjectStoreCheckResult(
                label="deployment",
                binding_name="objectstore",
                passed=False,
                error_class="connectivity / unknown",
            ),
        ]
        with mock.patch(
            "application_sdk.execution._temporal.sdr.check_object_store_access",
            mock.AsyncMock(return_value=results),
        ):
            result = await preflight(PreflightInput(credentials=[]))

        check = result.checks[1]
        assert check.error is not None
        assert check.error.category == FailureCategory.SOURCE_UNAVAILABLE
        assert check.error.audience == Audience.USER
        assert check.error.retryable is True
        assert check.error.evidence.get("endpoint") == "objectstore"

    async def test_not_configured_object_store_is_non_retryable_precondition(
        self,
    ) -> None:
        """A missing binding is a permanent config gap, not a transient outage.

        The probe emits ``"not configured"`` (bypassing the classifier); it must
        map to PRECONDITION / retryable=False, and it must surface the probe's
        own "add a Dapr component" hint as the customer step rather than
        discarding it behind a generic connectivity action.

        Only the ``deployment`` label is exercised because only it can reach
        this state: ``check_object_store_access`` appends the upstream probe
        solely when ``infra.upstream_storage is not None``, so an unconfigured
        upstream store produces no row at all rather than a not-configured one.
        ``test_every_producer_bucket_maps_to_a_leaf`` covers the mapper's
        label-agnostic handling directly.
        """
        from application_sdk.errors.categories import FailureCategory
        from application_sdk.storage.preflight import (
            _NOT_CONFIGURED,
            ObjectStoreCheckResult,
        )

        handler = _StubHandler()
        preflight = self._preflight(handler)
        results = [
            ObjectStoreCheckResult(
                label="deployment",
                binding_name="objectstore",
                passed=False,
                error_class=_NOT_CONFIGURED,
                hint="Add a Dapr component named 'objectstore'.",
            ),
        ]
        with mock.patch(
            "application_sdk.execution._temporal.sdr.check_object_store_access",
            mock.AsyncMock(return_value=results),
        ):
            result = await preflight(PreflightInput(credentials=[]))

        row = result.checks[1]
        assert row.error is not None
        assert row.error.category == FailureCategory.PRECONDITION
        assert row.error.retryable is False
        # The probe's engineer hint becomes the customer step.
        assert row.error.suggested_action == "Add a Dapr component named 'objectstore'."

    def test_every_producer_bucket_maps_to_a_leaf(self) -> None:
        """Every bucket the producer can emit maps to a typed, correctly-retryable leaf.

        Iterates ``_OBJECT_STORE_ERROR_CLASSES``, which the producer *derives*
        from its own rule table (plus the ``"not configured"`` path that
        bypasses the classifier) — so adding a classifier bucket fails here
        rather than silently falling through ``_object_store_failure`` to the
        retryable connectivity default, which is how ``"not configured"``
        originally slipped past it. A renamed bucket fails too: the mapper keys
        off the literals.

        Availability buckets stay retryable; a permanent config gap must not.
        """
        from application_sdk.execution._temporal.sdr import (
            _OBJECT_STORE_ACTIONS,
            _object_store_failure,
        )
        from application_sdk.storage.preflight import (
            _NOT_CONFIGURED,
            _OBJECT_STORE_ERROR_CLASSES,
            _classify_access_error,
        )

        # The declared surface has to be the *real* one: anything the classifier
        # returns from outside the rule table (an early return added later)
        # escapes the loop below, so pin the two together.
        classifier_buckets = {
            _classify_access_error(exc)[0]
            for exc in (
                Exception("403 AccessDenied"),
                Exception("401 SignatureDoesNotMatch"),
                Exception("connection refused"),
            )
        }
        assert classifier_buckets <= _OBJECT_STORE_ERROR_CLASSES
        assert _NOT_CONFIGURED in _OBJECT_STORE_ERROR_CLASSES

        for bucket in sorted(_OBJECT_STORE_ERROR_CLASSES):
            for label in ("deployment", "upstream"):
                err = _object_store_failure(
                    label=label,
                    error_class=bucket,
                    binding_name="binding",
                    message="failed",
                    hint="do the thing",
                )
                fd = err.to_failure_details()
                if bucket == _NOT_CONFIGURED:
                    # A permanent config gap is never advertised as retryable,
                    # and the probe's own hint is the customer step.
                    assert fd.retryable is False, f"{label}/{bucket} was retryable"
                    assert fd.suggested_action == "do the thing"
                    continue
                # Asserting the bucket's *own* action, not merely that some
                # action exists: ``_object_store_failure`` defaults an unknown
                # bucket to the connectivity entry, so a truthiness check would
                # pass for a bucket nobody mapped.
                assert (label, bucket) in _OBJECT_STORE_ACTIONS, (
                    f"{label}/{bucket} has no action — _object_store_failure "
                    "would silently serve the connectivity default"
                )
                assert fd.suggested_action == _OBJECT_STORE_ACTIONS[(label, bucket)]

    async def test_banner_pins_object_store_over_nonfatal_secret_store(self) -> None:
        """A non-fatal secret-store row must not become the aggregate banner."""
        from application_sdk.errors.categories import FailureCategory
        from application_sdk.storage.preflight import ObjectStoreCheckResult

        handler = _StubHandler()
        preflight = self._preflight(handler)

        zero_resolved = SecretStoreCheckResult(
            passed=False,
            store_down=False,
            fatal=False,
            substituted=0,
            message="Secret store is reachable, but no secret was resolved.",
            suggested_action="Check that the configured secret keys exist.",
            resolved={"username": "literal-user"},
        )
        object_store = [
            ObjectStoreCheckResult(
                label="deployment",
                binding_name="objectstore",
                passed=False,
                error_class="permission denied",
            ),
        ]
        fake_infra = mock.MagicMock()
        fake_infra.secret_store = mock.MagicMock(name="SecretStore")
        with (
            mock.patch(
                "application_sdk.execution._temporal.sdr.get_infrastructure",
                return_value=fake_infra,
            ),
            mock.patch(
                "application_sdk.execution._temporal.sdr.check_secret_store_access",
                new=mock.AsyncMock(return_value=zero_resolved),
            ),
            mock.patch(
                "application_sdk.execution._temporal.sdr.check_object_store_access",
                mock.AsyncMock(return_value=object_store),
            ),
        ):
            result = await preflight(
                PreflightInput(agent_json={"agent-name": "acme", "secret-path": "p"})
            )

        secret_check = next(c for c in result.checks if c.name == "Secret store")
        assert secret_check.passed is False
        object_check = next(
            c for c in result.checks if c.name == "Deployment object store"
        )
        assert object_check.passed is False
        assert result.status == PreflightStatus.NOT_READY
        assert result.error is not None
        assert result.error.category == FailureCategory.PERMISSION
        assert result.message == "Configured object store is not accessible."
        assert result.resolved_message == "Configured object store is not accessible."

    async def test_failed_check_does_not_upgrade_partial(self) -> None:
        """A handler PARTIAL/NOT_READY verdict is left untouched on failure."""
        from application_sdk.storage.preflight import ObjectStoreCheckResult

        class _PartialHandler(_StubHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                self.preflight_input = input
                return PreflightOutput(status=PreflightStatus.PARTIAL, checks=[])

        handler = _PartialHandler()
        preflight = self._preflight(handler)

        results = [
            ObjectStoreCheckResult(
                label="deployment",
                binding_name="objectstore",
                passed=False,
                error_class="connectivity / unknown",
                cause="timeout",
                hint="check network",
                failed_operation="connectivity",
            ),
        ]
        with mock.patch(
            "application_sdk.execution._temporal.sdr.check_object_store_access",
            mock.AsyncMock(return_value=results),
        ):
            result = await preflight(PreflightInput(credentials=[]))

        # PARTIAL is preserved — the downgrade only fires from READY.
        assert result.status == PreflightStatus.PARTIAL
        # Reachable marker + the failed probe row.
        assert len(result.checks) == 2

    async def test_augmentation_never_breaks_handler_result(self) -> None:
        """An unexpected error in the object-store check is a no-op."""
        handler = _StubHandler()
        preflight = self._preflight(handler)

        with mock.patch(
            "application_sdk.execution._temporal.sdr.check_object_store_access",
            mock.AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await preflight(PreflightInput(credentials=[]))

        # Handler's own result stands; no crash, no extra checks.
        assert result.status == PreflightStatus.READY
        assert result.checks == []


class TestSecretStoreCheckRow:
    """``_secret_store_check_row`` maps a probe result onto the UI check row —
    the failure category must come from ``store_down`` (is the store the
    blocker?), not from whether a fetch happened."""

    def test_store_down_is_source_unavailable(self) -> None:
        # The customer owns the secret store, so a store outage is a
        # SourceUnavailableError (SOURCE_UNAVAILABLE / audience=USER), not the
        # DEPENDENCY_UNAVAILABLE/PLATFORM pair that would route to the app team.
        from application_sdk.credentials.agent import SecretStoreCheckResult
        from application_sdk.errors.categories import Audience, FailureCategory

        row = _secret_store_check_row(
            SecretStoreCheckResult(
                passed=False,
                store_down=True,
                fatal=True,
                substituted=0,
                message="Configured secret store is not accessible.",
                suggested_action="Check that the secret store is running.",
            )
        )
        assert row.passed is False
        assert row.error is not None
        assert row.error.category == FailureCategory.SOURCE_UNAVAILABLE
        assert row.error.audience == Audience.USER
        assert row.error.suggested_action == "Check that the secret store is running."

    def test_config_gap_is_precondition_not_dependency_unavailable(self) -> None:
        # A multi-key spec with no secret-path is fatal (creds can't resolve) but
        # the store is never contacted — it is a PRECONDITION config gap, and must
        # NOT be miscategorised as a store outage.
        from application_sdk.credentials.agent import SecretStoreCheckResult
        from application_sdk.errors.categories import FailureCategory

        row = _secret_store_check_row(
            SecretStoreCheckResult(
                passed=False,
                store_down=False,
                fatal=True,
                substituted=0,
                message="Multi-key credentials require a secret-path...",
            )
        )
        assert row.passed is False
        assert row.error is not None
        assert row.error.category == FailureCategory.PRECONDITION
        # A config gap is permanent — it must not be advertised as retryable.
        assert row.error.retryable is False

    def test_no_store_configured_is_a_non_retryable_precondition(self) -> None:
        # "No secret store configured" is a deployment config gap, not a store
        # outage: PRECONDITION / retryable=False, never a retryable
        # SourceUnavailableError. store_down is False for exactly this reason.
        from application_sdk.credentials.agent import SecretStoreCheckResult
        from application_sdk.errors.categories import FailureCategory

        row = _secret_store_check_row(
            SecretStoreCheckResult(
                passed=False,
                store_down=False,
                fatal=True,
                substituted=0,
                message="Secret store is not configured for the SDR deployment.",
                suggested_action="Configure a secret store on the SDR deployment.",
            )
        )
        assert row.error is not None
        assert row.error.category == FailureCategory.PRECONDITION
        assert row.error.retryable is False

    def test_passed_row_has_no_error(self) -> None:
        from application_sdk.credentials.agent import SecretStoreCheckResult

        row = _secret_store_check_row(
            SecretStoreCheckResult(
                passed=True,
                store_down=False,
                fatal=False,
                substituted=2,
                message="Secret store reachable; 2 secret(s) resolved.",
            )
        )
        assert row.passed is True
        assert row.error is None
