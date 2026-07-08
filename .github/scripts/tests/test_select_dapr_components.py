"""Tests for .github/actions/sdr-e2e/select_dapr_components.py.

The module under test lives alongside the sdr-e2e composite action (not
under .github/scripts/) because it's invoked via the action's
`${{ steps.action_root.outputs.path }}` — the same co-location the
composite already uses for test-config.yaml.tmpl and components/*.yaml, so
it resolves correctly whether the action is referenced remotely
(`uses: atlanhq/application-sdk/.github/actions/sdr-e2e@main`) or locally.
The test itself stays under .github/scripts/tests/ so `scripts-tests.yaml`
picks it up without a new pytest invocation, importing by explicit path
the same way test_write_dapr_components.py does.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "actions" / "sdr-e2e"))

from select_dapr_components import (  # noqa: E402
    MissingUpstreamComponentError,
    _resolve_app_components_dir,
    main,
    select_components,
)


def _write(path: Path, name: str, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"apiVersion: dapr.io/v1alpha1\nkind: Component\nmetadata:\n  name: {name}\nspec:\n  type: {kind}\n"
    )


def _make_sdk_defaults(tmp_path: Path) -> Path:
    sdk_dir = tmp_path / "sdk-components"
    _write(sdk_dir / "objectstore.yaml", "objectstore", "bindings.localstorage")
    _write(sdk_dir / "statestore.yaml", "statestore", "state.in-memory")
    return sdk_dir


def test_single_store_copies_sdk_defaults_then_app_overrides(tmp_path: Path) -> None:
    sdk_dir = _make_sdk_defaults(tmp_path)
    app_dir = tmp_path / "app-components"
    _write(app_dir / "objectstore.yaml", "objectstore", "bindings.aws.s3")
    ci_dir = tmp_path / "ci-deploy" / "components"

    skipped = select_components(ci_dir, sdk_dir, app_dir, two_store=False)

    assert skipped == []
    assert "bindings.aws.s3" in (ci_dir / "objectstore.yaml").read_text()
    assert (ci_dir / "statestore.yaml").exists()


def test_two_store_skips_app_objectstore_override(tmp_path: Path) -> None:
    sdk_dir = _make_sdk_defaults(tmp_path)
    app_dir = tmp_path / "app-components"
    _write(app_dir / "objectstore.yaml", "objectstore", "bindings.aws.s3")
    ci_dir = tmp_path / "ci-deploy" / "components"
    _write(ci_dir / "atlan-objectstore.yaml", "atlan-objectstore", "bindings.aws.s3")

    skipped = select_components(ci_dir, sdk_dir, app_dir, two_store=True)

    assert skipped == ["objectstore.yaml"]
    # SDK-default localstorage wins, not the app's tenant-blobstorage override.
    assert "bindings.localstorage" in (ci_dir / "objectstore.yaml").read_text()


def test_two_store_applies_non_objectstore_app_overrides(tmp_path: Path) -> None:
    sdk_dir = _make_sdk_defaults(tmp_path)
    app_dir = tmp_path / "app-components"
    _write(app_dir / "statestore.yaml", "statestore", "state.redis")
    ci_dir = tmp_path / "ci-deploy" / "components"
    _write(ci_dir / "atlan-objectstore.yaml", "atlan-objectstore", "bindings.aws.s3")

    select_components(ci_dir, sdk_dir, app_dir, two_store=True)

    assert "state.redis" in (ci_dir / "statestore.yaml").read_text()


def test_two_store_missing_atlan_objectstore_raises(tmp_path: Path) -> None:
    sdk_dir = _make_sdk_defaults(tmp_path)
    ci_dir = tmp_path / "ci-deploy" / "components"

    raised = False
    try:
        select_components(ci_dir, sdk_dir, None, two_store=True)
    except MissingUpstreamComponentError as exc:
        raised = True
        assert "atlan-objectstore.yaml" in str(exc)
    assert raised, "expected MissingUpstreamComponentError"


def test_single_store_missing_atlan_objectstore_is_fine(tmp_path: Path) -> None:
    sdk_dir = _make_sdk_defaults(tmp_path)
    ci_dir = tmp_path / "ci-deploy" / "components"

    skipped = select_components(ci_dir, sdk_dir, None, two_store=False)

    assert skipped == []
    assert not (ci_dir / "atlan-objectstore.yaml").exists()


def test_no_app_dir_is_a_noop(tmp_path: Path) -> None:
    sdk_dir = _make_sdk_defaults(tmp_path)
    ci_dir = tmp_path / "ci-deploy" / "components"

    skipped = select_components(ci_dir, sdk_dir, None, two_store=False)

    assert skipped == []
    assert (ci_dir / "objectstore.yaml").exists()


def test_empty_app_dir_is_a_noop(tmp_path: Path) -> None:
    sdk_dir = _make_sdk_defaults(tmp_path)
    app_dir = tmp_path / "app-components"
    app_dir.mkdir()
    ci_dir = tmp_path / "ci-deploy" / "components"

    skipped = select_components(ci_dir, sdk_dir, app_dir, two_store=False)

    assert skipped == []


def test_existing_configurator_output_is_not_clobbered(tmp_path: Path) -> None:
    # ci_components_dir already has configurator-generated files (e.g.
    # atlan-objectstore.yaml + secretstore.yaml) before this script ever
    # runs; select_components only writes the filenames it knows about.
    sdk_dir = _make_sdk_defaults(tmp_path)
    ci_dir = tmp_path / "ci-deploy" / "components"
    _write(ci_dir / "atlan-objectstore.yaml", "atlan-objectstore", "bindings.aws.s3")
    _write(ci_dir / "secretstore.yaml", "secretstore", "secretstores.local.env")

    select_components(ci_dir, sdk_dir, None, two_store=True)

    assert "secretstores.local.env" in (ci_dir / "secretstore.yaml").read_text()
    assert "bindings.aws.s3" in (ci_dir / "atlan-objectstore.yaml").read_text()


def test_resolve_app_components_dir_prefers_explicit_input() -> None:
    resolved = _resolve_app_components_dir("explicit/dir", ".github/e2e")
    assert resolved == Path("explicit/dir")


def test_resolve_app_components_dir_falls_back_to_sdr_config_convention() -> None:
    resolved = _resolve_app_components_dir("", ".github/e2e")
    assert resolved == Path(".github/e2e/components")


def test_resolve_app_components_dir_none_when_neither_set() -> None:
    assert _resolve_app_components_dir("", "") is None


def test_main_two_store_missing_atlan_objectstore_exits_nonzero(
    tmp_path: Path, capsys
) -> None:
    sdk_dir = _make_sdk_defaults(tmp_path)
    ci_dir = tmp_path / "ci-deploy" / "components"

    rc = main(
        [
            "--ci-components-dir",
            str(ci_dir),
            "--sdk-components-dir",
            str(sdk_dir),
            "--two-store",
            "true",
        ]
    )

    assert rc == 1
    assert "::error::" in capsys.readouterr().err


def test_main_two_store_success_reports_effective_components(
    tmp_path: Path, capsys
) -> None:
    sdk_dir = _make_sdk_defaults(tmp_path)
    ci_dir = tmp_path / "ci-deploy" / "components"
    _write(ci_dir / "atlan-objectstore.yaml", "atlan-objectstore", "bindings.aws.s3")

    rc = main(
        [
            "--ci-components-dir",
            str(ci_dir),
            "--sdk-components-dir",
            str(sdk_dir),
            "--two-store",
            "true",
        ]
    )

    assert rc == 0
    assert "atlan-objectstore.yaml" in capsys.readouterr().err


def test_main_resolves_app_components_dir_from_sdr_config_dir(
    tmp_path: Path,
) -> None:
    sdk_dir = _make_sdk_defaults(tmp_path)
    ci_dir = tmp_path / "ci-deploy" / "components"
    sdr_config_dir = tmp_path / ".github" / "e2e"
    _write(
        sdr_config_dir / "components" / "objectstore.yaml",
        "objectstore",
        "bindings.aws.s3",
    )

    rc = main(
        [
            "--ci-components-dir",
            str(ci_dir),
            "--sdk-components-dir",
            str(sdk_dir),
            "--sdr-config-dir",
            str(sdr_config_dir),
            "--two-store",
            "false",
        ]
    )

    assert rc == 0
    assert "bindings.aws.s3" in (ci_dir / "objectstore.yaml").read_text()
