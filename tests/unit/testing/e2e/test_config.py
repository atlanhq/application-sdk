"""Unit tests for the deprecated ``testing.e2e.config`` module.

The module had no test file of its own — ``tests/unit/test_app_config.py`` is
about :class:`application_sdk.main.AppConfig`, the production runtime config,
which is the *other* class in the name collision this deprecation exists to
resolve. That gap is named in FND-239.

Most of it is now closed from the harness side: ``test_scaffold.py`` covers the
keyword and positional forms, the restored mutability, the absence of an
instance ``__dict__``, and the removal version. This file covers what neither
that file nor the runtime one does — the collision itself, the defaults, and the
two consequences of the shim not being a dataclass any more.
"""

from __future__ import annotations

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
        # arg-type: the overrides are typed `object` so a caller can pass any
        # field; AppConfig's parameters are str/int.
        return AppConfig(**fields)  # type: ignore[arg-type]


def test_the_two_app_configs_really_are_different_classes() -> None:
    """The collision the rename resolves, and the reason this file exists: one is
    a test-harness locator, the other is the object every app is configured
    through. ``tests/unit/test_app_config.py`` tests that one."""
    assert AppConfig is not RuntimeAppConfig
    assert not issubclass(AppConfig, RuntimeAppConfig)
    assert AppConfig.__module__ != RuntimeAppConfig.__module__


def test_nothing_is_required_any_more() -> None:
    """A shim should accept everything the old signature accepted and a little
    more — never less. The four originally-required fields default rather than
    raising, so a call site that drops one gets a deprecation, not a TypeError."""
    config = _config()
    assert (config.app_module, config.image) == ("", "")
    assert (config.worker_health_port, config.timeout) == (8081, 300)
    with pytest.warns(DeprecationWarning):
        assert AppConfig().app_name == ""


def test_the_dead_fields_are_stored_but_carry_no_meaning() -> None:
    """They are readable — a call site that sets one can read it back — and they
    are absent from the replacement, which is the whole point of dropping them."""
    config = _config(image="ghcr.io/org/my-app:latest", timeout=600)
    assert config.image == "ghcr.io/org/my-app:latest"
    assert config.timeout == 600
    assert not hasattr(AppUnderTest(app_name="a", namespace="b"), "image")


def test_two_configs_differing_only_in_a_dead_field_compare_equal() -> None:
    """``__eq__`` comes from AppUnderTest, so it considers only the three live
    fields. Correct, given the other four have no effect on anything — but it is
    a behaviour change from the original dataclass, so it is pinned."""
    assert _config(image="a", timeout=1) == _config(image="b", timeout=2)
    assert _config(namespace="one") != _config(namespace="two")


def test_a_dead_field_can_be_deleted_as_well_as_set() -> None:
    """The original was a plain mutable dataclass, so ``__delattr__`` worked. The
    shim restores both halves; restoring only ``__setattr__`` would leave a
    frozen-flavoured hole in a class that is otherwise mutable."""
    config = _config(timeout=600)
    del config.timeout
    assert not hasattr(config, "timeout")


def test_the_warning_says_the_dropped_fields_are_not_carried_over() -> None:
    """A call site that reads one of them back off the replacement gets an
    AttributeError, so the warning has to say which fields do not survive — not
    only that the class moved."""
    with pytest.warns(DeprecationWarning) as caught:
        AppConfig(app_name="a", namespace="b", timeout=1)
    assert "app_module, image, worker_health_port and timeout" in str(caught[0].message)


def test_the_removal_version_is_a_major() -> None:
    """Every deprecation in this SDK names its removal version, and dropping four
    fields is a break, so it can only land on a major."""
    assert APP_CONFIG_REMOVAL_VERSION.endswith(".0")
