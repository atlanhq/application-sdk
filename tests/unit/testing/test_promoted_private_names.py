"""Private names connectors imported now have public homes (FND-4661).

Each private name keeps working until v4.0.0 as a deprecated alias: it must
warn, and it must return what the public name returns. An alias that stopped
resolving would break the connectors that import it today; one that resolved
silently would give them no migration signal.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from application_sdk.app.registry import AppRegistry
from application_sdk.testing import apply_mustache_subs
from application_sdk.testing._mustache import (
    apply_mustache_subs as private_apply_mustache_subs,
)
from application_sdk.testing.e2e import payload
from application_sdk.testing.integration import fixtures, runner
from application_sdk.testing.integration.fixtures import KitOptions


def test_agent_credential_ref_keys_alias_warns_and_delegates() -> None:
    expected = payload.agent_credential_ref_keys(
        auth_type="iam", connector_short_name="glue"
    )
    with pytest.warns(DeprecationWarning, match="use agent_credential_ref_keys"):
        actual = payload._agent_credential_ref_keys(  # pyright: ignore[reportDeprecated]
            auth_type="iam", connector_short_name="glue"
        )
    assert (
        actual
        == expected
        == {
            "iam.username": "SDR_GLUE_USERNAME",
            "iam.password": "SDR_GLUE_PASSWORD",
        }
    )


def test_auto_discover_credentials_alias_warns_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "fakeapp")
    monkeypatch.setenv("E2E_FAKEAPP_USERNAME", "someone")
    expected = runner.auto_discover_credentials()
    with pytest.warns(DeprecationWarning, match="use auto_discover_credentials"):
        actual = runner._auto_discover_credentials()  # pyright: ignore[reportDeprecated]
    assert actual == expected == {"username": "someone"}


def test_verify_registration_alias_warns_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Registered:
        _app_name = "registered-app"

    monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
    monkeypatch.setattr(
        AppRegistry.get_instance(), "list_apps", lambda: ["registered-app"]
    )
    with pytest.warns(DeprecationWarning, match="use verify_registration"):
        name = fixtures._verify_registration(_Registered)  # type: ignore[arg-type]  # pyright: ignore[reportDeprecated]
    assert name == fixtures.verify_registration(_Registered) == "registered-app"  # type: ignore[arg-type]


def test_artifact_preservation_alias_warns_and_delegates() -> None:
    with pytest.warns(DeprecationWarning, match="use artifact_preservation"):
        manager = fixtures._artifact_preservation(KitOptions())  # pyright: ignore[reportDeprecated]
    with manager:
        pass


def test_current_outputs_alias_warns_and_is_the_same_object() -> None:
    outputs = importlib.import_module("application_sdk.outputs")
    with pytest.warns(
        DeprecationWarning, match="use application_sdk.outputs.current_outputs"
    ):
        alias: Any = outputs._current_outputs
    assert alias is outputs.current_outputs


def test_apply_mustache_subs_is_public_from_testing() -> None:
    assert apply_mustache_subs is private_apply_mustache_subs
    assert apply_mustache_subs({"a": ["{{x}}"]}, {"{{x}}": 1}) == {"a": [1]}


def test_promoted_kit_helpers_stay_out_of_the_star_import() -> None:
    """54 connectors star-import the kit, so ``__all__`` must not grow."""
    assert "verify_registration" not in fixtures.__all__
    assert "artifact_preservation" not in fixtures.__all__
