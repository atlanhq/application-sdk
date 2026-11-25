"""Tests for .github/scripts/write_dapr_components.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from write_dapr_components import (
    SDR_E2E_SECRETS_FILE,
    main,
    render_components,
    write_components,
    write_sdr_e2e_components,
)

from application_sdk.dev._dapr_components import (
    render_components as sdk_render_components,
)

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
    # Guards against a filename/metadata-name mismatch in the shared templates.
    rendered = render_components(objectstore_root="/o", eventstore_root="/e")
    assert {name.removesuffix(".yaml") for name in rendered} == (
        _EXPECTED_COMPONENT_NAMES
    )
    for filename, content in rendered.items():
        assert yaml.safe_load(content)["metadata"]["name"] == filename.removesuffix(
            ".yaml"
        )


def test_renders_the_sdk_embedded_runtime_templates() -> None:
    # The script must render the SDK's own templates, not a copy: the same
    # inputs give byte-identical output to application_sdk.dev._dapr_components.
    kwargs = {
        "objectstore_root": "/o",
        "eventstore_root": "/e",
        "secrets_file": "/s.json",
    }
    assert render_components(**kwargs) == sdk_render_components(**kwargs)


def test_sdr_e2e_profile_writes_container_layout(tmp_path: Path) -> None:
    components = tmp_path / "components"
    result = write_sdr_e2e_components(components)

    assert result == {
        "objectstore.yaml": "wrote",
        "eventstore.yaml": "wrote",
        "secretstore.yaml": "wrote",
    }

    def _meta(name: str) -> dict[str, str]:
        spec = _load(components / f"{name}.yaml")["spec"]
        return {m["name"]: m["value"] for m in spec["metadata"]}

    # Container paths go in verbatim: never resolved against the runner.
    assert _meta("objectstore") == {"rootPath": "/tmp/atlan-data"}
    assert _meta("eventstore") == {"rootPath": "/tmp/atlan-eventstore"}
    secret_spec = _load(components / "secretstore.yaml")["spec"]
    assert secret_spec["type"] == "secretstores.local.file"
    assert _meta("secretstore")["secretsFile"] == SDR_E2E_SECRETS_FILE


def test_main_sdr_e2e_profile(tmp_path: Path) -> None:
    rc = main([str(tmp_path / "out"), "--profile", "sdr-e2e"])
    assert rc == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "eventstore.yaml",
        "objectstore.yaml",
        "secretstore.yaml",
    ]


def test_main_sdr_e2e_profile_rejects_host_roots(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path), "--profile", "sdr-e2e", "--objectstore-root", "x"])
    assert exc.value.code == 2


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
