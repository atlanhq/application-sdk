"""Unit tests for ``application_sdk.dev._dapr_components``."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import yaml

from application_sdk.dev import _dapr_components
from application_sdk.dev._dapr_components import (
    COMPONENT_TEMPLATES,
    DEFAULT_EVENTSTORE_ROOT,
    DEFAULT_OBJECTSTORE_ROOT,
    FILE_SECRET_STORE_NAMES,
    render_components,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_module_imports_only_the_standard_library() -> None:
    # .github/scripts/write_dapr_components.py loads this file by path under the
    # runner's system python3, with no SDK dependencies installed. Any non-stdlib
    # import (including application_sdk itself) would break both CI callers.
    tree = ast.parse(Path(_dapr_components.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports cannot load by file path"
            imported.add((node.module or "").split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    assert imported <= allowed, f"non-stdlib imports: {sorted(imported - allowed)}"


def test_paths_are_inserted_verbatim() -> None:
    rendered = render_components(
        objectstore_root="/tmp/obj", eventstore_root="relative/evt"
    )
    assert "value: /tmp/obj" in rendered["objectstore.yaml"]
    assert "value: relative/evt" in rendered["eventstore.yaml"]


def test_default_set_uses_env_secret_stores() -> None:
    rendered = render_components(objectstore_root="/o", eventstore_root="/e")
    assert set(rendered) == set(COMPONENT_TEMPLATES)
    for name in FILE_SECRET_STORE_NAMES:
        spec = yaml.safe_load(rendered[f"{name}.yaml"])["spec"]
        assert spec["type"] == "secretstores.local.env"


def test_secrets_file_switches_both_secret_stores_to_file() -> None:
    rendered = render_components(
        objectstore_root="/o", eventstore_root="/e", secrets_file="/s/creds.json"
    )
    assert set(rendered) == set(COMPONENT_TEMPLATES)
    for name in FILE_SECRET_STORE_NAMES:
        doc = yaml.safe_load(rendered[f"{name}.yaml"])
        assert doc["metadata"]["name"] == name
        assert doc["spec"]["type"] == "secretstores.local.file"
        meta = {m["name"]: m["value"] for m in doc["spec"]["metadata"]}
        assert meta == {"secretsFile": "/s/creds.json", "nestedSeparator": ":"}


@pytest.mark.parametrize(
    ("filename", "default_root"),
    [
        ("objectstore.yaml", DEFAULT_OBJECTSTORE_ROOT),
        ("eventstore.yaml", DEFAULT_EVENTSTORE_ROOT),
    ],
)
def test_wheel_shipped_components_share_the_default_roots(
    filename: str, default_root: str
) -> None:
    # components/ ships in the wheel (application_sdk/components/) for consumer
    # apps and is not rendered from the templates; its localstorage roots must
    # still point where the embedded runtime and CI write, so local data lands
    # in the same place whichever path started daprd.
    doc = yaml.safe_load((_REPO_ROOT / "components" / filename).read_text())
    meta = {m["name"]: m["value"] for m in doc["spec"]["metadata"]}
    assert meta["rootPath"] == default_root
