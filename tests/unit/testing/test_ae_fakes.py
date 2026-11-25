"""``FakeAE`` keeps the shape of ``AEClient``.

The shared double is only worth having while it is the *whole* client: a public
method the fake lacks is an ``AttributeError`` in whichever suite first reaches
it, and a renamed or re-kinded parameter is a call the fake accepts and the
client rejects. Both are caught here, once, against the real signatures.
"""

from __future__ import annotations

import inspect

import pytest

from application_sdk.testing.harness.automation_engine import AEClient
from tests.unit.testing._ae_fakes import FakeAE, failed, succeeded


def _public_methods(cls: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


_CLIENT = _public_methods(AEClient)
_FAKE_ONLY = {"calls_to", "args"}


def test_the_fake_has_every_public_client_method() -> None:
    fake = set(_public_methods(FakeAE)) - _FAKE_ONLY
    assert fake == set(_CLIENT)


@pytest.mark.parametrize("method", sorted(_CLIENT))
def test_each_method_takes_the_clients_parameters(method: str) -> None:
    """Same names, kinds, order and defaults.

    Defaults included because the fake records every bound argument: a test
    that pins "the caller left the retry budget at its default" reads the
    fake's default, and it must be the client's.
    """
    real = [(p.name, p.kind, p.default) for p in _CLIENT[method].parameters.values()]
    fake = [
        (p.name, p.kind, p.default)
        for p in inspect.signature(getattr(FakeAE, method)).parameters.values()
    ]
    assert fake == real


@pytest.mark.parametrize("method", sorted(_CLIENT))
def test_each_method_is_async_like_the_client(method: str) -> None:
    assert inspect.iscoroutinefunction(getattr(FakeAE, method)) is (
        inspect.iscoroutinefunction(getattr(AEClient, method))
    )


async def test_scripted_polls_are_served_in_order_and_the_last_repeats() -> None:
    ae = FakeAE(failed("extract"), succeeded("extract"))
    assert (await ae.poll_native_status("run-1")).status.value == "Failed"
    assert (await ae.poll_native_status("run-1")).status.value == "Succeeded"
    assert (await ae.get_native_status("run-1")).status.value == "Succeeded"


async def test_both_submit_endpoints_share_one_run_counter_and_report_it() -> None:
    seen: list[str] = []
    ae = FakeAE()
    first = await ae.submit_workflow({}, slug="a", on_run_id=seen.append)
    second = await ae.submit_published_version("b", on_run_id=seen.append)
    assert (first, second) == ("run-1", "run-2")
    assert seen == ["run-1", "run-2"]
    assert ae.args("submit_published_version", "slug") == ["b"]


async def test_a_scripted_error_is_raised_after_the_call_is_recorded() -> None:
    boom = RuntimeError("boom")
    ae = FakeAE(errors={"terminate_run": boom})
    with pytest.raises(RuntimeError):
        await ae.terminate_run("run-9")
    assert ae.args("terminate_run", "run_id") == ["run-9"]
