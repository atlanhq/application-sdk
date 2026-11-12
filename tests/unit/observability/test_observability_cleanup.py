"""The observability retention sweep is gone, and its config is accepted but inert.

The sweep (``_check_and_cleanup`` / ``_cleanup_old_records``) could not delete
anything from the object store: it walked the pod's local partition tree, which
holds only empty directories once each file is uploaded and unlinked, and the
key it deleted lacked the ``artifacts/apps/observability/`` prefix every upload
uses. With cleanup enabled it still cost a state-store round trip per flush.

These tests pin what replaced it:

- a flush never touches the state store and never calls ``storage.delete``;
- ``retention_days`` / ``cleanup_enabled`` are still accepted by the base
  constructor for one release, with a ``DeprecationWarning``, and change nothing;
- the six removed constants still import, from ``constants`` and from the
  adaptor module that re-exported each, with a ``DeprecationWarning``.
"""

from __future__ import annotations

import importlib
import warnings
from datetime import datetime
from typing import Any
from unittest import mock

import pytest

from application_sdk.observability.observability import AtlanObservability


class _StubObservability(AtlanObservability[dict[str, Any]]):
    """Minimal concrete subclass with the object-store sink forced on."""

    def process_record(self, record: dict[str, Any]) -> dict[str, Any]:
        return record

    def export_record(self, record: dict[str, Any]) -> None:  # pragma: no cover
        raise AssertionError("export_record must not run in these tests")

    def _store_sink_enabled(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _reset_instances():
    AtlanObservability._reset_for_testing()
    yield
    AtlanObservability._reset_for_testing()


def _make(tmp_path: Any, **kwargs: Any) -> _StubObservability:
    return _StubObservability(
        batch_size=100,
        flush_interval=60,
        data_dir=str(tmp_path / "observability"),
        file_name="log.parquet",
        **kwargs,
    )


class TestRetentionSweepRemoved:
    def test_sweep_methods_are_gone(self) -> None:
        assert not hasattr(AtlanObservability, "_check_and_cleanup")
        assert not hasattr(AtlanObservability, "_cleanup_old_records")

    async def test_flush_touches_neither_state_store_nor_delete(self, tmp_path) -> None:
        """Even when a caller still asks for cleanup, a flush only uploads."""
        with pytest.warns(DeprecationWarning):
            obs = _make(tmp_path, retention_days=1, cleanup_enabled=True)

        state_store = mock.MagicMock()
        state_store.load = mock.AsyncMock(return_value=None)
        state_store.save = mock.AsyncMock()
        infra = mock.MagicMock(state_store=state_store)

        with (
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=infra,
            ),
            mock.patch(
                "application_sdk.storage.delete", new_callable=mock.AsyncMock
            ) as mock_delete,
            mock.patch.object(
                _StubObservability, "_upload_and_delete", new_callable=mock.AsyncMock
            ) as mock_upload,
            mock.patch(
                "application_sdk.observability.observability.in_temporal_workflow",
                return_value=False,
            ),
        ):
            await obs._flush_records(
                [{"timestamp": datetime(2026, 6, 15, 12).timestamp(), "msg": "x"}]
            )

        mock_upload.assert_awaited_once()
        state_store.load.assert_not_called()
        state_store.save.assert_not_called()
        mock_delete.assert_not_called()


class TestDeprecatedConstructorParams:
    def test_omitting_them_does_not_warn(self, tmp_path) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            _make(tmp_path)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"retention_days": 7},
            {"cleanup_enabled": False},
            {"retention_days": 7, "cleanup_enabled": True},
        ],
    )
    def test_passing_either_warns(self, tmp_path, kwargs: dict[str, Any]) -> None:
        with pytest.warns(DeprecationWarning, match="bucket lifecycle rule"):
            _make(tmp_path, **kwargs)


_REMOVED_CONSTANTS = {
    "LOG_RETENTION_DAYS": 30,
    "LOG_CLEANUP_ENABLED": False,
    "METRICS_RETENTION_DAYS": 30,
    "METRICS_CLEANUP_ENABLED": False,
    "SIZING_RETENTION_DAYS": 90,
    "SIZING_CLEANUP_ENABLED": False,
}


#: Each removed constant, and every module it was importable from before.
_ALIAS_SITES = [
    (module, name)
    for name in sorted(_REMOVED_CONSTANTS)
    for module in (
        "application_sdk.constants",
        {
            "LOG": "application_sdk.observability.logger_adaptor",
            "METRICS": "application_sdk.observability.metrics_adaptor",
            "SIZING": "application_sdk.observability.sizing_sink",
        }[name.split("_")[0]],
    )
]


class TestDeprecatedConstants:
    @pytest.mark.parametrize(("module_name", "name"), _ALIAS_SITES)
    def test_alias_warns_and_returns_former_default(
        self, module_name: str, name: str
    ) -> None:
        module = importlib.import_module(module_name)
        with pytest.warns(DeprecationWarning, match=f"{name} is deprecated"):
            value = getattr(module, name)
        assert value == _REMOVED_CONSTANTS[name]

    def test_unknown_name_still_raises(self) -> None:
        constants = importlib.import_module("application_sdk.constants")
        with pytest.raises(AttributeError):
            constants.NOT_A_REAL_CONSTANT  # noqa: B018
