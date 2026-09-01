"""The shared dispatch-ID resolver both dispatch paths call.

The invariant lives in one function so it is testable in one place: the ID
Temporal runs under and the ``Input.workflow_id`` the workflow reads are the
same value, whoever supplied it.
"""

from __future__ import annotations

import re
from unittest import mock

from application_sdk.common.dispatch import resolve_dispatch_workflow_id


class _WfInput:
    workflow_id: str = ""

    def config_hash(self) -> str:
        return "cfghash"


class TestResolveDispatchWorkflowId:
    def test_explicit_id_wins_and_is_stamped(self) -> None:
        inp = _WfInput()

        resolved = resolve_dispatch_workflow_id(
            inp, "my-app", explicit_workflow_id="caller-wf-id"
        )

        assert resolved == "caller-wf-id"
        assert inp.workflow_id == "caller-wf-id"

    def test_input_supplied_id_wins_when_no_explicit(self) -> None:
        inp = _WfInput()
        inp.workflow_id = "input-wf-id"

        assert resolve_dispatch_workflow_id(inp, "my-app") == "input-wf-id"

    def test_mints_the_handler_shape_and_stamps(self) -> None:
        inp = _WfInput()

        resolved = resolve_dispatch_workflow_id(inp, "my-app")

        assert re.fullmatch(r"my-app-cfghash-[0-9a-f]{8}", resolved)
        assert inp.workflow_id == resolved

    def test_two_mints_differ(self) -> None:
        first = resolve_dispatch_workflow_id(_WfInput(), "my-app")
        second = resolve_dispatch_workflow_id(_WfInput(), "my-app")

        assert first != second

    def test_input_without_config_hash_still_mints(self) -> None:
        class _Bare:
            workflow_id: str = ""

        inp = _Bare()

        resolved = resolve_dispatch_workflow_id(inp, "my-app")

        assert re.fullmatch(r"my-app-[0-9a-f]{8}", resolved)
        assert inp.workflow_id == resolved

    def test_rejected_stamp_warns_instead_of_passing_silently(self) -> None:
        """No memo fallback exists here: a swallowed stamp reproduces the
        exact workflow_id divergence the resolver exists to rule out, so it
        must leave a trace a test suite can find."""

        class _Frozen:
            __slots__ = ()

        with mock.patch(
            "application_sdk.observability.logger_adaptor.get_logger"
        ) as get_logger:
            resolved = resolve_dispatch_workflow_id(_Frozen(), "my-app")

        assert re.fullmatch(r"my-app-[0-9a-f]{8}", resolved)
        warning = get_logger.return_value.warning
        warning.assert_called_once()
        assert "rejected the workflow_id stamp" in warning.call_args.args[0]
