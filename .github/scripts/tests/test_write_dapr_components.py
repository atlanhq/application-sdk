"""Tests for .github/scripts/write_dapr_components.py."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from write_dapr_components import _COMPONENTS, main, write_components  # noqa: E402

# The names the SDK resolves at startup; objectstore is the one whose absence
# crashed CI (StorageBindingNotFoundError [AAF-STR-003]).
_EXPECTED_COMPONENT_NAMES = {
    "statestore",
    "secretstore",
    "deployment-secret-store",
    "objectstore",
    "eventstore",
    "pubsub",
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_writes_full_component_set_into_empty_dir(tmp_path: Path) -> None:
    components = tmp_path / "components"
    result = write_components(
        components,
        objectstore_root=tmp_path / "obj",
        eventstore_root=tmp_path / "evt",
    )

    assert set(result.values()) == {"wrote"}
    written_names = {_load(p)["metadata"]["name"] for p in components.glob("*.yaml")}
    assert written_names == _EXPECTED_COMPONENT_NAMES


def test_objectstore_is_localstorage_rooted_at_given_path(tmp_path: Path) -> None:
    components = tmp_path / "components"
    obj_root = tmp_path / "obj"
    write_components(
        components, objectstore_root=obj_root, eventstore_root=tmp_path / "evt"
    )

    spec = _load(components / "objectstore.yaml")["spec"]
    assert spec["type"] == "bindings.localstorage"
    # rootPath is resolved to an absolute path daprd/the SDK can read.
    root_path = next(m["value"] for m in spec["metadata"] if m["name"] == "rootPath")
    assert Path(root_path).is_absolute()
    assert Path(root_path) == obj_root.resolve()


def test_creates_binding_root_directories(tmp_path: Path) -> None:
    obj_root = tmp_path / "obj"
    evt_root = tmp_path / "evt"
    write_components(
        tmp_path / "components", objectstore_root=obj_root, eventstore_root=evt_root
    )

    assert obj_root.is_dir()
    assert evt_root.is_dir()


def test_does_not_overwrite_existing_component(tmp_path: Path) -> None:
    components = tmp_path / "components"
    components.mkdir()
    # Simulate a connector shipping a real (e.g. S3) objectstore component.
    sentinel = "apiVersion: dapr.io/v1alpha1\nkind: Component\nmetadata:\n  name: objectstore\nspec:\n  type: bindings.aws.s3\n"
    (components / "objectstore.yaml").write_text(sentinel)

    result = write_components(
        components, objectstore_root=tmp_path / "obj", eventstore_root=tmp_path / "evt"
    )

    assert result["objectstore.yaml"] == "kept"
    assert (components / "objectstore.yaml").read_text() == sentinel
    # The rest are still filled in.
    assert result["statestore.yaml"] == "wrote"


def test_idempotent_second_run_keeps_everything(tmp_path: Path) -> None:
    components = tmp_path / "components"
    write_components(
        components, objectstore_root=tmp_path / "obj", eventstore_root=tmp_path / "evt"
    )
    second = write_components(
        components, objectstore_root=tmp_path / "obj", eventstore_root=tmp_path / "evt"
    )
    assert set(second.values()) == {"kept"}


def test_all_written_yaml_is_valid_and_named(tmp_path: Path) -> None:
    components = tmp_path / "components"
    write_components(
        components, objectstore_root=tmp_path / "obj", eventstore_root=tmp_path / "evt"
    )
    for path in components.glob("*.yaml"):
        doc = _load(path)
        assert doc["kind"] == "Component"
        assert doc["metadata"]["name"]
        assert doc["spec"]["type"]


def test_template_keys_match_expected_names() -> None:
    # Guards against a filename/metadata-name mismatch when editing _COMPONENTS.
    assert {
        name.removesuffix(".yaml") for name in _COMPONENTS
    } == _EXPECTED_COMPONENT_NAMES


def test_main_defaults_to_components_dir_and_exits_zero(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    assert (tmp_path / "components" / "objectstore.yaml").exists()
    out = capsys.readouterr().out
    assert "components/objectstore.yaml" in out


def test_main_accepts_explicit_dir_and_roots(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    rc = main(
        [
            "my-components",
            "--objectstore-root",
            "obj-root",
            "--eventstore-root",
            "evt-root",
        ]
    )
    assert rc == 0
    spec = _load(tmp_path / "my-components" / "objectstore.yaml")["spec"]
    root_path = next(m["value"] for m in spec["metadata"] if m["name"] == "rootPath")
    assert Path(root_path) == (tmp_path / "obj-root").resolve()


def test_main_reports_io_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    # Point the components dir at a path whose parent is a file → mkdir fails.
    clash = tmp_path / "afile"
    clash.write_text("x")
    rc = main([str(clash / "components")])
    assert rc == 1
    assert "::error::" in capsys.readouterr().err
