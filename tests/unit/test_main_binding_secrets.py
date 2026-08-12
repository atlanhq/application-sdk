"""BLDX-1619: main.py pre-resolves a binding's secretKeyRef entries.

``main.py`` awaits ``wait_for_dapr_sidecar()`` before it builds the object
stores, so it is the one place that can reach the Dapr secret store on the
worker's behalf.  It fetches the secrets there and hands them to the (still
synchronous, still public) obstore resolver.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

COMPONENT_WITH_SECRET_STORE = """\
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

COMPONENT_WITHOUT_SECRET_STORE = """\
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
    - name: accessKey
      value: AKIAEXAMPLE
"""


def _components(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "components"
    d.mkdir()
    (d / "atlan-objectstore.yaml").write_text(body)
    return d


class TestFetchBindingSecrets:
    async def test_fetches_each_secret_from_the_declared_store(
        self, tmp_path: Path
    ) -> None:
        from application_sdk.main import _fetch_binding_secrets

        client = mock.Mock()
        client.get_secret = mock.AsyncMock(
            return_value={
                "ATLAN_AUTH_CLIENT_ID": "id-1",
                "ATLAN_AUTH_CLIENT_SECRET": "secret-1",
            }
        )

        secrets = await _fetch_binding_secrets(
            client,
            "atlan-objectstore",
            components_dir=_components(tmp_path, COMPONENT_WITH_SECRET_STORE),
        )

        client.get_secret.assert_awaited_once_with(
            "deployment-secret-store", "atlan-auth-secret"
        )
        assert secrets == {
            "atlan-auth-secret": {
                "ATLAN_AUTH_CLIENT_ID": "id-1",
                "ATLAN_AUTH_CLIENT_SECRET": "secret-1",
            }
        }

    async def test_skips_the_sidecar_when_no_secret_store_is_declared(
        self, tmp_path: Path
    ) -> None:
        """No auth.secretStore means the env path — do not call the sidecar."""
        from application_sdk.main import _fetch_binding_secrets

        client = mock.Mock()
        client.get_secret = mock.AsyncMock()

        secrets = await _fetch_binding_secrets(
            client,
            "atlan-objectstore",
            components_dir=_components(tmp_path, COMPONENT_WITHOUT_SECRET_STORE),
        )

        assert secrets == {}
        client.get_secret.assert_not_awaited()

    async def test_a_failing_secret_lookup_does_not_abort_the_others(
        self, tmp_path: Path
    ) -> None:
        """One unreadable secret must not hide the ones that did resolve."""
        from application_sdk.main import _fetch_binding_secrets

        body = COMPONENT_WITH_SECRET_STORE.replace(
            """      secretKeyRef:
        name: atlan-auth-secret
        key: ATLAN_AUTH_CLIENT_SECRET""",
            """      secretKeyRef:
        name: other-secret
        key: ATLAN_AUTH_CLIENT_SECRET""",
        )

        async def _get_secret(store: str, name: str) -> dict[str, str]:
            if name == "other-secret":
                raise RuntimeError("secret store denied")
            return {"ATLAN_AUTH_CLIENT_ID": "id-1"}

        client = mock.Mock()
        client.get_secret = mock.AsyncMock(side_effect=_get_secret)

        secrets = await _fetch_binding_secrets(
            client, "atlan-objectstore", components_dir=_components(tmp_path, body)
        )

        assert secrets == {"atlan-auth-secret": {"ATLAN_AUTH_CLIENT_ID": "id-1"}}

    async def test_an_absent_component_is_not_an_error(self, tmp_path: Path) -> None:
        from application_sdk.main import _fetch_binding_secrets

        empty = tmp_path / "empty"
        empty.mkdir()
        client = mock.Mock()
        client.get_secret = mock.AsyncMock()

        assert (
            await _fetch_binding_secrets(
                client, "atlan-objectstore", components_dir=empty
            )
            == {}
        )
        client.get_secret.assert_not_awaited()


@pytest.mark.parametrize("required", [True, False])
async def test_infrastructure_passes_the_fetched_secrets_to_the_resolver(
    tmp_path: Path, required: bool
) -> None:
    """The secrets main.py fetched must reach the upstream store resolver."""
    from application_sdk import main as main_mod

    components = _components(tmp_path, COMPONENT_WITH_SECRET_STORE)
    fetched = {"atlan-auth-secret": {"ATLAN_AUTH_CLIENT_ID": "id-1"}}

    with (
        mock.patch.dict(
            "os.environ",
            {
                "DAPR_HTTP_PORT": "3500",
                "DAPR_COMPONENTS_PATH": str(components),
            },
        ),
        mock.patch.object(
            main_mod, "_log_dapr_components", new=mock.AsyncMock(return_value=[])
        ),
        mock.patch(
            "application_sdk.infrastructure._dapr.http.wait_for_dapr_sidecar",
            new=mock.AsyncMock(),
        ),
        mock.patch("application_sdk.constants.ENABLE_ATLAN_UPLOAD", required),
        mock.patch.object(
            main_mod, "_fetch_binding_secrets", new=mock.AsyncMock(return_value=fetched)
        ),
        mock.patch(
            "application_sdk.storage.binding._create_store_from_binding_optional_with_put_attrs",
            return_value=(mock.Mock(), None),
        ) as upstream_resolver,
        mock.patch(
            "application_sdk.storage.create_store_from_binding_with_put_attrs",
            return_value=(mock.Mock(), None),
        ),
        mock.patch("application_sdk.infrastructure._dapr.http.AsyncDaprClient"),
    ):
        await main_mod._create_infrastructure()

    assert upstream_resolver.call_args.kwargs["secrets"] == fetched
