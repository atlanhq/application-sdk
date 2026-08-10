"""BLDX-1619: secretKeyRef resolution via a component's ``auth.secretStore``.

A k8s SDR deployment renders ``atlan-objectstore`` with ``secretKeyRef``
entries plus ``auth: secretStore: deployment-secret-store``, and deliberately
does *not* inject the matching env vars into the worker.  The Dapr sidecar
resolves that fine; the SDK's own obstore resolver used to read env vars only,
so it saw an unresolvable component, treated it as absent, and left
``upstream_storage`` as ``None`` — a silent wrong-bucket write.

These tests pin the resolver's two contracts:
  1. a caller-supplied secret map resolves ``secretKeyRef`` entries
  2. ``required=True`` surfaces a broken binding instead of swallowing it
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

SECURE_COMPONENT = """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: atlan-objectstore
spec:
  version: v1
  type: bindings.aws.s3
  metadata:
    - name: bucket
      value: tenant-bucket
    - name: region
      value: us-east-1
    - name: accessKey
      secretKeyRef:
        name: atlan-auth-secret
        key: ATLAN_AUTH_CLIENT_ID
    - name: secretKey
      secretKeyRef:
        name: atlan-auth-secret
        key: ATLAN_AUTH_CLIENT_SECRET
auth:
  secretStore: deployment-secret-store
"""

# Same component, no auth block, ref name == key (the Docker Compose /
# secretstores.local.env shape that already worked via env vars).
ENV_COMPONENT = """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: atlan-objectstore
spec:
  version: v1
  type: bindings.aws.s3
  metadata:
    - name: bucket
      value: tenant-bucket
    - name: region
      value: us-east-1
    - name: accessKey
      secretKeyRef:
        name: ATLAN_AUTH_CLIENT_ID
        key: ATLAN_AUTH_CLIENT_ID
    - name: secretKey
      secretKeyRef:
        name: ATLAN_AUTH_CLIENT_SECRET
        key: ATLAN_AUTH_CLIENT_SECRET
"""

SECRETS = {
    "atlan-auth-secret": {
        "ATLAN_AUTH_CLIENT_ID": "id-from-secret-store",
        "ATLAN_AUTH_CLIENT_SECRET": "secret-from-secret-store",
    }
}


def _write(tmp_path: Path, body: str, filename: str = "atlan-objectstore.yaml") -> Path:
    d = tmp_path / filename.removesuffix(".yaml")
    d.mkdir()
    (d / filename).write_text(body)
    return d


@pytest.fixture()
def secure_dir(tmp_path: Path) -> Path:
    return _write(tmp_path, SECURE_COMPONENT)


@pytest.fixture(autouse=True)
def _no_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The secure Helm path gates these env vars off — model that explicitly."""
    monkeypatch.delenv("ATLAN_AUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("ATLAN_AUTH_CLIENT_SECRET", raising=False)


@pytest.fixture()
def captured_s3_config():
    """Capture the obstore S3 config the resolver builds, without touching AWS."""
    captured: dict[str, object] = {}

    def _fake_make_s3_store(bucket, config, **kwargs):
        captured["bucket"] = bucket
        captured["config"] = config
        return mock.Mock(name="S3Store")

    with mock.patch(
        "application_sdk.storage._obstore_config.make_s3_store",
        side_effect=_fake_make_s3_store,
    ):
        yield captured


# ===========================================================================
# read_binding_secret_refs — what main.py needs before it can fetch anything
# ===========================================================================


class TestReadBindingSecretRefs:
    def test_reports_the_declared_secret_store_and_every_ref(
        self, secure_dir: Path
    ) -> None:
        from application_sdk.storage.binding import read_binding_secret_refs

        result = read_binding_secret_refs(
            "atlan-objectstore", components_dir=secure_dir
        )

        assert result.secret_store == "deployment-secret-store"
        assert sorted(result.refs) == [
            ("atlan-auth-secret", "ATLAN_AUTH_CLIENT_ID"),
            ("atlan-auth-secret", "ATLAN_AUTH_CLIENT_SECRET"),
        ]
        assert result.secret_names == ["atlan-auth-secret"]

    def test_reports_no_secret_store_when_the_component_declares_none(
        self, tmp_path: Path
    ) -> None:
        from application_sdk.storage.binding import read_binding_secret_refs

        result = read_binding_secret_refs(
            "atlan-objectstore", components_dir=_write(tmp_path, ENV_COMPONENT)
        )

        assert result.secret_store is None
        assert result.secret_names == [
            "ATLAN_AUTH_CLIENT_ID",
            "ATLAN_AUTH_CLIENT_SECRET",
        ]

    def test_returns_empty_for_an_absent_component(self, tmp_path: Path) -> None:
        from application_sdk.storage.binding import read_binding_secret_refs

        empty = tmp_path / "empty"
        empty.mkdir()
        result = read_binding_secret_refs("atlan-objectstore", components_dir=empty)

        assert result.secret_store is None
        assert result.refs == []


# ===========================================================================
# Claim 1 — the resolver honours a caller-supplied secret map
# ===========================================================================


class TestSecretMapResolution:
    def test_secret_map_resolves_the_credentials_into_the_store_config(
        self, secure_dir: Path, captured_s3_config: dict
    ) -> None:
        from application_sdk.storage.binding import create_store_from_binding

        create_store_from_binding(
            "atlan-objectstore", components_dir=secure_dir, secrets=SECRETS
        )

        config = captured_s3_config["config"]
        assert config["aws_access_key_id"] == "id-from-secret-store"
        assert config["aws_secret_access_key"] == "secret-from-secret-store"

    def test_without_a_secret_map_the_component_is_still_broken(
        self, secure_dir: Path
    ) -> None:
        """No map and no env var — nothing to resolve from. Must not guess."""
        from application_sdk.storage.binding import create_store_from_binding
        from application_sdk.storage.errors import StorageBindingBrokenError

        with pytest.raises(StorageBindingBrokenError) as exc:
            create_store_from_binding("atlan-objectstore", components_dir=secure_dir)
        assert set(exc.value.broken_fields or []) == {"accessKey", "secretKey"}

    def test_env_vars_still_resolve_when_no_secret_map_is_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_s3_config: dict
    ) -> None:
        """Back-compat: the Docker Compose / local.env path is unchanged."""
        from application_sdk.storage.binding import create_store_from_binding

        monkeypatch.setenv("ATLAN_AUTH_CLIENT_ID", "id-from-env")
        monkeypatch.setenv("ATLAN_AUTH_CLIENT_SECRET", "secret-from-env")

        create_store_from_binding(
            "atlan-objectstore", components_dir=_write(tmp_path, ENV_COMPONENT)
        )

        assert captured_s3_config["config"]["aws_access_key_id"] == "id-from-env"

    def test_secret_map_wins_over_a_conflicting_env_var(
        self,
        secure_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        captured_s3_config: dict,
    ) -> None:
        """auth.secretStore is authoritative — env is only the fallback."""
        from application_sdk.storage.binding import create_store_from_binding

        monkeypatch.setenv("ATLAN_AUTH_CLIENT_ID", "id-from-env")
        monkeypatch.setenv("ATLAN_AUTH_CLIENT_SECRET", "secret-from-env")

        create_store_from_binding(
            "atlan-objectstore", components_dir=secure_dir, secrets=SECRETS
        )

        assert (
            captured_s3_config["config"]["aws_access_key_id"] == "id-from-secret-store"
        )

    def test_env_covers_a_ref_the_secret_map_misses(
        self,
        secure_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        captured_s3_config: dict,
    ) -> None:
        """A partial map falls back per-ref rather than failing the whole component."""
        from application_sdk.storage.binding import create_store_from_binding

        monkeypatch.setenv("ATLAN_AUTH_CLIENT_SECRET", "secret-from-env")
        partial = {
            "atlan-auth-secret": {"ATLAN_AUTH_CLIENT_ID": "id-from-secret-store"}
        }

        create_store_from_binding(
            "atlan-objectstore", components_dir=secure_dir, secrets=partial
        )

        config = captured_s3_config["config"]
        assert config["aws_access_key_id"] == "id-from-secret-store"
        assert config["aws_secret_access_key"] == "secret-from-env"

    def test_is_binding_configured_accepts_the_secret_map(
        self, secure_dir: Path
    ) -> None:
        from application_sdk.storage.binding import is_binding_configured

        assert (
            is_binding_configured("atlan-objectstore", components_dir=secure_dir)
            is False
        )
        assert (
            is_binding_configured(
                "atlan-objectstore", components_dir=secure_dir, secrets=SECRETS
            )
            is True
        )


