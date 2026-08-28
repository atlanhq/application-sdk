"""Unit tests for the shared in-process integration fixture set."""

from __future__ import annotations

import ast
import contextlib
import inspect
import os
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from application_sdk.testing.fake_source import HttpFakeSource, HttpFakeSourceFactory
from application_sdk.testing.integration import _errors, fixtures
from application_sdk.testing.integration.fixtures import (
    CLEANUP_INTERCEPTOR_ENV,
    AppExecutor,
    KitOptions,
)

_APP_NAME = "kit-test-app"

# What an adopting conftest gets from ``from ...integration.fixtures import *``.
# Aliased here because this module imports the kit rather than star-importing it.
http_fake_source_factory = fixtures.http_fake_source_factory
reset_http_fake_sources = fixtures.reset_http_fake_sources


def _fn(fixture: Any) -> Any:
    """The plain function behind a pytest fixture definition."""
    return getattr(fixture, "__wrapped__", fixture)


def _params(fixture: Any) -> list[str]:
    return list(inspect.signature(_fn(fixture)).parameters)


class _FakeApp:
    _app_name = _APP_NAME


@dataclass
class _RecordingBackend:
    calls: list[dict[str, Any]]

    async def execute(self, app_cls: Any, input_data: Any, **kwargs: Any) -> str:
        self.calls.append({"app_cls": app_cls, "input_data": input_data, **kwargs})
        return "executed"


class _FakeRegistry:
    def list_apps(self) -> list[str]:
        return [_APP_NAME]


