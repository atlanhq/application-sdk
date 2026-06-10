"""Tests for application_sdk.app.context public contracts.

These tests guard against the BLDX-1129 bug class:
- runtime errors caused by inline imports referencing renamed/removed symbols,
- contract drift between AppContext / TaskExecutionContext and their dependencies,
- silently swallowed failures inside helpers.

Side effects (Temporal, secret store, state store, observability ContextVars)
are mocked. The tests attach to public contracts only; no internal helpers
are exercised directly except where the public surface delegates to them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.app.base_errors import (
    SecretStoreNotConfiguredError,
    StateStoreNotConfiguredError,
)
from application_sdk.app.context import (
    AppContext,
    AppMetadata,
    ChildAppContext,
    TaskExecutionContext,
    _is_atlan_logger,
    _is_in_workflow,
    _utc_now,
    _WorkflowSafeLogger,
)
from application_sdk.contracts.base import HeartbeatDetails
from application_sdk.observability.context import (
    ExecutionContext,
    set_execution_context,
)
from application_sdk.observability.correlation import (
    CorrelationContext,
    set_correlation_context,
)
from application_sdk.testing.mocks import (
    MockCredentialStore,
    MockHeartbeatController,
    MockSecretStore,
    MockStateStore,
)

# ---------------------------------------------------------------------------
# AppMetadata
# ---------------------------------------------------------------------------


class TestAppMetadata:
    def test_defaults_are_independent_instances(self) -> None:
        """Default factory must not be shared mutable state across instances."""
        a = AppMetadata()
        b = AppMetadata()
        a.tags.append("x")
        a.properties["k"] = "v"
        assert b.tags == []
        assert b.properties == {}

    def test_explicit_values_round_trip(self) -> None:
        m = AppMetadata(tags=["t1"], properties={"k": "v"})
        assert m.tags == ["t1"]
        assert m.properties == {"k": "v"}


# ---------------------------------------------------------------------------
# AppContext: identity, defaults, properties
# ---------------------------------------------------------------------------


class TestAppContextIdentity:
    def test_correlation_id_defaults_to_run_id(self) -> None:
        ctx = AppContext(app_name="a", app_version="1", run_id="run-42")
        assert ctx.correlation_id == "run-42"

    def test_correlation_id_preserved_when_explicit(self) -> None:
        ctx = AppContext(
            app_name="a",
            app_version="1",
            run_id="run-1",
            correlation_id="trace-abc",
        )
        assert ctx.correlation_id == "trace-abc"

    def test_run_id_str_alias(self) -> None:
        ctx = AppContext(app_name="a", app_version="1", run_id="X")
        assert ctx.run_id_str == "X" == ctx.run_id

    def test_run_id_default_is_unique(self) -> None:
        a = AppContext(app_name="a", app_version="1")
        b = AppContext(app_name="a", app_version="1")
        assert a.run_id != b.run_id
        assert isinstance(a.run_id, str) and len(a.run_id) > 0

    def test_started_at_is_utc_aware(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        assert ctx.started_at.tzinfo is not None

    def test_is_cancelled_default_false(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        assert ctx.is_cancelled() is False

    def test_is_cancelled_reflects_internal_flag(self) -> None:
        ctx = AppContext(app_name="a", app_version="1", _cancelled=True)
        assert ctx.is_cancelled() is True

    def test_storage_returns_configured_store(self) -> None:
        sentinel = object()
        ctx = AppContext(app_name="a", app_version="1", _storage=sentinel)  # type: ignore[arg-type]
        assert ctx.storage is sentinel

    def test_storage_returns_none_by_default(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        assert ctx.storage is None

    def test_upstream_storage_returns_configured_store(self) -> None:
        sentinel = object()
        ctx = AppContext(app_name="a", app_version="1", _upstream_storage=sentinel)  # type: ignore[arg-type]
        assert ctx.upstream_storage is sentinel

    def test_upstream_storage_returns_none_by_default(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        assert ctx.upstream_storage is None


# ---------------------------------------------------------------------------
# AppContext: state store contract
# ---------------------------------------------------------------------------


class TestAppContextStateStore:
    @pytest.mark.asyncio
    async def test_save_state_namespaces_key(self) -> None:
        store = MockStateStore()
        ctx = AppContext(
            app_name="myapp", app_version="1", run_id="r1", _state_store=store
        )
        await ctx.save_state("foo", {"x": 1})
        assert store.get_save_calls() == [("myapp:r1:foo", {"x": 1})]

    @pytest.mark.asyncio
    async def test_load_state_namespaces_key(self) -> None:
        store = MockStateStore()
        await store.save("myapp:r1:foo", {"x": 1})
        ctx = AppContext(
            app_name="myapp", app_version="1", run_id="r1", _state_store=store
        )
        assert await ctx.load_state("foo") == {"x": 1}
        assert store.get_load_calls() == ["myapp:r1:foo"]

    @pytest.mark.asyncio
    async def test_save_state_prefixes_deployment_name_when_set(self) -> None:
        """In deployed environments keys are scoped by deployment identity."""
        store = MockStateStore()
        ctx = AppContext(
            app_name="myapp", app_version="1", run_id="r1", _state_store=store
        )
        with patch("application_sdk.app.context.DEPLOYMENT_NAME", "myapp-dep-1"):
            await ctx.save_state("foo", {"x": 1})
        assert store.get_save_calls() == [("myapp-dep-1:r1:foo", {"x": 1})]

    @pytest.mark.asyncio
    async def test_save_and_load_keys_consistent_with_deployment_name(self) -> None:
        """Reads and writes within one run use the same key shape."""
        store = MockStateStore()
        ctx = AppContext(
            app_name="myapp", app_version="1", run_id="r1", _state_store=store
        )
        with patch("application_sdk.app.context.DEPLOYMENT_NAME", "myapp-dep-1"):
            await ctx.save_state("foo", {"x": 1})
            assert await ctx.load_state("foo") == {"x": 1}
        assert store.get_load_calls() == ["myapp-dep-1:r1:foo"]

    @pytest.mark.asyncio
    async def test_state_key_falls_back_to_app_name_when_local(self) -> None:
        """Unset/local deployment name keeps the pre-namespacing key shape."""
        store = MockStateStore()
        ctx = AppContext(
            app_name="myapp", app_version="1", run_id="r1", _state_store=store
        )
        with patch("application_sdk.app.context.DEPLOYMENT_NAME", "local"):
            await ctx.save_state("foo", {"x": 1})
        assert store.get_save_calls() == [("myapp:r1:foo", {"x": 1})]

    @pytest.mark.asyncio
    async def test_load_state_returns_none_when_missing(self) -> None:
        ctx = AppContext(app_name="a", app_version="1", _state_store=MockStateStore())
        assert await ctx.load_state("nope") is None

    @pytest.mark.asyncio
    async def test_save_state_without_store_raises(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        with pytest.raises(StateStoreNotConfiguredError) as exc_info:
            await ctx.save_state("k", {})
        assert exc_info.value.message == "No state store configured"

    @pytest.mark.asyncio
    async def test_load_state_without_store_raises(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        with pytest.raises(StateStoreNotConfiguredError) as exc_info:
            await ctx.load_state("k")
        assert exc_info.value.message == "No state store configured"


# ---------------------------------------------------------------------------
# AppContext: secret store contract
# ---------------------------------------------------------------------------


class TestAppContextSecretStore:
    @pytest.mark.asyncio
    async def test_get_secret_delegates_to_store(self) -> None:
        ss = MockSecretStore({"db_password": "hunter2"})
        ctx = AppContext(app_name="a", app_version="1", _secret_store=ss)
        assert await ctx.get_secret("db_password") == "hunter2"
        assert ss.get_get_calls() == ["db_password"]

    @pytest.mark.asyncio
    async def test_get_secret_without_store_raises(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        with pytest.raises(SecretStoreNotConfiguredError) as exc_info:
            await ctx.get_secret("anything")
        assert exc_info.value.message == "No secret store configured"

    @pytest.mark.asyncio
    async def test_get_secret_optional_returns_none_when_no_store(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        assert await ctx.get_secret_optional("anything") is None

    @pytest.mark.asyncio
    async def test_get_secret_optional_delegates_when_store_present(self) -> None:
        ss = MockSecretStore({"k": "v"})
        ctx = AppContext(app_name="a", app_version="1", _secret_store=ss)
        assert await ctx.get_secret_optional("k") == "v"
        assert await ctx.get_secret_optional("missing") is None
        assert ss.get_get_optional_calls() == ["k", "missing"]


# ---------------------------------------------------------------------------
# AppContext: credential resolution
# ---------------------------------------------------------------------------


class TestAppContextCredentialResolution:
    @pytest.mark.asyncio
    async def test_resolve_credential_returns_typed_credential(self) -> None:
        cred_store = MockCredentialStore()
        ref = cred_store.add_basic("svc", username="u", password="p")
        ctx = AppContext(
            app_name="a", app_version="1", _secret_store=cred_store.secret_store
        )
        result = await ctx.resolve_credential(ref)
        # We don't pin the exact class, just the credential contract:
        assert getattr(result, "username", None) == "u"
        assert getattr(result, "password", None) == "p"

    @pytest.mark.asyncio
    async def test_resolve_credential_raw_returns_dict(self) -> None:
        cred_store = MockCredentialStore()
        ref = cred_store.add_api_key("svc", api_key="abc")
        ctx = AppContext(
            app_name="a", app_version="1", _secret_store=cred_store.secret_store
        )
        raw = await ctx.resolve_credential_raw(ref)
        assert isinstance(raw, dict)
        assert raw.get("api_key") == "abc"
        assert raw.get("type") == "api_key"

    @pytest.mark.asyncio
    async def test_resolve_credential_without_store_raises(self) -> None:
        cred_store = MockCredentialStore()
        ref = cred_store.add_basic("svc", username="u", password="p")
        ctx = AppContext(app_name="a", app_version="1")
        with pytest.raises(SecretStoreNotConfiguredError) as exc_info:
            await ctx.resolve_credential(ref)
        assert exc_info.value.message == "No secret store configured"

    @pytest.mark.asyncio
    async def test_resolve_credential_raw_without_store_raises(self) -> None:
        cred_store = MockCredentialStore()
        ref = cred_store.add_basic("svc", username="u", password="p")
        ctx = AppContext(app_name="a", app_version="1")
        with pytest.raises(SecretStoreNotConfiguredError) as exc_info:
            await ctx.resolve_credential_raw(ref)
        assert exc_info.value.message == "No secret store configured"


# ---------------------------------------------------------------------------
# AppContext: logger property + log_* convenience wrappers
# ---------------------------------------------------------------------------


class TestAppContextLogger:
    def test_logger_is_lazy_and_memoized(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        assert ctx._logger is None
        first = ctx.logger
        second = ctx.logger
        assert first is second
        assert isinstance(first, _WorkflowSafeLogger)

    def test_logger_carries_app_context(self) -> None:
        ctx = AppContext(
            app_name="myapp",
            app_version="1",
            run_id="r1",
            correlation_id="c1",
        )
        logger = ctx.logger
        assert logger._context["app_name"] == "myapp"
        assert logger._context["run_id"] == "r1"
        assert logger._context["correlation_id"] == "c1"

    def test_log_info_delegates(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        fake_logger = MagicMock()
        ctx._logger = fake_logger
        ctx.log_info("hello", k="v")
        fake_logger.info.assert_called_once_with("hello", k="v")

    def test_log_debug_delegates(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        fake_logger = MagicMock()
        ctx._logger = fake_logger
        ctx.log_debug("d", x=1)
        fake_logger.debug.assert_called_once_with("d", x=1)

    def test_log_warning_delegates(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        fake_logger = MagicMock()
        ctx._logger = fake_logger
        ctx.log_warning("w")
        fake_logger.warning.assert_called_once_with("w")

    def test_log_error_delegates(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        fake_logger = MagicMock()
        ctx._logger = fake_logger
        ctx.log_error("oops", code=500)
        fake_logger.error.assert_called_once_with("oops", code=500)


# ---------------------------------------------------------------------------
# _WorkflowSafeLogger: bind() and activity-context (loguru) path
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_execution_context():
    """Ensure execution-context ContextVar is reset around each test."""
    set_execution_context(ExecutionContext())
    yield
    set_execution_context(ExecutionContext())


class TestWorkflowSafeLoggerBind:
    def test_bind_returns_new_instance(self) -> None:
        log = _WorkflowSafeLogger("n", "app", "run", "corr")
        bound = log.bind(extra="x")
        assert bound is not log
        assert isinstance(bound, _WorkflowSafeLogger)

    def test_bind_merges_context_without_mutating_original(self) -> None:
        log = _WorkflowSafeLogger("n", "app", "run", "corr")
        bound = log.bind(extra="x")
        assert bound._context["extra"] == "x"
        assert bound._context["app_name"] == "app"
        assert "extra" not in log._context


class TestWorkflowSafeLoggerActivityPath:
    """Activity / non-workflow path uses loguru via _get_structlog_logger."""

    def test_info_uses_loguru_when_not_in_workflow(
        self, reset_execution_context
    ) -> None:
        log = _WorkflowSafeLogger("n", "app", "run", "corr")
        fake_loguru = MagicMock()
        log._structlog_logger = fake_loguru
        log.info("hello", foo="bar")
        fake_loguru.info.assert_called_once()
        args, kwargs = fake_loguru.info.call_args
        assert args[0] == "hello"
        assert kwargs.get("foo") == "bar"

    def test_printf_style_args_are_pre_formatted(self, reset_execution_context) -> None:
        log = _WorkflowSafeLogger("n", "app", "run", "corr")
        fake_loguru = MagicMock()
        log._structlog_logger = fake_loguru
        log.info("processed %d records", 5)
        args, _kwargs = fake_loguru.info.call_args
        # The %-args MUST be pre-formatted, not passed through; otherwise loguru
        # silently drops them since it uses {} formatting.
        assert args[0] == "processed 5 records"
        assert len(args) == 1

    def test_correlation_id_pulled_from_contextvar_when_not_in_kwargs(
        self, reset_execution_context
    ) -> None:
        set_correlation_context(CorrelationContext(correlation_id="ctxvar-corr"))
        try:
            log = _WorkflowSafeLogger("n", "app", "run", "")
            fake_loguru = MagicMock()
            log._structlog_logger = fake_loguru
            log.info("hi")
            _args, kwargs = fake_loguru.info.call_args
            assert kwargs.get("correlation_id") == "ctxvar-corr"
        finally:
            set_correlation_context(CorrelationContext(correlation_id=""))

    def test_explicit_correlation_id_kwarg_wins(self, reset_execution_context) -> None:
        set_correlation_context(CorrelationContext(correlation_id="ctxvar-corr"))
        try:
            log = _WorkflowSafeLogger("n", "app", "run", "")
            fake_loguru = MagicMock()
            log._structlog_logger = fake_loguru
            log.info("hi", correlation_id="explicit")
            _args, kwargs = fake_loguru.info.call_args
            assert kwargs.get("correlation_id") == "explicit"
        finally:
            set_correlation_context(CorrelationContext(correlation_id=""))

    def test_each_level_uses_corresponding_logger_method(
        self, reset_execution_context
    ) -> None:
        log = _WorkflowSafeLogger("n", "app", "run", "corr")
        fake_loguru = MagicMock()
        log._structlog_logger = fake_loguru
        log.debug("d")
        log.info("i")
        log.warning("w")
        log.error("e")
        fake_loguru.debug.assert_called_once()
        fake_loguru.info.assert_called_once()
        fake_loguru.warning.assert_called_once()
        fake_loguru.error.assert_called_once()


# ---------------------------------------------------------------------------
# _WorkflowSafeLogger: workflow-context path (BLDX-1129 contract drift)
# ---------------------------------------------------------------------------


class TestWorkflowSafeLoggerWorkflowPath:
    """In workflow context, _log routes to _workflow.logger.

    Two branches matter:
    - Atlan logger (duck-typed via process + logger_name): receives kwargs flat.
    - Stdlib logger fallback: kwargs packed into ``extra=`` to avoid TypeError.
    """

    def test_in_workflow_uses_workflow_logger_stdlib_fallback(
        self, reset_execution_context
    ) -> None:
        # Simulate workflow execution context.
        set_execution_context(ExecutionContext(execution_type="workflow"))
        # Build a stdlib-style logger (NO process/logger_name attrs).
        fake_wf_logger = MagicMock(spec=logging.Logger)
        # spec=Logger ensures hasattr(.., 'process') would normally be True,
        # but the duck-type check requires BOTH process and logger_name.
        # Defensive: explicitly remove logger_name.
        if hasattr(fake_wf_logger, "logger_name"):
            del fake_wf_logger.logger_name

        with patch("application_sdk.app.context._workflow") as wf_mod:
            wf_mod.logger = fake_wf_logger
            log = _WorkflowSafeLogger("n", "app", "run", "corr")
            log.info("hello", user_field="x")

        fake_wf_logger.info.assert_called_once()
        _args, kwargs = fake_wf_logger.info.call_args
        # Stdlib path: arbitrary kwargs go inside `extra`.
        assert "extra" in kwargs
        assert kwargs["extra"].get("user_field") == "x"
        assert kwargs["extra"].get("app_name") == "app"
        # Stdlib path must NOT pass user_field as a top-level kwarg.
        assert "user_field" not in kwargs

    def test_in_workflow_uses_workflow_logger_atlan_path(
        self, reset_execution_context
    ) -> None:
        set_execution_context(ExecutionContext(execution_type="workflow"))
        # AtlanLoggerAdapter shape: has both process and logger_name attrs.
        fake_atlan_logger = MagicMock()
        fake_atlan_logger.process = MagicMock()
        fake_atlan_logger.logger_name = "atlan"

        with patch("application_sdk.app.context._workflow") as wf_mod:
            wf_mod.logger = fake_atlan_logger
            log = _WorkflowSafeLogger("n", "app", "run", "corr")
            log.info("hello", user_field="x")

        fake_atlan_logger.info.assert_called_once()
        _args, kwargs = fake_atlan_logger.info.call_args
        # Atlan path: arbitrary kwargs are passed flat.
        assert kwargs.get("user_field") == "x"
        assert kwargs.get("app_name") == "app"
        assert "extra" not in kwargs

    def test_in_workflow_stdlib_handles_exc_info(self, reset_execution_context) -> None:
        set_execution_context(ExecutionContext(execution_type="workflow"))
        fake_wf_logger = MagicMock(spec=logging.Logger)
        if hasattr(fake_wf_logger, "logger_name"):
            del fake_wf_logger.logger_name

        with patch("application_sdk.app.context._workflow") as wf_mod:
            wf_mod.logger = fake_wf_logger
            log = _WorkflowSafeLogger("n", "app", "run", "corr")
            log.error("boom", exc_info=True, extra_field="y")

        _args, kwargs = fake_wf_logger.error.call_args
        assert kwargs.get("exc_info") is True
        assert kwargs["extra"].get("extra_field") == "y"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestModuleHelpers:
    def test_utc_now_is_timezone_aware(self) -> None:
        assert _utc_now().tzinfo is not None

    def test_is_atlan_logger_duck_type(self) -> None:
        class Atlan:
            def process(self):
                return None

            logger_name = "x"

        class NotAtlan:
            def process(self):
                return None

        assert _is_atlan_logger(Atlan()) is True
        assert _is_atlan_logger(NotAtlan()) is False
        assert _is_atlan_logger(object()) is False

    def test_is_in_workflow_reads_execution_context(
        self, reset_execution_context
    ) -> None:
        assert _is_in_workflow() is False
        set_execution_context(ExecutionContext(execution_type="workflow"))
        assert _is_in_workflow() is True
        set_execution_context(ExecutionContext(execution_type="activity"))
        assert _is_in_workflow() is False


# ---------------------------------------------------------------------------
# ChildAppContext
# ---------------------------------------------------------------------------


class TestChildAppContext:
    def test_correlation_id_inherits_from_parent(self) -> None:
        parent = AppContext(
            app_name="p", app_version="1", run_id="rp", correlation_id="trace"
        )
        child = ChildAppContext(parent=parent, child_app_name="kid")
        assert child.correlation_id == "trace"

    def test_parent_run_id_property(self) -> None:
        parent = AppContext(app_name="p", app_version="1", run_id="rp")
        child = ChildAppContext(parent=parent, child_app_name="kid")
        assert child.parent_run_id == "rp"

    def test_correlation_id_tracks_parent_mutation(self) -> None:
        """Property reads through to parent, so updates propagate."""
        parent = AppContext(
            app_name="p", app_version="1", run_id="rp", correlation_id="a"
        )
        child = ChildAppContext(parent=parent, child_app_name="kid")
        parent.correlation_id = "b"
        assert child.correlation_id == "b"


# ---------------------------------------------------------------------------
# TaskExecutionContext: heartbeat + run_in_thread (BLDX-1129 inline-import)
# ---------------------------------------------------------------------------


class _DemoHeartbeat(HeartbeatDetails):
    """Test-only HeartbeatDetails subclass."""

    chunk_idx: int = 0
    loaded_count: int = 0


class TestTaskExecutionContextHeartbeat:
    def _build(self, hb=None) -> TaskExecutionContext:
        ctx = AppContext(app_name="a", app_version="1")
        return TaskExecutionContext(
            app_context=ctx,
            task_name="t",
            heartbeat_controller=hb or MockHeartbeatController(),
        )

    def test_heartbeat_delegates_to_controller(self) -> None:
        hb = MockHeartbeatController()
        tec = self._build(hb)
        tec.heartbeat(1, 100)
        assert hb.get_heartbeat_calls() == [(1, 100)]

    def test_get_last_heartbeat_details_delegates(self) -> None:
        hb = MockHeartbeatController()
        tec = self._build(hb)
        tec.heartbeat("a", "b")
        assert tec.get_last_heartbeat_details() == ("a", "b")

    def test_get_heartbeat_details_returns_none_when_empty(self) -> None:
        tec = self._build()
        assert tec.get_heartbeat_details(_DemoHeartbeat) is None

    def test_get_heartbeat_details_returns_instance_unchanged(self) -> None:
        hb = MockHeartbeatController()
        tec = self._build(hb)
        original = _DemoHeartbeat(chunk_idx=7, loaded_count=42)
        tec.heartbeat(original)
        out = tec.get_heartbeat_details(_DemoHeartbeat)
        assert out is original

    def test_get_heartbeat_details_reconstructs_from_dict(self) -> None:
        """Temporal returns dicts on retry — must reconstruct via cls(**filtered)."""
        hb = MockHeartbeatController()
        tec = self._build(hb)
        # Simulate Temporal serialization: heartbeat detail came back as dict.
        hb._details = ({"chunk_idx": 3, "loaded_count": 10},)
        out = tec.get_heartbeat_details(_DemoHeartbeat)
        assert isinstance(out, _DemoHeartbeat)
        assert out.chunk_idx == 3
        assert out.loaded_count == 10

    def test_get_heartbeat_details_ignores_unknown_keys(self) -> None:
        """Forward-compat: extra keys from a newer version must be silently dropped."""
        hb = MockHeartbeatController()
        tec = self._build(hb)
        hb._details = (
            {"chunk_idx": 5, "loaded_count": 1, "future_field": "ignore-me"},
        )
        out = tec.get_heartbeat_details(_DemoHeartbeat)
        assert isinstance(out, _DemoHeartbeat)
        assert out.chunk_idx == 5

    def test_get_heartbeat_details_uses_defaults_for_missing_keys(self) -> None:
        """Backward-compat: missing fields fall back to dataclass defaults."""
        hb = MockHeartbeatController()
        tec = self._build(hb)
        hb._details = ({"chunk_idx": 9},)  # loaded_count missing
        out = tec.get_heartbeat_details(_DemoHeartbeat)
        assert isinstance(out, _DemoHeartbeat)
        assert out.chunk_idx == 9
        assert out.loaded_count == 0  # default

    def test_get_heartbeat_details_returns_none_for_unknown_type(self) -> None:
        """When detail is neither cls instance nor dict, returns None."""
        hb = MockHeartbeatController()
        tec = self._build(hb)
        hb._details = ("a-bare-string",)
        assert tec.get_heartbeat_details(_DemoHeartbeat) is None


class TestTaskExecutionContextRunInThread:
    """Guards the BLDX-1129 bug class.

    ``run_in_thread`` does an inline ``from application_sdk.execution.heartbeat
    import run_in_thread`` at call-time (line 571–575). If that symbol is ever
    renamed or removed, this test will fail at runtime — the exact failure mode
    BLDX-1129 wants to catch.
    """

    @pytest.mark.asyncio
    async def test_run_in_thread_executes_sync_function(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        tec = TaskExecutionContext(
            app_context=ctx,
            task_name="t",
            heartbeat_controller=MockHeartbeatController(),
        )
        result = await tec.run_in_thread(lambda x: x * 2, 21)
        assert result == 42

    @pytest.mark.asyncio
    async def test_run_in_thread_propagates_exceptions(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        tec = TaskExecutionContext(
            app_context=ctx,
            task_name="t",
            heartbeat_controller=MockHeartbeatController(),
        )

        def boom() -> None:
            raise ValueError("expected")

        with pytest.raises(ValueError, match="expected"):
            await tec.run_in_thread(boom)

    @pytest.mark.asyncio
    async def test_run_in_thread_passes_args_and_kwargs(self) -> None:
        ctx = AppContext(app_name="a", app_version="1")
        tec = TaskExecutionContext(
            app_context=ctx,
            task_name="t",
            heartbeat_controller=MockHeartbeatController(),
        )

        def add(a: int, b: int, *, mult: int = 1) -> int:
            return (a + b) * mult

        result = await tec.run_in_thread(add, 2, 3, mult=10)
        assert result == 50

    def test_run_in_thread_inline_import_resolves(self) -> None:
        """Direct check that the inline-imported symbol exists.

        This is the BLDX-1129 guard — if ``run_in_thread`` is renamed in
        ``application_sdk.execution.heartbeat`` without updating ``context.py``,
        this assertion fails fast with a clear error.
        """
        from application_sdk.execution.heartbeat import run_in_thread

        assert callable(run_in_thread)


# ---------------------------------------------------------------------------
# Smoke: dataclass typing for AppContext (catches accidental field renames)
# ---------------------------------------------------------------------------


@dataclass
class _SmokeProbe:
    """Just here to make sure the dataclass fields we depend on still exist."""


class TestAppContextDataclassFields:
    def test_required_fields_present(self) -> None:
        # If any of these fields is renamed/removed, every dependent test
        # explodes — but this single assert points at the contract directly.
        ctx = AppContext(app_name="a", app_version="1")
        for attr in (
            "app_name",
            "app_version",
            "run_id",
            "correlation_id",
            "parent_run_id",
            "started_at",
            "metadata",
            "execution_id_prefix",
        ):
            assert hasattr(ctx, attr), f"missing AppContext field: {attr}"
