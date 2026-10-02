"""A task queue derived from a blank or padded app name is silent data loss.

`atlan--prod` and `atlan- redshift -prod` are well-formed strings that name a
queue NO worker polls. The submit returns success and the workflow simply never
runs -- no error, no retry, nothing in the app's logs. That is the worst shape a
bug can take here, so the name is refused at build rather than at first use.
"""

from __future__ import annotations

import pytest
from server_sdk.handler.base import Handler
from server_sdk.manifest import worker_task_queue
from server_sdk.server import build_asgi_app


@pytest.mark.parametrize(
    ("app_name", "expected"),
    [
        ("redshift", "atlan-redshift-prod"),
        (" redshift ", "atlan-redshift-prod"),
        ("\tredshift\n", "atlan-redshift-prod"),
    ],
)
def test_the_app_name_is_stripped(
    monkeypatch: pytest.MonkeyPatch, app_name: str, expected: str
) -> None:
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")
    assert worker_task_queue(app_name) == expected


@pytest.mark.parametrize("app_name", ["", "   ", "\t", "\n"])
def test_a_blank_app_name_is_refused(
    monkeypatch: pytest.MonkeyPatch, app_name: str
) -> None:
    """derive_task_queue answers None rather than manufacturing a queue; the
    equivalent decision here is to refuse, not to emit 'atlan--prod'."""
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")
    with pytest.raises(ValueError, match="app_name is required"):
        worker_task_queue(app_name)


def test_the_bare_queue_form_still_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no deployment the queue is the bare app name -- still stripped."""
    monkeypatch.delenv("ATLAN_DEPLOYMENT_NAME", raising=False)
    assert worker_task_queue(" redshift ") == "redshift"


def test_app_name_is_a_required_argument() -> None:
    """The default of "" is what made a blank name reachable at all."""

    class _H(Handler):
        async def test_auth(self, *args, **kwargs): ...
        async def preflight_check(self, *args, **kwargs): ...
        async def fetch_metadata(self, *args, **kwargs): ...

    with pytest.raises(TypeError):
        build_asgi_app(_H())  # type: ignore[call-arg]