class TestExecutorShim:
    async def test_entry_point_reaches_the_backend(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(  # type: ignore[arg-type]
            _FakeApp, {"x": 1}, entry_point="extract"
        )
        assert backend.calls[0]["entry_point"] == "extract"

    async def test_entry_point_defaults_to_none(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(_FakeApp, {})  # type: ignore[arg-type]
        assert backend.calls[0]["entry_point"] is None

    async def test_context_named_from_app(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(_FakeApp, {})  # type: ignore[arg-type]
        context = backend.calls[0]["context"]
        assert context.app_name == _APP_NAME
        # NOT the app name: AppContext derives correlation_id from run_id and the
        # backend stamps it into the Temporal start memo, so a constant would log
        # every run in the suite under one identity.
        assert context.run_id != _APP_NAME
        UUID(context.run_id)

    async def test_each_run_gets_its_own_identity(self) -> None:
        """Two submissions must be distinguishable in the logs."""
        backend = _RecordingBackend(calls=[])
        executor = AppExecutor(backend=backend)
        await executor.execute_app(_FakeApp, {})  # type: ignore[arg-type]
        await executor.execute_app(_FakeApp, {})  # type: ignore[arg-type]
        first, second = (call["context"] for call in backend.calls)
        assert first.run_id != second.run_id
        assert first.correlation_id != second.correlation_id

    async def test_execution_id_prefix_becomes_run_id(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(  # type: ignore[arg-type]
            _FakeApp, {}, execution_id_prefix="run-7"
        )
        assert backend.calls[0]["context"].run_id == "run-7"

    async def test_execution_id_prefix_also_reaches_its_own_field(self) -> None:
        """``AppContext`` has the field; overloading run_id alone left it empty."""
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(  # type: ignore[arg-type]
            _FakeApp, {}, execution_id_prefix="run-7"
        )
        assert backend.calls[0]["context"].execution_id_prefix == "run-7"


class TestPreconditions:
    def test_env_set_after_import_is_a_loud_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk import constants

        monkeypatch.setattr(constants, "APPLICATION_NAME", "snapshotted")
        monkeypatch.setenv(fixtures.APPLICATION_NAME_ENV, "live")
        with pytest.raises(_errors.IntegrationEnvOrderingError) as exc:
            fixtures._verify_env_ordering()
        assert exc.value.resource == fixtures.APPLICATION_NAME_ENV

    def test_matching_env_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from application_sdk import constants

        monkeypatch.setattr(constants, "APPLICATION_NAME", "same")
        monkeypatch.setenv(fixtures.APPLICATION_NAME_ENV, "same")
        monkeypatch.setattr(constants, "DEPLOYMENT_NAME", "ci")
        monkeypatch.setenv(fixtures.DEPLOYMENT_NAME_ENV, "ci")
        fixtures._verify_env_ordering()

    def test_unset_env_warns_rather_than_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(fixtures.APPLICATION_NAME_ENV, raising=False)
        monkeypatch.delenv(fixtures.DEPLOYMENT_NAME_ENV, raising=False)
        fixtures._verify_env_ordering()

    def test_unregistered_app_fails_before_the_worker_is_built(self) -> None:
        with pytest.raises(_errors.AppRegistrationMissingError):
            fixtures._verify_registration(_FakeApp)  # type: ignore[arg-type]

    def test_registered_app_passes_and_yields_the_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.app.registry import AppRegistry

        monkeypatch.setattr(AppRegistry, "get_instance", lambda: _FakeRegistry())
        assert fixtures._verify_registration(_FakeApp) == _APP_NAME  # type: ignore[arg-type]

    def test_env_set_after_import_fails_at_import(self) -> None:
        """The check runs on import, not on first fixture use.

        Asserting on ``inspect.getsource`` would pin the call's *text*; the
        behaviour that matters is that a mis-ordered conftest cannot collect at
        all. Only a fresh interpreter can show that, because this one has long
        since imported the module.
        """
        result = _run_python(
            "import os\n"
            "os.environ['ATLAN_APPLICATION_NAME'] = 'set-first'\n"
            "import application_sdk.constants  # snapshots 'set-first'\n"
            "os.environ['ATLAN_APPLICATION_NAME'] = 'set-too-late'\n"
            "import application_sdk.testing.integration.fixtures\n"
        )
        assert result.returncode != 0
        assert "IntegrationEnvOrderingError" in result.stderr
        assert "set-too-late" in result.stderr

    def test_matching_env_imports_cleanly(self) -> None:
        result = _run_python(
            "import os\n"
            "os.environ['ATLAN_APPLICATION_NAME'] = 'in-order'\n"
            "import application_sdk.testing.integration.fixtures\n"
        )
        assert result.returncode == 0, result.stderr


class TestOverrideFixtures:
    def test_app_cls_must_be_overridden(self) -> None:
        with pytest.raises(_errors.AppRegistrationMissingError) as exc:
            _fn(fixtures.integration_app_cls)()
        assert exc.value.resource == "integration_app_cls"

    def test_task_queue_is_the_canonical_derivation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The queue the real deployment uses, not a locally invented one.

        Pinned against ``task_queue_from_env`` itself rather than a literal:
        FND-195 exists because the worker and the served manifest once derived
        this name independently, and a literal here would re-open that gap
        while staying green.
        """
        from application_sdk.common.task_queue import task_queue_from_env

        monkeypatch.setenv(fixtures.APPLICATION_NAME_ENV, _APP_NAME)
        monkeypatch.setenv(fixtures.DEPLOYMENT_NAME_ENV, "ci")
        queue = _fn(fixtures.integration_task_queue)(_FakeApp)
        assert queue == task_queue_from_env() == f"atlan-{_APP_NAME}-ci"

    def test_task_queue_falls_back_to_the_local_dev_convention(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.app.registry import AppRegistry

        monkeypatch.delenv(fixtures.APPLICATION_NAME_ENV, raising=False)
        monkeypatch.delenv(fixtures.DEPLOYMENT_NAME_ENV, raising=False)
        monkeypatch.setattr(AppRegistry, "get_instance", lambda: _FakeRegistry())
        assert _fn(fixtures.integration_task_queue)(_FakeApp) == f"{_APP_NAME}-queue"

    def test_task_queue_fallback_reports_an_unregistered_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_app_name`` is stamped by registration, so its absence is that.

        Reading the attribute bare would raise ``AttributeError`` here — for
        exactly the case the kit exists to report actionably.
        """
        monkeypatch.delenv(fixtures.APPLICATION_NAME_ENV, raising=False)
        monkeypatch.delenv(fixtures.DEPLOYMENT_NAME_ENV, raising=False)

        class _UnregisteredApp:
            pass

        with pytest.raises(_errors.AppRegistrationMissingError):
            _fn(fixtures.integration_task_queue)(_UnregisteredApp)

    def test_source_defaults_to_none(self) -> None:
        assert _fn(fixtures.integration_source)() is None

    def test_secrets_default_empty(self) -> None:
        assert _fn(fixtures.integration_secrets)(None) == {}

    def test_options_default(self) -> None:
        assert _fn(fixtures.integration_options)() == KitOptions()

    def test_star_import_exposes_every_fixture_and_override(self) -> None:
        for name in (
            "integration_app_cls",
            "integration_task_queue",
            "integration_source",
            "integration_secrets",
            "integration_options",
            "store_root",
            "infrastructure",
            "embedded_temporal",
            "temporal_client",
            "worker",
            "executor",
        ):
            assert name in fixtures.__all__


class TestFixtureGraph:
    def test_worker_depends_on_infrastructure(self) -> None:
        assert "infrastructure" in _params(fixtures.worker)

    def test_executor_depends_on_worker(self) -> None:
        assert "worker" in _params(fixtures.executor)

    def test_infrastructure_sees_the_source_and_secrets(self) -> None:
        params = _params(fixtures.infrastructure)
        assert "integration_source" in params
        assert "integration_secrets" in params
        assert "store_root" in params


class TestArtifactPreservation:
    @pytest.fixture(autouse=True)
    def _restore_cleanup_env(self) -> Iterator[None]:
        """Restore the interceptor var around every test in this class.

        ``monkeypatch.delenv(..., raising=False)`` records nothing when the
        variable is already absent, so a test that then lets the code under
        test *set* it leaks that value to the end of the session — and this
        particular variable decides whether ``App.on_complete()`` deletes a
        run's artifacts, so the leak lands on unrelated tests as a missing
        output file.
        """
        sentinel = object()
        before: object = os.environ.get(CLEANUP_INTERCEPTOR_ENV, sentinel)
        try:
            yield
        finally:
            if before is sentinel:
                os.environ.pop(CLEANUP_INTERCEPTOR_ENV, None)
            else:
                os.environ[CLEANUP_INTERCEPTOR_ENV] = str(before)

    def test_defaults_the_interceptor_off_for_the_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLEANUP_INTERCEPTOR_ENV, raising=False)
        with fixtures._artifact_preservation(KitOptions()):
            assert os.environ[CLEANUP_INTERCEPTOR_ENV] == "false"

    def test_the_default_does_not_outlive_the_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole process reads this variable, so the default must not leak.

        ``pytest tests/`` runs integration and unit tests together; a
        cleanup-asserting unit test scheduled after the session-scoped worker
        would otherwise observe cleanup disabled and pass for the wrong reason
        (BLDX-1283).
        """
        monkeypatch.delenv(CLEANUP_INTERCEPTOR_ENV, raising=False)
        with fixtures._artifact_preservation(KitOptions()):
            pass
        assert CLEANUP_INTERCEPTOR_ENV not in os.environ

    def test_an_explicit_value_survives_the_block_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLEANUP_INTERCEPTOR_ENV, "true")
        with fixtures._artifact_preservation(KitOptions()):
            assert os.environ[CLEANUP_INTERCEPTOR_ENV] == "true"
        assert os.environ[CLEANUP_INTERCEPTOR_ENV] == "true"

    def test_explicit_setting_wins_and_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings = _capture_warnings(monkeypatch)
        monkeypatch.setenv(CLEANUP_INTERCEPTOR_ENV, "true")
        with fixtures._artifact_preservation(KitOptions()):
            pass
        assert len(warnings) == 1
        assert CLEANUP_INTERCEPTOR_ENV in warnings[0]

    def test_explicit_false_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warnings = _capture_warnings(monkeypatch)
        monkeypatch.setenv(CLEANUP_INTERCEPTOR_ENV, "false")
        with fixtures._artifact_preservation(KitOptions()):
            pass
        assert warnings == []

    @pytest.mark.parametrize("value", ["off", "disabled", "  false  ", "TRUE"])
    def test_every_value_the_sdk_reads_as_on_is_warned_about(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """The predicate must be the SDK's denylist, not a truthiness allowlist.

        ``App.on_complete`` enables cleanup for everything outside
        ``("0", "false", "no")`` and does not strip. An allowlist here
        (``{"1", "true", "yes", "on"}``) stayed silent for each of these while
        the SDK deleted the run's artifacts — the exact "output file missing"
        failure the warning exists to name. ``"  false  "`` is included
        deliberately: the SDK does not strip, so it means cleanup *on*.
        """
        assert fixtures._cleanup_enabled(value), "premise: SDK leaves cleanup on"
        warnings = _capture_warnings(monkeypatch)
        monkeypatch.setenv(CLEANUP_INTERCEPTOR_ENV, value)
        with fixtures._artifact_preservation(KitOptions()):
            pass
        assert len(warnings) == 1
        assert CLEANUP_INTERCEPTOR_ENV in warnings[0]

    @pytest.mark.parametrize("value", ["", "   "])
    def test_an_empty_value_is_absent_not_a_choice(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """``export VAR=`` must not defeat preservation.

        The SDK reads ``""`` as cleanup-on, so honouring it as "the user
        decided" disabled preservation *and* suppressed the warning naming the
        cause — the one combination with no signal at all. Treat it as unset.
        """
        monkeypatch.setenv(CLEANUP_INTERCEPTOR_ENV, value)
        with fixtures._artifact_preservation(KitOptions()):
            assert os.environ[CLEANUP_INTERCEPTOR_ENV] == "false"

    def test_an_empty_value_is_restored_not_deleted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Taking ownership of the value is not taking ownership of the key."""
        monkeypatch.setenv(CLEANUP_INTERCEPTOR_ENV, "")
        with fixtures._artifact_preservation(KitOptions()):
            pass
        assert os.environ[CLEANUP_INTERCEPTOR_ENV] == ""

    def test_the_predicate_matches_the_sdks_own_reader(self) -> None:
        """Pin the mirror, so the two readers cannot drift apart again.

        This is the premise every case above rests on: it reproduces
        ``App.on_complete``'s expression rather than trusting that
        ``_cleanup_enabled`` still matches it.
        """
        for value in [
            "true",
            "1",
            "yes",
            "on",
            "off",
            "",
            "  false  ",
            "disabled",
            "false",
            "0",
            "no",
            "TRUE",
            "False",
        ]:
            sdk_leaves_cleanup_on = value.lower() not in ("0", "false", "no")
            assert fixtures._cleanup_enabled(value) is sdk_leaves_cleanup_on, value

    def test_declining_leaves_the_environment_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLEANUP_INTERCEPTOR_ENV, raising=False)
        with fixtures._artifact_preservation(KitOptions(preserve_artifacts=False)):
            assert CLEANUP_INTERCEPTOR_ENV not in os.environ
        assert CLEANUP_INTERCEPTOR_ENV not in os.environ


class TestInfrastructureFixture:
    def test_mocked_stores_installed_then_torn_down(self, tmp_path: Path) -> None:
        from application_sdk.infrastructure.context import get_infrastructure
        from application_sdk.observability.observability import AtlanObservability
        from application_sdk.testing.mocks import MockSecretStore, MockStateStore

        before = AtlanObservability._deployment_store
        gen = _fn(fixtures.infrastructure)(tmp_path, None, {"k": "v"})
        ctx = next(gen)
        # try/finally, matching the sibling below: without it a failing
        # assertion skips the generator's own finally, and the infrastructure
        # context plus the observability store leak process-wide into whatever
        # runs next — under `pytest tests/` that is unit tests.
        try:
            assert isinstance(ctx.state_store, MockStateStore)
            assert isinstance(ctx.secret_store, MockSecretStore)
            assert ctx.storage is not None
            assert get_infrastructure() is ctx
            assert AtlanObservability._deployment_store is not before
        finally:
            with pytest.raises(StopIteration):
                next(gen)
        assert get_infrastructure() is None
        assert AtlanObservability._deployment_store is before

    def test_the_swap_is_visible_through_a_subclass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolve through a subclass first — the way production reaches it.

        Both consumers call ``self._get_deployment_store()``, so ``cls`` is the
        concrete adapter. While the getter cached with ``cls._deployment_store =
        ...`` that bound a *subclass* attribute shadowing the base ClassVar, and
        this fixture's swap of the base became a silent no-op: an adopting
        suite's observability wrote to the real deployment store.

        The cache must be **cold** for this to bite — a warm base short-circuits
        the assignment and no subclass attribute is ever created, which is why
        asserting on the base attribute alone (the test above) cannot catch it.
        """
        from application_sdk.observability.observability import AtlanObservability
        from application_sdk.storage import binding
        from application_sdk.storage.ops import BoundStore

        class _FakeAdapter(AtlanObservability):  # type: ignore[type-arg]
            pass

        resolved = object()
        monkeypatch.setattr(
            binding,
            "create_store_from_binding_with_put_attrs",
            lambda *a, **k: (resolved, None),
        )
        monkeypatch.setattr(AtlanObservability, "_deployment_store", None)

        # Cold resolve through the subclass: this is the write that used to land
        # on ``_FakeAdapter`` instead of the base.
        assert _FakeAdapter._get_deployment_store().store is resolved
        assert "_deployment_store" not in _FakeAdapter.__dict__

        gen = _fn(fixtures.infrastructure)(tmp_path, None, {})
        next(gen)
        try:
            swapped = _FakeAdapter._get_deployment_store()
            assert isinstance(swapped, BoundStore)
            assert swapped.store is not resolved
        finally:
            with pytest.raises(StopIteration):
                next(gen)
        assert _FakeAdapter._get_deployment_store().store is resolved

    async def test_secrets_are_seeded(self, tmp_path: Path) -> None:
        gen = _fn(fixtures.infrastructure)(tmp_path, None, {"creds": '{"a": 1}'})
        ctx = next(gen)
        try:
            assert await ctx.secret_store.get("creds") == '{"a": 1}'
        finally:
            with pytest.raises(StopIteration):
                next(gen)

    def test_kit_infrastructure_accepts_a_replacement_store(
        self, tmp_path: Path
    ) -> None:
        """The override path the guide documents, since wrapping is impossible.

        A star-imported fixture cannot be wrapped — ``def infrastructure(
        infrastructure)`` is a recursive-dependency error — so overrides replace,
        and replacing used to mean copying the body including the observability
        swap. This is what makes that unnecessary.
        """
        from application_sdk.storage import create_memory_store

        replacement = create_memory_store()
        with fixtures.kit_infrastructure(
            tmp_path, {"k": "v"}, storage=replacement
        ) as ctx:
            assert ctx.storage is replacement

    def test_kit_infrastructure_defaults_storage_to_the_store_root(
        self, tmp_path: Path
    ) -> None:
        with fixtures.kit_infrastructure(tmp_path, {}) as ctx:
            assert ctx.storage is not None


class TestOptionsReachTheirConsumers:
    """Every KitOptions knob must be pinned to the call site it feeds.

    ``data_converter``, ``enable_prometheus`` and ``preserve_artifacts`` were
    covered; ``log_level`` and ``store_root_prefix`` were not, and both could be
    hardcoded with the whole suite green — which also left ``embedded_temporal``
    with no unit test at all.
    """

    async def test_log_level_reaches_the_embedded_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk import dev

        calls: list[dict[str, Any]] = []

        @contextlib.asynccontextmanager
        async def fake_runtime(**kwargs: Any) -> Any:
            calls.append(kwargs)
            yield "runtime"

        monkeypatch.setattr(dev, "embedded_runtime", fake_runtime)
        gen = _fn(fixtures.embedded_temporal)(KitOptions(log_level="debug"))
        assert await gen.__anext__() == "runtime"
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        assert calls[0]["log_level"] == "debug"

    def test_store_root_prefix_reaches_the_factory(self, tmp_path: Path) -> None:
        prefixes: list[str] = []

        class _FakeFactory:
            def mktemp(self, prefix: str) -> Path:
                prefixes.append(prefix)
                return tmp_path

            def getbasetemp(self) -> Path:  # pragma: no cover - unused
                return tmp_path

        root = _fn(fixtures.store_root)(
            _FakeFactory(), KitOptions(store_root_prefix="custom-prefix")
        )
        assert root == tmp_path
        assert prefixes == ["custom-prefix"]


class TestClientFixture:
    async def test_prometheus_off_converter_on_namespace_threaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _capture_client(monkeypatch)
        await _fn(fixtures.temporal_client)(_FakeRuntime(), _FakeApp, KitOptions())
        assert calls[0]["enable_prometheus"] is False
        assert calls[0]["data_converter"] == "converter"
        assert calls[0]["namespace"] == "test-ns"
        assert calls[0]["host"] == "127.0.0.1:7233"

    async def test_prometheus_can_be_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _capture_client(monkeypatch)
        await _fn(fixtures.temporal_client)(
            _FakeRuntime(), _FakeApp, KitOptions(enable_prometheus=True)
        )
        assert calls[0]["enable_prometheus"] is True

    async def test_converter_can_be_declined(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _capture_client(monkeypatch)
        await _fn(fixtures.temporal_client)(
            _FakeRuntime(), _FakeApp, KitOptions(data_converter=False)
        )
        assert calls[0]["data_converter"] is None


class TestLazyPackage:
    """The star-import must not pay for the framework it replaces.

    Python imports a parent package before its submodule, so an adopting
    conftest's ``from ...fixtures import *`` runs
    ``testing/integration/__init__.py`` first. Eagerly re-exporting there made
    that import pull in ``BaseIntegrationTest`` and pyatlan_v9 — ~2s warm, per
    process, under ``pytest -n auto --dist=loadfile``.
    """

    def test_fixtures_does_not_drag_in_the_scenario_framework(self) -> None:
        result = _run_python(
            "import sys\n"
            "import application_sdk.testing.integration.fixtures\n"
            "leaked = [m for m in "
            "('application_sdk.testing.integration.runner',\n"
            " 'application_sdk.testing.integration.client',\n"
            " 'application_sdk.testing.integration.validation',\n"
            " 'application_sdk.validation') if m in sys.modules]\n"
            "assert not leaked, leaked\n"
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_every_reexport_still_resolves(self) -> None:
        from application_sdk.testing import integration

        for name in integration.__all__:
            assert getattr(integration, name) is not None
        assert set(integration.__all__) <= set(dir(integration))

    def test_unknown_attribute_still_raises(self) -> None:
        from application_sdk.testing import integration

        with pytest.raises(AttributeError):
            integration.definitely_not_exported  # type: ignore[attr-defined]

    def test_lazy_map_matches_the_public_api(self) -> None:
        from application_sdk.testing import integration

        assert set(integration._LAZY_EXPORTS) == set(integration.__all__)

    def test_submodules_are_still_reachable_as_attributes(self) -> None:
        """``integration.models.Scenario`` — an access the eager form gave free.

        Importing a submodule binds it on its package as a side effect, so the
        pre-lazy ``from .models import ...`` made this work. ``__getattr__``
        resolving names does not, which turned the access into an
        ``AttributeError`` whose presence depended on whether something else had
        already touched a name from that submodule — order-dependent, so a test
        touching ``Scenario`` first would not have noticed.
        """
        script = """
from application_sdk.testing import integration

assert integration.models.Scenario is not None
assert integration.assertions.equals is not None
assert "models" in dir(integration)
assert "assertions" in dir(integration)
print("OK")
"""
        result = _run_python(script)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_type_checking_block_matches_the_lazy_map(self) -> None:
        """The third parallel list, read the way its only two readers read it.

        ``_LAZY_EXPORTS`` and ``__all__`` are pinned to each other above. The
        ``if TYPE_CHECKING:`` block is a third copy, and it is the one griffe
        and pyright resolve names from — neither follows ``__getattr__``. A
        name added to the other two and omitted here leaves this repo entirely
        green: runtime resolves it lazily, pyright only errors on names absent
        from the *target* submodule, and Capability Manifest Drift compares a
        regenerated manifest against the committed one, so both are missing the
        symbol and agree. Downstream, it vanishes from
        ``docs/agents/sdk-capabilities.md`` and types as unknown at every
        consumer call site — the two failures the block's own comment says it
        exists to prevent.

        Compared as a dict, not a set, so a name pointed at the wrong module is
        caught too: ``__getattr__`` would import one module and static readers
        the other.
        """
        from application_sdk.testing import integration

        assert integration.__file__ is not None
        tree = ast.parse(Path(integration.__file__).read_text())
        declared: dict[str, str] = {}
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING"
            ):
                continue
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.ImportFrom):
                    # Spelled exactly as _LAZY_EXPORTS spells it: a relative
                    # import keeps its leading dots, an absolute one its path.
                    module = "." * stmt.level + (stmt.module or "")
                    for alias in stmt.names:
                        declared[alias.asname or alias.name] = module

        assert declared, "no TYPE_CHECKING import block found"
        assert declared == integration._LAZY_EXPORTS


def _run_python(script: str) -> subprocess.CompletedProcess[str]:
    """Run *script* in a fresh interpreter, for import-time behaviour.

    Import-time effects — the env-ordering check, what a module pulls into
    ``sys.modules`` — cannot be observed in this process, which imported
    everything during collection.
    """
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[4],
        timeout=120,
    )


def _capture_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        fixtures.logger,
        "warning",
        lambda msg, *args, **kwargs: seen.append(msg % args if args else msg),
    )
    return seen


def _capture_client(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from application_sdk import execution

    calls: list[dict[str, Any]] = []

    async def fake_client(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "client"

    monkeypatch.setattr(execution, "create_temporal_client", fake_client)
    monkeypatch.setattr(
        execution, "create_data_converter_for_app", lambda app_cls: "converter"
    )
    return calls


@dataclass
class _FakeRuntime:
    host: str = "127.0.0.1:7233"
    namespace: str = "test-ns"


@pytest.fixture(scope="session")
def shipped_fake_source(
    http_fake_source_factory: HttpFakeSourceFactory,
) -> HttpFakeSource:
    """The connector-side half of the shipped pattern: routes, nothing else."""
    source = http_fake_source_factory(
        name="shipped-fake-source", connection_timeout=0.2
    )
    source.route(r"/api/objects", lambda _r: {"items": [{"id": "obj-01"}]})
    return source


@pytest.mark.allow_hosts(["127.0.0.1"])
class TestFakeSourceFixtures:
    """The session factory plus its autouse reset, used as the kit documents."""

    def test_the_session_factory_yields_a_started_server(
        self,
        shipped_fake_source: HttpFakeSource,
        http_fake_source_factory: HttpFakeSourceFactory,
    ) -> None:
        with urllib.request.urlopen(  # noqa: S310 - loopback fake on 127.0.0.1
            f"{shipped_fake_source.base_url}/api/objects"
        ) as response:
            assert response.status == 200
        assert len(shipped_fake_source.requests) == 1
        assert shipped_fake_source.hits(r"/api/objects") == 1
        assert list(http_fake_source_factory.sources) == [shipped_fake_source]

    def test_per_test_recordings_do_not_leak_between_tests(
        self, shipped_fake_source: HttpFakeSource
    ) -> None:
        """Whichever order these two run in, each starts with a clean slate.

        The assertion is deliberately not on ``unused_routes``: that reads a
        lifetime counter the reset does not clear, so it is a per-suite answer
        and cannot be asserted from inside one test.
        """
        assert list(shipped_fake_source.requests) == []
        assert shipped_fake_source.hits(r"/api/objects") == 0

    def test_the_reset_is_autouse_and_takes_no_arguments(self) -> None:
        assert _params(fixtures.reset_http_fake_sources) == []
        assert fixtures.reset_http_fake_sources._fixture_function_marker.autouse

    def test_the_reset_is_a_noop_before_any_factory_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A suite whose source is a container must not pay for this fixture."""
        monkeypatch.setattr(fixtures, "_ACTIVE_FAKE_SOURCE_FACTORY", None)
        assert _fn(fixtures.reset_http_fake_sources)() is None

    def test_both_names_ship_in_the_kit_star_import(self) -> None:
        """The factory and its reset are one unit: neither can be picked up alone."""
        assert "http_fake_source_factory" in fixtures.__all__
        assert "reset_http_fake_sources" in fixtures.__all__
