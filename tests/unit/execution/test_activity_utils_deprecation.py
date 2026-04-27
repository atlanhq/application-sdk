"""Tests for the deprecation warnings on the legacy activity_utils helpers.

The public ``get_workflow_id`` / ``get_workflow_run_id`` / ``build_output_path``
helpers in ``application_sdk.execution._temporal.activity_utils`` are
deprecated. Apps should read ``input.workflow_id`` from the typed ``Input``
contract instead.

These tests verify that:
1. Each deprecated helper emits a ``DeprecationWarning`` at call time.
2. The warning message names ``input.workflow_id`` so apps and AI agents
   can find the right migration target.
3. The private ``_``-prefixed helpers used by the SDK itself do *not*
   emit warnings.
"""

from unittest import mock

import pytest

from application_sdk.execution._temporal import activity_utils


@pytest.fixture
def fake_activity_info() -> mock.MagicMock:
    info = mock.MagicMock()
    info.workflow_id = "wf-test-123"
    info.workflow_run_id = "run-test-456"
    return info


def test_get_workflow_id_emits_deprecation_warning(
    fake_activity_info: mock.MagicMock,
) -> None:
    with mock.patch.object(
        activity_utils.activity, "info", return_value=fake_activity_info
    ):
        with pytest.warns(DeprecationWarning, match="input.workflow_id"):
            result = activity_utils.get_workflow_id()
    assert result == "wf-test-123"


def test_get_workflow_run_id_emits_deprecation_warning(
    fake_activity_info: mock.MagicMock,
) -> None:
    with mock.patch.object(
        activity_utils.activity, "info", return_value=fake_activity_info
    ):
        with pytest.warns(DeprecationWarning, match="input.workflow_id"):
            result = activity_utils.get_workflow_run_id()
    assert result == "run-test-456"


def test_build_output_path_emits_deprecation_warning(
    fake_activity_info: mock.MagicMock,
) -> None:
    with mock.patch.object(
        activity_utils.activity, "info", return_value=fake_activity_info
    ):
        with pytest.warns(DeprecationWarning, match="input.workflow_id"):
            result = activity_utils.build_output_path()
    assert "wf-test-123" in result
    assert "run-test-456" in result


def test_private_helpers_do_not_emit_warnings(
    fake_activity_info: mock.MagicMock,
) -> None:
    """The ``_``-prefixed SDK-internal helpers must not warn."""
    import warnings

    with mock.patch.object(
        activity_utils.activity, "info", return_value=fake_activity_info
    ):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert activity_utils._get_workflow_id() == "wf-test-123"
            assert activity_utils._get_workflow_run_id() == "run-test-456"
            path = activity_utils._build_output_path()
            assert "wf-test-123" in path
            assert "run-test-456" in path


def test_get_object_store_prefix_does_not_warn() -> None:
    """``get_object_store_prefix`` is a path-shape utility, not deprecated."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert (
            activity_utils.get_object_store_prefix("datasets/sales/")
            == "datasets/sales"
        )
