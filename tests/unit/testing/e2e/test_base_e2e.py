"""Unit tests for BaseE2ETest — manifest loader and substitution walker.

Covers the two methods the review flagged as uncovered (TEST-001/G4):
  - ``_apply_mustache_subs``: recursive {{...}} replacement
  - ``_seed_dag_from_manifest``: manifest JSON loading + queue patching + subs
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application_sdk.contracts.types import ConnectionRef
from application_sdk.testing.e2e.base import BaseE2ETest
from application_sdk.testing.e2e.payload import RunMode
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.full_dag._errors import (
    ManifestDagMissingError,
    ManifestFileNotFoundError,
)


def _make_connection_ref() -> ConnectionRef:
    return ConnectionRef.model_validate(
        {
            "typeName": "Connection",
            "attributes": {
                "qualifiedName": "default/openapi/test-123",
                "name": "test-conn",
                "connectorName": "openapi",
                "adminUsers": [],
                "adminGroups": [],
                "adminRoles": [],
            },
        }
    )


class _ConcreteE2ETest(BaseE2ETest):
    """Minimal concrete subclass for unit testing BaseE2ETest without setup_method."""

    connector_short_name = "openapi"
    argo_package_name = "@atlan/openapi"
    argo_template_name = "atlan-openapi"
    mode = RunMode.DIRECT
    app_service_url = "http://openapi.svc"

    def _mustache_substitutions(self) -> MustacheSubstitutions:
        return MustacheSubstitutions(connection=_make_connection_ref())


# ---------------------------------------------------------------------------
# _apply_mustache_subs
# ---------------------------------------------------------------------------


class TestApplyMustacheSubs:
    """Recursive {{...}} replacement — exact-match only, no partial substitution."""

    def setup_method(self) -> None:
        self.harness = _ConcreteE2ETest()

    def test_replaces_matching_string(self) -> None:
        assert self.harness._apply_mustache_subs("{{foo}}", {"{{foo}}": "bar"}) == "bar"

    def test_leaves_non_matching_string(self) -> None:
        assert (
            self.harness._apply_mustache_subs("{{foo}}", {"{{other}}": "bar"})
            == "{{foo}}"
        )

    def test_partial_match_not_substituted(self) -> None:
        # Only whole-string matches are replaced; substrings are left alone.
        result = self.harness._apply_mustache_subs(
            "prefix-{{foo}}-suffix", {"{{foo}}": "bar"}
        )
        assert result == "prefix-{{foo}}-suffix"

    def test_recurses_into_dict_values(self) -> None:
        result = self.harness._apply_mustache_subs(
            {"key": "{{val}}", "nested": {"k2": "{{v2}}"}},
            {"{{val}}": "a", "{{v2}}": 42},
        )
        assert result == {"key": "a", "nested": {"k2": 42}}

    def test_dict_keys_are_not_substituted(self) -> None:
        result = self.harness._apply_mustache_subs(
            {"{{foo}}": "literal-key"}, {"{{foo}}": "bar"}
        )
        # Values are substituted but keys are preserved.
        assert result == {"{{foo}}": "literal-key"}

    def test_recurses_into_list(self) -> None:
        result = self.harness._apply_mustache_subs(
            ["{{x}}", "unchanged", {"y": "{{x}}"}],
            {"{{x}}": "replaced"},
        )
        assert result == ["replaced", "unchanged", {"y": "replaced"}]

    def test_non_string_scalar_passthrough(self) -> None:
        assert self.harness._apply_mustache_subs(42, {"{{foo}}": "bar"}) == 42

    def test_none_passthrough(self) -> None:
        assert self.harness._apply_mustache_subs(None, {"{{foo}}": "bar"}) is None

    def test_replacement_value_can_be_dict(self) -> None:
        payload = {"conn": "{{connection}}"}
        subs = {"{{connection}}": {"typeName": "Connection", "attributes": {}}}
        result = self.harness._apply_mustache_subs(payload, subs)
        assert result["conn"]["typeName"] == "Connection"


# ---------------------------------------------------------------------------
# _seed_dag_from_manifest
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, dag: dict[str, Any]) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"dag": dag}))
    return manifest


class TestSeedDagFromManifest:
    """Manifest loader: file resolution, queue patching, mustache substitution."""

    def setup_method(self) -> None:
        self.harness = _ConcreteE2ETest()
        self.harness.tenant_deployment_name = "production"  # type: ignore[attr-defined]

    # --- error paths -------------------------------------------------------

    def test_missing_file_raises(self) -> None:
        self.harness.manifest_path = "/no/such/file/manifest.json"  # type: ignore[attr-defined]
        with pytest.raises(ManifestFileNotFoundError):
            self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")

    def test_missing_dag_key_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"other_key": {}}))
        self.harness.manifest_path = str(manifest)  # type: ignore[attr-defined]
        with pytest.raises(ManifestDagMissingError):
            self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")

    def test_empty_dag_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"dag": {}}))
        self.harness.manifest_path = str(manifest)  # type: ignore[attr-defined]
        with pytest.raises(ManifestDagMissingError):
            self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")

    # --- happy path --------------------------------------------------------

    def test_extract_node_queue_replaced_with_caller_queue(
        self, tmp_path: Path
    ) -> None:
        dag: dict[str, Any] = {
            "extract": {
                "node_type": "workflow",
                "app_name": "openapi",
                "app_task_queue": "atlan-openapi-{deployment_name}",
                "inputs": {
                    "task_queue": "atlan-openapi-{deployment_name}",
                    "args": {},
                },
            }
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-99")
        assert result["extract"]["inputs"]["task_queue"] == "atlan-openapi-agent-99"

    def test_non_extract_queue_substitutes_deployment_name(
        self, tmp_path: Path
    ) -> None:
        dag = {
            "publish": {
                "node_type": "workflow",
                "app_name": "publish",
                "app_task_queue": "atlan-publish-{deployment_name}",
                "inputs": {
                    "task_queue": "atlan-publish-{deployment_name}",
                    "args": {},
                },
            }
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")
        assert result["publish"]["inputs"]["task_queue"] == "atlan-publish-production"

    def test_app_name_placeholder_substituted(self, tmp_path: Path) -> None:
        dag = {
            "extract": {
                "node_type": "workflow",
                "app_name": "{app_name}",
                "app_task_queue": "atlan-{app_name}-production",
                "inputs": {
                    "task_queue": "atlan-{app_name}-production",
                    "app_name": "{app_name}",
                    "args": {},
                },
            }
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")
        assert result["extract"]["app_name"] == "openapi"
        assert result["extract"]["inputs"]["app_name"] == "openapi"

    def test_mustache_subs_applied_to_args(self, tmp_path: Path) -> None:
        dag = {
            "extract": {
                "node_type": "workflow",
                "app_name": "openapi",
                "app_task_queue": "atlan-openapi-production",
                "inputs": {
                    "task_queue": "atlan-openapi-production",
                    "args": {
                        "connection": "{{connection}}",
                        "static_value": "unchanged",
                    },
                },
            }
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")
        args = result["extract"]["inputs"]["args"]
        # {{connection}} is replaced with the typed ConnectionRef dict.
        assert isinstance(args["connection"], dict)
        assert args["connection"]["typeName"] == "Connection"
        assert (
            args["connection"]["attributes"]["qualifiedName"]
            == "default/openapi/test-123"
        )
        # Static values pass through unchanged.
        assert args["static_value"] == "unchanged"

    def test_unresolved_mustache_key_left_as_is(self, tmp_path: Path) -> None:
        dag = {
            "extract": {
                "node_type": "workflow",
                "app_name": "openapi",
                "app_task_queue": "atlan-openapi-production",
                "inputs": {
                    "task_queue": "atlan-openapi-production",
                    "args": {"unknown_key": "{{no-such-sub}}"},
                },
            }
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")
        # Keys absent from the subs model are left as literal strings.
        assert result["extract"]["inputs"]["args"]["unknown_key"] == "{{no-such-sub}}"

    def test_returns_all_dag_nodes(self, tmp_path: Path) -> None:
        dag = {
            "extract": {
                "node_type": "workflow",
                "app_name": "openapi",
                "app_task_queue": "atlan-openapi-production",
                "inputs": {"task_queue": "atlan-openapi-production", "args": {}},
            },
            "publish": {
                "node_type": "workflow",
                "app_name": "publish",
                "app_task_queue": "atlan-publish-{deployment_name}",
                "inputs": {"task_queue": "atlan-publish-{deployment_name}", "args": {}},
                "depends_on": {"node_id": "extract"},
            },
        }
        self.harness.manifest_path = str(_write_manifest(tmp_path, dag))  # type: ignore[attr-defined]
        result = self.harness._seed_dag_from_manifest("atlan-openapi-agent-1")
        assert set(result.keys()) == {"extract", "publish"}
