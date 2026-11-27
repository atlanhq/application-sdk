"""connector-integration-tests sets ATLAN_LOCAL_DEVELOPMENT=true (FND-5517).

Connector ``main.py`` files, not the SDK, read ``ATLAN_LOCAL_DEVELOPMENT``:
with it ``"true"`` and ``DAPR_HTTP_PORT`` unset they call ``app.run_dev``,
which boots the embedded daprd and an in-process Temporal. Without it they fall
back to ``application_sdk.main.main()``, which needs an external sidecar.

On SDK >= 3.13 the action does not start an external daprd or Temporal unless
``force-external-runtime`` is set. So when the variable was dropped from the
action (because nothing under ``application_sdk/`` reads it), nothing started
Dapr, and every such app failed at "Start app server" with
``DaprNotDetectedError``. A grep of the SDK cannot see this contract, so these
tests pin it.
"""

from __future__ import annotations

import pytest
from _workflows import ACTIONS, load_workflow, step_named

ACTION = ACTIONS / "connector-integration-tests" / "action.yaml"


@pytest.mark.parametrize(
    "step",
    ["Start app server", "Run integration tests (parallel via pytest-xdist)"],
)
def test_step_sets_atlan_local_development(step: str) -> None:
    env: dict[str, object] = step_named(load_workflow(ACTION), step).get("env") or {}
    assert env.get("ATLAN_LOCAL_DEVELOPMENT") == "true", (
        f'{step!r} must set ATLAN_LOCAL_DEVELOPMENT: "true": connector main.py '
        "reads it to boot the embedded runtime, and without it the app server "
        "never finds a Dapr sidecar."
    )
