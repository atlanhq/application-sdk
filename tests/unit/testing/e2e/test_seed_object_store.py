"""``BaseE2ETest.seed_object_store`` — the binding this resolves is a contract.

Every other seeding test monkeypatches this method away, which is right for
those tests and wrong as the whole story: the store it picks is where the seed's
NDJSON lands, and the tenant's ``publish`` app has to be able to read that exact
bucket. Resolve the wrong binding — the connector's CI-local deployment store
instead of the tenant blobstorage — and publish reads an empty prefix, reports
success, and the seed silently lands nothing.

So the default binding name, the default components directory, the two env
overrides and the absent-component failure are all pinned here, against a
patched :func:`create_store_from_binding_optional`. No Dapr component files, no
tenant.
"""

from __future__ import annotations

from typing import Any

import pytest

from application_sdk.testing.e2e.base import (
    _DEFAULT_SEED_COMPONENTS_DIR,
    _DEFAULT_SEED_STORE_BINDING,
    BaseE2ETest,
)
from application_sdk.testing.e2e.payload import RunMode
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.harness import seed as harness_seed

#: The two env vars a CI leg uses to redirect the lookup without a code change.
_COMPONENTS_ENV = "E2E_SEED_COMPONENTS_DIR"
_BINDING_ENV = "E2E_SEED_STORE_BINDING"


class _StoreE2ETest(BaseE2ETest):
    """Minimal concrete subclass, built without ``setup_method``."""

    connector_short_name = "openapi"
    mode = RunMode.DIRECT

    def _mustache_substitutions(self) -> MustacheSubstitutions:  # pragma: no cover
        raise NotImplementedError


def _record_lookup(
    monkeypatch: pytest.MonkeyPatch, *, store: object | None
) -> list[tuple[str, Any]]:
    """Patch the binding resolver, recording ``(binding, components_dir)``."""
    calls: list[tuple[str, Any]] = []

    def _resolve(name: str, *, components_dir: Any, **_: Any) -> object | None:
        calls.append((name, components_dir))
        return store

    monkeypatch.setattr(
        "application_sdk.testing.e2e.base.create_store_from_binding_optional", _resolve
    )
    return calls


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither override is set unless a test sets it.

    Autouse because a leg that exports one of these into the pytest process
    would otherwise silently rewrite the defaults these tests exist to pin.
    """
    monkeypatch.delenv(_COMPONENTS_ENV, raising=False)
    monkeypatch.delenv(_BINDING_ENV, raising=False)


class TestDefaultBinding:
    """What an unconfigured leg resolves, spelled out rather than derived."""

    def test_the_tenant_blobstorage_binding_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``atlan-objectstore`` is the configurator-emitted TENANT binding.
        ``objectstore`` is the connector's own deployment store, and in
        two-store mode it is a path inside the worker container — publish
        cannot read it."""
        sentinel = object()
        calls = _record_lookup(monkeypatch, store=sentinel)
        assert _StoreE2ETest().seed_object_store() is sentinel
        assert calls == [("atlan-objectstore", "ci-deploy/components")]

    def test_the_defaults_are_the_named_constants(self) -> None:
        """Pinned separately from the call above so a rename of either constant
        cannot quietly move the lookup while the assertion still reads true."""
        assert _DEFAULT_SEED_STORE_BINDING == "atlan-objectstore"
        assert _DEFAULT_SEED_COMPONENTS_DIR == "ci-deploy/components"


class TestEnvironmentOverrides:
    """A leg whose layout differs redirects the lookup without a code change."""

    def test_both_overrides_are_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_COMPONENTS_ENV, "custom/components")
        monkeypatch.setenv(_BINDING_ENV, "tenant-store")
        calls = _record_lookup(monkeypatch, store=object())
        _StoreE2ETest().seed_object_store()
        assert calls == [("tenant-store", "custom/components")]

    def test_each_override_is_independent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting one must not drag the other off its default."""
        monkeypatch.setenv(_BINDING_ENV, "tenant-store")
        calls = _record_lookup(monkeypatch, store=object())
        _StoreE2ETest().seed_object_store()
        assert calls == [("tenant-store", _DEFAULT_SEED_COMPONENTS_DIR)]

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_override_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ) -> None:
        """An unset GitHub Actions input arrives as an empty string, so blank
        has to mean "not set" — resolving a binding named ``""`` would fail as
        a missing component and point the reader at the wrong thing."""
        monkeypatch.setenv(_COMPONENTS_ENV, blank)
        monkeypatch.setenv(_BINDING_ENV, blank)
        calls = _record_lookup(monkeypatch, store=object())
        _StoreE2ETest().seed_object_store()
        assert calls == [(_DEFAULT_SEED_STORE_BINDING, _DEFAULT_SEED_COMPONENTS_DIR)]


class TestMissingStore:
    """No binding is a precondition failure, and it has to say which one."""

    def test_an_absent_component_raises_naming_both_halves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _record_lookup(monkeypatch, store=None)
        with pytest.raises(harness_seed.SeedStoreUnavailableError) as caught:
            _StoreE2ETest().seed_object_store()
        message = str(caught.value)
        assert _DEFAULT_SEED_STORE_BINDING in message
        assert _DEFAULT_SEED_COMPONENTS_DIR in message

    def test_the_message_names_the_overrides_that_would_fix_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reader is on a CI leg with no tenant access; the remedy has to be
        in the failure, not in a doc they have to go and find."""
        _record_lookup(monkeypatch, store=None)
        with pytest.raises(harness_seed.SeedStoreUnavailableError) as caught:
            _StoreE2ETest().seed_object_store()
        message = str(caught.value)
        assert _COMPONENTS_ENV in message
        assert _BINDING_ENV in message

    def test_the_override_that_was_used_is_the_one_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reporting the default after an override was applied would send the
        reader to a directory the run never looked in."""
        monkeypatch.setenv(_COMPONENTS_ENV, "custom/components")
        monkeypatch.setenv(_BINDING_ENV, "tenant-store")
        _record_lookup(monkeypatch, store=None)
        with pytest.raises(harness_seed.SeedStoreUnavailableError) as caught:
            _StoreE2ETest().seed_object_store()
        assert caught.value.resource == "custom/components/tenant-store"
        assert "tenant-store" in str(caught.value)
