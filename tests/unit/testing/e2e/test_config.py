"""Unit tests for the deprecated ``testing.e2e.config`` module.

The module had no test file of its own — ``tests/unit/test_app_config.py`` is
about :class:`application_sdk.main.AppConfig`, the production runtime config,
which is the *other* class in the name collision this deprecation exists to
resolve. That gap is named in FND-239; this closes it.

``test_scaffold.py`` covers the rename from the harness side (the new class
keeps only the fields anything reads). This covers the compatibility promise
from the deprecated side: an existing call site keeps working, unchanged.
"""

from __future__ import annotations

import dataclasses
import warnings

import pytest

from application_sdk.main import AppConfig as RuntimeAppConfig
from application_sdk.testing.e2e.config import APP_CONFIG_REMOVAL_VERSION, AppConfig
from application_sdk.testing.harness import AppUnderTest


def _config(**overrides: object) -> AppConfig:
    """Build an ``AppConfig`` without the deprecation warning failing the test."""
    fields: dict[str, object] = {"app_name": "my-app", "namespace": "app-my-app"}
    fields.update(overrides)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return AppConfig(**fields)  # type: ignore[arg-type]


def test_the_two_app_configs_really_are_different_classes() -> None:
    """The collision the rename resolves: one is a test-harness locator, the
    other is the object every app is configured through."""
    assert AppConfig is not RuntimeAppConfig
    assert not issubclass(AppConfig, RuntimeAppConfig)


def test_every_dropped_field_is_still_accepted() -> None:
    """All seven kwargs, exactly as the one known consumer passes them."""
    config = _config(
        app_module="my_app.main:App",
        image="ghcr.io/org/my-app:latest",
        handler_port=9000,
        worker_health_port=8081,
        timeout=600,
    )
    assert (config.app_name, config.namespace, config.handler_port) == (
        "my-app",
        "app-my-app",
        9000,
    )
    assert config.app_module == "my_app.main:App"
    assert config.timeout == 600


def test_the_dropped_fields_default_rather_than_being_required() -> None:
    config = _config()
    assert (config.app_module, config.image) == ("", "")
    assert (config.worker_health_port, config.timeout) == (8081, 300)


def test_it_is_an_app_under_test_so_a_harness_call_site_accepts_it() -> None:
    assert isinstance(_config(), AppUnderTest)


def test_it_stays_frozen() -> None:
    """Losing immutability in the shim would let a call site mutate a value the
    replacement cannot."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        _config().app_name = "other"  # type: ignore[misc]


def test_the_warning_names_the_replacement_and_the_removal_version() -> None:
    with pytest.warns(DeprecationWarning) as caught:
        AppConfig(app_name="a", namespace="b")
    message = str(caught[0].message)
    assert "application_sdk.testing.harness.AppUnderTest" in message
    assert f"v{APP_CONFIG_REMOVAL_VERSION}" in message


def test_the_warning_says_the_dropped_fields_are_not_carried_over() -> None:
    """A call site that reads one of them back off the replacement would get an
    AttributeError, so the warning has to say so rather than only that the class
    moved."""
    with pytest.warns(DeprecationWarning) as caught:
        AppConfig(app_name="a", namespace="b", timeout=1)
    message = str(caught[0].message)
    assert "app_module, image, worker_health_port and timeout" in message


def test_the_removal_version_is_a_major() -> None:
    """Every deprecation in this SDK names its removal version, and a field
    removal is a break, so it can only land on a major."""
    assert APP_CONFIG_REMOVAL_VERSION.endswith(".0")
