"""``/workflows/v1/validate`` enforces required credential fields (FND-502).

Runs on the consolidated API host against BigQuery's generated forms
(trimmed fixtures): the credential form ``atlan-connectors-bigquery.json``
and the ``crawler`` entry point's workflow form.
"""

from __future__ import annotations

import shutil
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.base import DefaultHandler

_FIXTURE = Path(__file__).parent / "fixtures" / "generated_bigquery_credentials"
_PACKAGE = "fnd502_validate_credentials_app"
_ROUTE = "/workflows/v1/validate?entrypoint=crawler"

_INPUT_MODULE = textwrap.dedent(
    """
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.templates.contracts import ExtractionInput


    class AppInputContract(ExtractionInput):
        bigquery_crawler_credential: CredentialRef | None = None
    """
)

_WIF_EXTRA = {
    "project_id": "example-project",
    "service_account_email": "sa@example.invalid",
    "wif_pool_provider_id": "example-pool",
    "atlan_oauth_id": "example-client-id",
    "atlan_oauth_secret": "example-client-secret",
}


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    package = tmp_path / _PACKAGE
    generated = package / "generated"
    shutil.copytree(_FIXTURE, generated)
    (package / "__init__.py").write_text("")
    (generated / "__init__.py").write_text("")
    (generated / "crawler" / "__init__.py").write_text("")
    (generated / "crawler" / "_input.py").write_text(_INPUT_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield TestClient(
            build_asgi_app(
                DefaultHandler(),
                app_name="bigquery-validate",
                app_package=_PACKAGE,
                generated_dir=generated,
                object_store=None,
            ),
            raise_server_exceptions=False,
        )
    finally:
        for name in [m for m in sys.modules if m.startswith(_PACKAGE)]:
            del sys.modules[name]


def _fields(response: Any) -> list[str]:
    return [item["field"] for item in response.json()["detail"]]


class TestInlineCredential:
    """Credentials sent inline, as at credential creation."""

    def test_gcp_wif_without_oauth_client_id_is_rejected(self, host) -> None:
        """FND-502 acceptance: a field-level 422 naming atlan_oauth_id."""
        extra = {k: v for k, v in _WIF_EXTRA.items() if k != "atlan_oauth_id"}
        response = host.post(
            _ROUTE,
            json={
                "credential_guid": "guid-1",
                "credentials": {"authType": "gcp-wif", "extra": extra},
            },
        )

        assert response.status_code == 422
        assert _fields(response) == ["credentials.extra.atlan_oauth_id"]
        assert response.json()["detail"][0]["type"] == "missing"
        # Only field names come back, never a credential value.
        for value in extra.values():
            assert value not in response.text

    def test_complete_gcp_wif_credential_is_accepted(self, host) -> None:
        response = host.post(
            _ROUTE,
            json={
                "credential_guid": "guid-1",
                "credentials": {"authType": "gcp-wif", "extra": _WIF_EXTRA},
            },
        )

        assert response.status_code == 200, response.text

    def test_v3_pairs_are_checked_the_same_way(self, host) -> None:
        pairs = [{"key": "authType", "value": "basic"}]
        pairs.append({"key": "username", "value": "sa@example.invalid"})
        response = host.post(
            _ROUTE, json={"credential_guid": "guid-1", "credentials": pairs}
        )

        assert response.status_code == 422
        assert _fields(response) == [
            "credentials.password",
            "credentials.extra.project_id",
        ]

    def test_v2_flat_credential_is_checked(self, host) -> None:
        response = host.post(
            _ROUTE,
            json={
                "credential_guid": "guid-1",
                "authType": "gcp-wif",
                "extra": {k: v for k, v in _WIF_EXTRA.items() if k != "project_id"},
            },
        )

        assert response.status_code == 422
        assert _fields(response) == ["credentials.extra.project_id"]


class TestWorkflowCredentialReference:
    """The workflow body itself: a required credential must be referenced."""

    def test_direct_workflow_without_a_credential_is_rejected(self, host) -> None:
        response = host.post(_ROUTE, json={"extraction_method": "direct"})

        assert response.status_code == 422
        assert _fields(response) == ["credential_guid"]

    def test_direct_workflow_with_a_guid_is_accepted(self, host) -> None:
        response = host.post(_ROUTE, json={"credential_guid": "guid-1"})

        assert response.status_code == 200, response.text

    def test_ae_nested_kebab_reference_is_recognised(self, host) -> None:
        """AE nests config under ``metadata`` with kebab keys."""
        response = host.post(
            _ROUTE,
            json={
                "metadata": {
                    "credential-guid": "guid-1",
                    "extraction-method": "direct",
                }
            },
        )

        assert response.status_code == 200, response.text

    def test_a_typed_credential_ref_satisfies_the_slot(self, host) -> None:
        response = host.post(
            _ROUTE,
            json={
                "bigquery_crawler_credential": {
                    "name": "bq",
                    "credential_type": "unknown",
                    "credential_guid": "guid-1",
                }
            },
        )

        assert response.status_code == 200, response.text

    def test_agent_workflow_missing_an_oauth_mapping_is_rejected(self, host) -> None:
        agent = {
            "agent-name": "agent-1",
            "secret-manager": "awssecretmanager",
            "secret-path": "path/to/secret",
            "auth-type": "gcp-wif",
        }
        agent.update(
            {
                f"gcp-wif.extra.{k}": f"key-{k}"
                for k in _WIF_EXTRA
                if k != "atlan_oauth_id"
            }
        )
        response = host.post(
            _ROUTE, json={"extraction_method": "agent", "agent_json": agent}
        )

        assert response.status_code == 422
        assert _fields(response) == ["agent_json.gcp-wif.extra.atlan_oauth_id"]

    def test_complete_agent_workflow_is_accepted(self, host) -> None:
        agent = {
            "agent-name": "agent-1",
            "secret-manager": "awssecretmanager",
            "secret-path": "path/to/secret",
            "auth-type": "gcp-wif",
        }
        agent.update({f"gcp-wif.extra.{k}": f"key-{k}" for k in _WIF_EXTRA})
        response = host.post(
            _ROUTE, json={"extraction_method": "agent", "agent_json": agent}
        )

        assert response.status_code == 200, response.text

    def test_agent_workflow_without_an_agent_spec_is_rejected(self, host) -> None:
        response = host.post(_ROUTE, json={"extraction_method": "agent"})

        assert response.status_code == 422
        assert _fields(response) == ["agent_json"]
