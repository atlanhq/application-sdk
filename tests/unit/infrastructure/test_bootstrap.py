"""Unit tests for the infrastructure bootstrap (``application_sdk.infrastructure._bootstrap``).

Moved from ``tests/unit/test_main.py`` with the code (FND-4689)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from application_sdk.infrastructure._bootstrap import (
    _create_infrastructure,
    _log_dapr_components,
    _parse_all_component_yamls,
)
from application_sdk.main_errors import (
    DaprNotDetectedError,
)


class TestLogDaprComponents:
    """Tests for _log_dapr_components()."""

    async def test_returns_registered_component_names(self, tmp_path: Path) -> None:
        """Returns the set of registered component names on success."""
        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {
            "registeredComponents": [
                {"name": "atlan-statestore", "type": "bindings.redis", "version": "v1"},
                {
                    "name": "atlan-secretstore",
                    "type": "bindings.redis",
                    "version": "v1",
                },
            ]
        }

        result = await _log_dapr_components(dapr_client, tmp_path)

        assert result == {"atlan-statestore", "atlan-secretstore"}

    async def test_returns_empty_set_on_metadata_failure(self, tmp_path: Path) -> None:
        """Returns empty set when Dapr metadata query fails."""
        dapr_client = AsyncMock()
        dapr_client.get_metadata.side_effect = Exception("Dapr not reachable")

        result = await _log_dapr_components(dapr_client, tmp_path)

        assert result == set()

    async def test_return_type_is_set_of_str(self, tmp_path: Path) -> None:
        """Return type is set[str] — every element is a string."""
        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {
            "registeredComponents": [
                {"name": "my-component", "type": "bindings.redis", "version": "v1"},
            ]
        }

        result = await _log_dapr_components(dapr_client, tmp_path)

        assert isinstance(result, set)
        assert all(isinstance(name, str) for name in result)


class TestLogDaprComponentsBindingOutcomes:
    """The expected-binding loop classifies each binding against ATLAN_ENABLE_*.

    Source of truth is ``atlan.yaml``'s ``deploy.dapr.{statestore,objectstore,
    secretstore,eventstore}`` block, which the chart translates into env vars
    on the pod via ``seed-env.sh``.
    """

    @staticmethod
    def _rendered(call_args_list: list) -> list[str]:
        """Render mock log calls with %-formatting applied so substring assertions work."""
        out: list[str] = []
        for c in call_args_list:
            args = c.args
            if not args:
                continue
            fmt = args[0]
            rest = args[1:]
            try:
                out.append(fmt % rest if rest else fmt)
            except (TypeError, ValueError):
                out.append(fmt)
        return out

    @staticmethod
    def _clear_enable_env(monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "ATLAN_ENABLE_STATESTORE",
            "ATLAN_ENABLE_SECRETSTORE",
            "ATLAN_ENABLE_OBJECTSTORE",
            "ATLAN_ENABLE_EVENTSTORE",
        ):
            monkeypatch.delenv(var, raising=False)

    async def test_missing_with_declared_false_logs_info_not_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """statestore=false in atlan.yaml → missing component is INFO, never warning."""
        self._clear_enable_env(monkeypatch)
        monkeypatch.setenv("ATLAN_ENABLE_STATESTORE", "false")
        # Other expected components present so they don't pollute the test
        monkeypatch.setenv("ATLAN_ENABLE_SECRETSTORE", "true")
        monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", "true")
        monkeypatch.setenv("ATLAN_ENABLE_EVENTSTORE", "false")

        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {
            "components": [
                {"name": "secretstore", "type": "bindings.redis", "version": "v1"},
                {"name": "objectstore", "type": "bindings.gcp.bucket", "version": "v1"},
            ]
        }
        with patch("application_sdk.infrastructure._bootstrap.logger") as mock_log:
            await _log_dapr_components(dapr_client, tmp_path)

        warning_msgs = self._rendered(mock_log.warning.call_args_list)
        info_msgs = self._rendered(mock_log.info.call_args_list)
        assert not any("statestore" in m for m in warning_msgs), (
            f"statestore disabled-by-config must not warn; got: {warning_msgs}"
        )
        assert any(
            "statestore" in m and "disabled by config" in m for m in info_msgs
        ), f"expected disabled-by-config INFO for statestore; got: {info_msgs}"

    @pytest.mark.parametrize("flag", [None, "false"], ids=["flag_unset", "flag_false"])
    async def test_missing_objectstore_logs_error_not_expected_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str | None
    ) -> None:
        """The deployment object store is required: missing is ERROR, never 'expected'."""
        self._clear_enable_env(monkeypatch)
        if flag is not None:
            monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", flag)

        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {"components": []}
        with (
            patch(
                "application_sdk.constants.DEPLOYMENT_OBJECT_STORE_NAME", "objectstore"
            ),
            patch("application_sdk.infrastructure._bootstrap.logger") as mock_log,
        ):
            await _log_dapr_components(dapr_client, tmp_path)

        error_msgs = self._rendered(mock_log.error.call_args_list)
        quieter_msgs = self._rendered(
            mock_log.info.call_args_list + mock_log.warning.call_args_list
        )
        assert any(
            "objectstore" in m and "deploy.dapr.objectstore" in m for m in error_msgs
        ), f"expected an ERROR naming deploy.dapr.objectstore; got: {error_msgs}"
        assert not any("role=object_store" in m for m in quieter_msgs), (
            f"missing object store must not be INFO/WARNING; got: {quieter_msgs}"
        )

    async def test_missing_with_declared_true_logs_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """statestore=true but absent in sidecar → WARNING (genuine config bug)."""
        self._clear_enable_env(monkeypatch)
        monkeypatch.setenv("ATLAN_ENABLE_STATESTORE", "true")
        monkeypatch.setenv("ATLAN_ENABLE_SECRETSTORE", "false")
        monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", "false")
        monkeypatch.setenv("ATLAN_ENABLE_EVENTSTORE", "false")

        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {"components": []}
        with patch("application_sdk.infrastructure._bootstrap.logger") as mock_log:
            await _log_dapr_components(dapr_client, tmp_path)

        warning_msgs = self._rendered(mock_log.warning.call_args_list)
        assert any(
            "statestore" in m and "declared enabled" in m for m in warning_msgs
        ), f"expected declared-but-missing WARNING; got: {warning_msgs}"

    async def test_missing_with_unset_env_logs_info_with_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env var unset → INFO with explicit guidance to set deploy.dapr.* in atlan.yaml."""
        self._clear_enable_env(monkeypatch)

        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {"components": []}
        with patch("application_sdk.infrastructure._bootstrap.logger") as mock_log:
            await _log_dapr_components(dapr_client, tmp_path)

        info_msgs = self._rendered(mock_log.info.call_args_list)
        assert any(
            "statestore" in m
            and "ATLAN_ENABLE_STATESTORE" in m
            and "deploy.dapr.statestore" in m
            for m in info_msgs
        ), (
            "expected unset-env INFO naming the env var and atlan.yaml key; "
            f"got: {info_msgs}"
        )

    async def test_registered_with_declared_false_logs_drift_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Component present but declared disabled → INFO drift signal, never warning."""
        self._clear_enable_env(monkeypatch)
        monkeypatch.setenv("ATLAN_ENABLE_STATESTORE", "false")
        monkeypatch.setenv("ATLAN_ENABLE_SECRETSTORE", "false")
        monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", "false")
        monkeypatch.setenv("ATLAN_ENABLE_EVENTSTORE", "false")

        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {
            "components": [
                {"name": "statestore", "type": "state.redis", "version": "v1"},
            ]
        }
        with patch("application_sdk.infrastructure._bootstrap.logger") as mock_log:
            await _log_dapr_components(dapr_client, tmp_path)

        warning_msgs = self._rendered(mock_log.warning.call_args_list)
        info_msgs = self._rendered(mock_log.info.call_args_list)
        assert not any("statestore" in m and "drift" in m for m in warning_msgs), (
            f"drift case must be INFO not WARNING; got: {warning_msgs}"
        )
        assert any("statestore" in m and "drift" in m for m in info_msgs), (
            f"expected drift INFO for registered+declared-false; got: {info_msgs}"
        )

    async def test_registered_with_declared_true_logs_accepted_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path: declared enabled and registered → INFO accepted, no warning."""
        self._clear_enable_env(monkeypatch)
        monkeypatch.setenv("ATLAN_ENABLE_STATESTORE", "true")
        monkeypatch.setenv("ATLAN_ENABLE_SECRETSTORE", "false")
        monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", "false")
        monkeypatch.setenv("ATLAN_ENABLE_EVENTSTORE", "false")

        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {
            "components": [
                {"name": "statestore", "type": "state.redis", "version": "v1"},
            ]
        }
        with patch("application_sdk.infrastructure._bootstrap.logger") as mock_log:
            await _log_dapr_components(dapr_client, tmp_path)

        warning_msgs = self._rendered(mock_log.warning.call_args_list)
        info_msgs = self._rendered(mock_log.info.call_args_list)
        assert not any("statestore" in m for m in warning_msgs), (
            f"happy path must not warn; got: {warning_msgs}"
        )
        assert any("statestore" in m and "accepted" in m for m in info_msgs), (
            f"expected accepted INFO; got: {info_msgs}"
        )

    async def test_every_expected_binding_emits_at_least_one_log_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract: never silently proceed — each of the 4 bindings produces a log line."""
        self._clear_enable_env(monkeypatch)
        # Mix of states across the four expected bindings
        monkeypatch.setenv("ATLAN_ENABLE_STATESTORE", "false")  # disabled
        monkeypatch.setenv("ATLAN_ENABLE_SECRETSTORE", "true")  # enabled+missing
        monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", "true")  # enabled+present
        # ATLAN_ENABLE_EVENTSTORE deliberately unset

        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {
            "components": [
                {"name": "objectstore", "type": "bindings.gcp.bucket", "version": "v1"},
                {"name": "eventstore", "type": "bindings.kafka", "version": "v1"},
            ]
        }
        with patch("application_sdk.infrastructure._bootstrap.logger") as mock_log:
            await _log_dapr_components(dapr_client, tmp_path)

        all_msgs = self._rendered(
            mock_log.warning.call_args_list + mock_log.info.call_args_list
        )
        for binding in ("statestore", "secretstore", "objectstore", "eventstore"):
            matching = [m for m in all_msgs if binding in m and "role=" in m]
            assert matching, (
                f"binding {binding} produced no role-tagged log line; "
                f"all messages: {all_msgs}"
            )

        # (registered, declared=None): eventstore is in sidecar but ATLAN_ENABLE_EVENTSTORE
        # is unset — the accepted INFO message must render the env value as <unset>
        info_msgs = self._rendered(mock_log.info.call_args_list)
        eventstore_accepted = [
            m for m in info_msgs if "eventstore" in m and "accepted" in m
        ]
        assert eventstore_accepted, (
            f"expected eventstore accepted-INFO for registered+unset; got: {all_msgs}"
        )
        assert any("<unset>" in m for m in eventstore_accepted), (
            f"expected <unset> in eventstore accepted message; got: {eventstore_accepted}"
        )


class TestCreateInfrastructureEventBinding:
    """Tests for _create_infrastructure() conditional event_binding."""

    _DAPR_CLIENT_MOD = "application_sdk.infrastructure._dapr.client"
    _STORAGE_MOD = "application_sdk.storage"

    def _make_dapr_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        # _create_infrastructure awaits the sidecar before anything else; with a
        # non-local ATLAN_DEPLOYMENT_NAME the real wait polls localhost:3500.
        monkeypatch.setattr(
            "application_sdk.infrastructure._dapr.http.wait_for_dapr_sidecar",
            AsyncMock(),
        )

    async def test_event_binding_created_when_component_registered(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DaprBinding is created when the eventstore component is in the registered set."""
        from application_sdk.constants import EVENT_STORE_NAME

        self._make_dapr_env(monkeypatch)

        with (
            patch(
                "application_sdk.infrastructure._bootstrap._log_dapr_components",
                new_callable=AsyncMock,
                return_value={EVENT_STORE_NAME},
            ),
            patch(f"{self._DAPR_CLIENT_MOD}.AsyncDaprClient"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprStateStore"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprSecretStore"),
            patch(
                f"{self._STORAGE_MOD}.create_store_from_binding_with_put_attrs",
                return_value=(MagicMock(), None),
            ),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprBinding") as mock_binding,
        ):
            infra = await _create_infrastructure()

        mock_binding.assert_called_once()
        assert infra.event_binding is not None

    async def test_event_binding_is_none_when_component_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """event_binding is None when eventstore is not registered."""
        self._make_dapr_env(monkeypatch)

        with (
            patch(
                "application_sdk.infrastructure._bootstrap._log_dapr_components",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(f"{self._DAPR_CLIENT_MOD}.AsyncDaprClient"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprStateStore"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprSecretStore"),
            patch(
                f"{self._STORAGE_MOD}.create_store_from_binding_with_put_attrs",
                return_value=(MagicMock(), None),
            ),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprBinding") as mock_binding,
        ):
            infra = await _create_infrastructure()

        mock_binding.assert_not_called()
        assert infra.event_binding is None


class TestCreateInfrastructureUpstreamStore:
    """Tests for _create_infrastructure() upstream_storage selection."""

    _DAPR_CLIENT_MOD = "application_sdk.infrastructure._dapr.client"
    _STORAGE_MOD = "application_sdk.storage"

    def _make_dapr_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        # _create_infrastructure awaits the sidecar before anything else; with a
        # non-local ATLAN_DEPLOYMENT_NAME the real wait polls localhost:3500.
        monkeypatch.setattr(
            "application_sdk.infrastructure._dapr.http.wait_for_dapr_sidecar",
            AsyncMock(),
        )

    async def test_upstream_storage_none_when_optional_helper_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """upstream_storage is None when the optional helper returns (None, None).

        Whether that happens because the component is absent or broken is tested
        in TestCreateStoreFromBindingOptionalWithPutAttrs; here we only verify
        that _create_infrastructure surfaces (None, None) correctly.
        """
        self._make_dapr_env(monkeypatch)
        deployment_store = MagicMock()

        with (
            patch(
                "application_sdk.infrastructure._bootstrap._log_dapr_components",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(f"{self._DAPR_CLIENT_MOD}.AsyncDaprClient"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprStateStore"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprSecretStore"),
            patch(
                "application_sdk.storage.binding._create_store_from_binding_optional_with_put_attrs",
                return_value=(None, None),
            ),
            patch(
                f"{self._STORAGE_MOD}.create_store_from_binding_with_put_attrs",
                return_value=(deployment_store, None),
            ),
        ):
            infra = await _create_infrastructure()

        assert infra.upstream_storage is None
        assert infra.upstream_storage_put_attributes is None

    async def test_upstream_storage_set_when_component_present(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """upstream_storage is a live store in SDR deployments (component present)."""
        self._make_dapr_env(monkeypatch)
        upstream_store = MagicMock()
        deployment_store = MagicMock()

        with (
            patch(
                "application_sdk.infrastructure._bootstrap._log_dapr_components",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(f"{self._DAPR_CLIENT_MOD}.AsyncDaprClient"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprStateStore"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprSecretStore"),
            patch(
                "application_sdk.storage.binding._create_store_from_binding_optional_with_put_attrs",
                return_value=(upstream_store, None),
            ) as mock_optional,
            patch(
                f"{self._STORAGE_MOD}.create_store_from_binding_with_put_attrs",
                return_value=(deployment_store, None),
            ) as mock_binding,
            patch(
                "application_sdk.constants.DEPLOYMENT_OBJECT_STORE_NAME",
                "objectstore",
            ),
            patch(
                "application_sdk.constants.UPSTREAM_OBJECT_STORE_NAME",
                "atlan-objectstore",
            ),
        ):
            infra = await _create_infrastructure()

        assert infra.upstream_storage is upstream_store
        assert infra.storage is deployment_store
        mock_optional.assert_called_once_with(
            "atlan-objectstore", components_dir=ANY, required=ANY, secrets=ANY
        )
        mock_binding.assert_called_once_with(
            "objectstore", components_dir=ANY, secrets=ANY
        )

    async def test_upstream_storage_aliases_deployment_when_names_are_one_component(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One component under both names is one store — one handle, aliased.

        In-cluster charts point UPSTREAM_OBJECT_STORE_NAME and
        DEPLOYMENT_OBJECT_STORE_NAME at the app's single object-store component.
        A *second* store object over the same bucket makes App.upload route by
        identity and copy the bucket onto itself, so startup must not build one;
        ``None`` would be wrong the other way, because every consumer outside
        App.upload reads the handle as "is there an upstream store". Alias it.
        """
        self._make_dapr_env(monkeypatch)
        deployment_store = MagicMock()
        deployment_attrs = {"Storage-Class": "STANDARD_IA"}

        with (
            patch(
                "application_sdk.infrastructure._bootstrap._log_dapr_components",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(
                "application_sdk.infrastructure._bootstrap._fetch_binding_secrets",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_fetch,
            patch(f"{self._DAPR_CLIENT_MOD}.AsyncDaprClient"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprStateStore"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprSecretStore"),
            patch(
                "application_sdk.storage.binding._create_store_from_binding_optional_with_put_attrs",
                return_value=(MagicMock(), None),
            ) as mock_optional,
            patch(
                f"{self._STORAGE_MOD}.create_store_from_binding_with_put_attrs",
                return_value=(deployment_store, deployment_attrs),
            ) as mock_binding,
            patch(
                "application_sdk.constants.DEPLOYMENT_OBJECT_STORE_NAME",
                "my-app-objectstore",
            ),
            patch(
                "application_sdk.constants.UPSTREAM_OBJECT_STORE_NAME",
                "my-app-objectstore",
            ),
        ):
            infra = await _create_infrastructure()

        # Same object, not an equal one: identity is what the dual-write
        # fan-out and _upload_from_store's same-object guard test.
        assert infra.storage is deployment_store
        assert infra.upstream_storage is deployment_store
        assert infra.upstream_storage_put_attributes is deployment_attrs
        assert infra.single_store is True
        mock_optional.assert_not_called()
        # The binding is built once and its secrets fetched once.
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args[1] == "my-app-objectstore"
        mock_binding.assert_called_once_with(
            "my-app-objectstore", components_dir=ANY, secrets=ANY
        )

    async def test_two_distinct_components_are_not_single_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A real SDR wiring keeps two handles and reports single_store False."""
        self._make_dapr_env(monkeypatch)
        deployment_store = MagicMock()
        upstream_store = MagicMock()

        with (
            patch(
                "application_sdk.infrastructure._bootstrap._log_dapr_components",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(
                "application_sdk.infrastructure._bootstrap._fetch_binding_secrets",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(f"{self._DAPR_CLIENT_MOD}.AsyncDaprClient"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprStateStore"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprSecretStore"),
            patch(
                "application_sdk.storage.binding._create_store_from_binding_optional_with_put_attrs",
                return_value=(upstream_store, None),
            ),
            patch(
                f"{self._STORAGE_MOD}.create_store_from_binding_with_put_attrs",
                return_value=(deployment_store, None),
            ),
            patch(
                "application_sdk.constants.DEPLOYMENT_OBJECT_STORE_NAME",
                "objectstore",
            ),
            patch(
                "application_sdk.constants.UPSTREAM_OBJECT_STORE_NAME",
                "atlan-objectstore",
            ),
        ):
            infra = await _create_infrastructure()

        assert infra.upstream_storage is upstream_store
        assert infra.single_store is False

    async def test_upstream_required_true_when_sdr_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """required=True is passed to the upstream factory when ENABLE_ATLAN_UPLOAD=True."""
        self._make_dapr_env(monkeypatch)
        monkeypatch.setattr("application_sdk.constants.ENABLE_ATLAN_UPLOAD", True)
        upstream_store = MagicMock()
        deployment_store = MagicMock()

        with (
            patch(
                "application_sdk.infrastructure._bootstrap._log_dapr_components",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(f"{self._DAPR_CLIENT_MOD}.AsyncDaprClient"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprStateStore"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprSecretStore"),
            patch(
                "application_sdk.storage.binding._create_store_from_binding_optional_with_put_attrs",
                return_value=(upstream_store, None),
            ) as mock_optional,
            patch(
                f"{self._STORAGE_MOD}.create_store_from_binding_with_put_attrs",
                return_value=(deployment_store, None),
            ),
        ):
            await _create_infrastructure()

        _name, _args, kwargs = mock_optional.mock_calls[0]
        assert kwargs.get("required") is True


# --------------------------------------------------------------------------- #
# _create_infrastructure — Dapr-not-set path                                  #
# --------------------------------------------------------------------------- #


class TestCreateInfrastructureNoDapr:
    """_create_infrastructure() raises when Dapr sidecar env var is absent."""

    async def test_raises_runtime_error_when_no_dapr_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without DAPR_HTTP_PORT, _create_infrastructure must raise DaprNotDetectedError."""
        monkeypatch.delenv("DAPR_HTTP_PORT", raising=False)
        with pytest.raises(DaprNotDetectedError) as exc_info:
            await _create_infrastructure()
        assert exc_info.value.code == "DEPENDENCY_UNAVAILABLE_DAPR_SIDECAR"


class TestCreateInfrastructureDeploymentObjectStoreRequired:
    """The deployment object store is required: its absence is a named config error.

    In a real pod the chart sets ``ATLAN_ENABLE_OBJECTSTORE`` only on the
    env-seeder init container, so the app container normally sees it unset.
    The error must therefore fire on the missing component alone, not on the
    flag.
    """

    @staticmethod
    def _write_local_objectstore(components_dir: Path) -> None:
        import yaml

        doc = {
            "apiVersion": "dapr.io/v1alpha1",
            "kind": "Component",
            "metadata": {"name": "objectstore"},
            "spec": {
                "type": "bindings.localstorage",
                "metadata": [
                    {"name": "rootPath", "value": str(components_dir / "data")}
                ],
            },
        }
        (components_dir / "objectstore.yaml").write_text(yaml.dump(doc))

    @staticmethod
    async def _build(monkeypatch: pytest.MonkeyPatch, components_dir: Path) -> Any:
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        monkeypatch.setenv("DAPR_COMPONENTS_PATH", str(components_dir))
        with (
            patch(
                "application_sdk.infrastructure._bootstrap._log_dapr_components",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(
                "application_sdk.infrastructure._dapr.http.wait_for_dapr_sidecar",
                new_callable=AsyncMock,
            ),
            patch("application_sdk.infrastructure._dapr.http.AsyncDaprClient"),
            patch(
                "application_sdk.constants.DEPLOYMENT_OBJECT_STORE_NAME", "objectstore"
            ),
            patch(
                "application_sdk.constants.UPSTREAM_OBJECT_STORE_NAME", "objectstore"
            ),
        ):
            return await _create_infrastructure()

    @pytest.mark.parametrize("flag", [None, "false"], ids=["flag_unset", "flag_false"])
    async def test_missing_component_raises_config_error_naming_the_contract_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str | None
    ) -> None:
        from application_sdk.errors.categories import Audience
        from application_sdk.storage.errors import StorageBindingNotFoundError

        if flag is None:
            monkeypatch.delenv("ATLAN_ENABLE_OBJECTSTORE", raising=False)
        else:
            monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", flag)

        with pytest.raises(StorageBindingNotFoundError) as exc_info:
            await self._build(monkeypatch, tmp_path)

        err = exc_info.value
        assert "deploy.dapr.objectstore" in err.message
        assert type(err).audience is Audience.APP_OWNER
        assert isinstance(err.__cause__, StorageBindingNotFoundError)
        assert err.__cause__ is not err

    async def test_missing_component_with_flag_false_says_the_flag_disables_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.storage.errors import StorageBindingNotFoundError

        monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", "false")
        with pytest.raises(StorageBindingNotFoundError) as exc_info:
            await self._build(monkeypatch, tmp_path)

        assert "ATLAN_ENABLE_OBJECTSTORE=false" in exc_info.value.message

    async def test_missing_component_logs_error_before_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.storage.errors import StorageBindingNotFoundError

        monkeypatch.delenv("ATLAN_ENABLE_OBJECTSTORE", raising=False)
        with (
            patch("application_sdk.infrastructure._bootstrap.logger") as mock_log,
            pytest.raises(StorageBindingNotFoundError),
        ):
            await self._build(monkeypatch, tmp_path)

        rendered = [
            c.args[0] % c.args[1:] if len(c.args) > 1 else c.args[0]
            for c in mock_log.error.call_args_list
        ]
        assert any("deploy.dapr.objectstore" in m for m in rendered), rendered

    @pytest.mark.parametrize("flag", [None, "false"], ids=["flag_unset", "flag_false"])
    async def test_present_component_starts_whatever_the_flag_says(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str | None
    ) -> None:
        if flag is None:
            monkeypatch.delenv("ATLAN_ENABLE_OBJECTSTORE", raising=False)
        else:
            monkeypatch.setenv("ATLAN_ENABLE_OBJECTSTORE", flag)
        self._write_local_objectstore(tmp_path)

        infra = await self._build(monkeypatch, tmp_path)

        assert infra.storage is not None
        assert infra.upstream_storage is infra.storage


# --------------------------------------------------------------------------- #
# _parse_all_component_yamls                                                  #
# --------------------------------------------------------------------------- #


class TestParseAllComponentYamls:
    """Tests for _parse_all_component_yamls()."""

    def test_returns_safe_metadata_only(self, tmp_path: Path) -> None:
        """Allowlisted keys are returned, secret-y keys are filtered out."""
        (tmp_path / "comp.yaml").write_text(
            "apiVersion: dapr.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: my-store\n"
            "spec:\n"
            "  type: state.redis\n"
            "  metadata:\n"
            "    - name: bucket\n"
            "      value: my-bucket\n"
            "    - name: accessKey\n"
            "      value: SECRET\n"
        )
        result = _parse_all_component_yamls(tmp_path)
        assert "my-store" in result
        assert result["my-store"].get("bucket") == "my-bucket"
        # accessKey is NOT in the allowlist — must be excluded
        assert "accessKey" not in result["my-store"]

    def test_skips_non_component_kinds(self, tmp_path: Path) -> None:
        """Non-Component YAMLs (e.g. Configuration) are ignored."""
        (tmp_path / "cfg.yaml").write_text(
            "apiVersion: dapr.io/v1alpha1\n"
            "kind: Configuration\n"
            "metadata:\n"
            "  name: my-cfg\n"
        )
        assert _parse_all_component_yamls(tmp_path) == {}

    def test_skips_components_without_name(self, tmp_path: Path) -> None:
        """Components missing metadata.name are skipped (no KeyError)."""
        (tmp_path / "noname.yaml").write_text(
            "apiVersion: dapr.io/v1alpha1\n"
            "kind: Component\n"
            "metadata: {}\n"
            "spec:\n"
            "  type: state.redis\n"
        )
        assert _parse_all_component_yamls(tmp_path) == {}

    def test_returns_empty_dict_on_parse_error(self, tmp_path: Path) -> None:
        """Malformed YAML must not raise — returns empty dict."""
        (tmp_path / "bad.yaml").write_text("not: yaml: : :\nlol")
        with patch("application_sdk.infrastructure._bootstrap.logger") as mock_log:
            result = _parse_all_component_yamls(tmp_path)
        assert result == {}
        # Failure must be observable as a warning (not silently swallowed).
        assert mock_log.warning.called


# --------------------------------------------------------------------------- #
# _log_dapr_components — branch with safe_meta                                #
# --------------------------------------------------------------------------- #


class TestLogDaprComponentsSafeMetaBranch:
    """Cover the log path that includes safe metadata details."""

    async def test_logs_with_safe_meta_when_yaml_present(self, tmp_path: Path) -> None:
        """When a component has allowlisted metadata in YAML, it appears in the log line."""
        # Write a component YAML with bucket=my-bucket so the safe-meta path triggers
        (tmp_path / "store.yaml").write_text(
            "apiVersion: dapr.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: my-store\n"
            "spec:\n"
            "  type: state.s3\n"
            "  metadata:\n"
            "    - name: bucket\n"
            "      value: my-bucket\n"
        )
        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {
            "components": [{"name": "my-store", "type": "state.s3", "version": "v1"}]
        }
        with patch("application_sdk.infrastructure._bootstrap.logger") as mock_log:
            result = await _log_dapr_components(dapr_client, tmp_path)
        assert result == {"my-store"}
        # The "with safe_meta" code path produces an INFO call whose format
        # string contains a 4th positional placeholder for the detail string.
        info_calls = [c for c in mock_log.info.call_args_list]
        # At least one info should reference the bucket value
        assert any("bucket=my-bucket" in str(c) for c in info_calls)