# ===========================================================================
# Claim 2 — required= must be honoured for a broken binding
# ===========================================================================


class TestRequiredHonoursBrokenBinding:
    def test_required_true_raises_on_a_broken_binding(self, secure_dir: Path) -> None:
        from application_sdk.storage.binding import (
            _create_store_from_binding_optional_with_put_attrs,
        )
        from application_sdk.storage.errors import StorageBindingBrokenError

        with pytest.raises(StorageBindingBrokenError) as exc:
            _create_store_from_binding_optional_with_put_attrs(
                "atlan-objectstore", components_dir=secure_dir, required=True
            )
        assert exc.value.binding_name == "atlan-objectstore"
        assert set(exc.value.broken_fields or []) == {"accessKey", "secretKey"}

    def test_required_false_still_degrades_to_none(self, secure_dir: Path) -> None:
        """Non-SDR deployments keep the tolerant behaviour."""
        from application_sdk.storage.binding import (
            _create_store_from_binding_optional_with_put_attrs,
        )

        assert _create_store_from_binding_optional_with_put_attrs(
            "atlan-objectstore", components_dir=secure_dir, required=False
        ) == (None, None)

    def test_required_true_succeeds_once_the_secret_map_resolves_the_refs(
        self, secure_dir: Path, captured_s3_config: dict
    ) -> None:
        from application_sdk.storage.binding import (
            _create_store_from_binding_optional_with_put_attrs,
        )

        store, _ = _create_store_from_binding_optional_with_put_attrs(
            "atlan-objectstore",
            components_dir=secure_dir,
            required=True,
            secrets=SECRETS,
        )
        assert store is not None
        assert (
            captured_s3_config["config"]["aws_access_key_id"] == "id-from-secret-store"
        )

    def test_required_true_still_raises_when_the_component_is_absent(
        self, tmp_path: Path
    ) -> None:
        from application_sdk.storage.binding import (
            _create_store_from_binding_optional_with_put_attrs,
        )
        from application_sdk.storage.errors import StorageBindingNotFoundError

        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(StorageBindingNotFoundError):
            _create_store_from_binding_optional_with_put_attrs(
                "atlan-objectstore", components_dir=empty, required=True
            )
