"""Tests for .github/scripts/two_store_setup.py.

Covers the driver's decision + side effects — the part that used to be inlined
``case``/``if`` shell in the sdr-e2e action's "Build compose -f chain" step:

  * non-allowlisted app        -> no-op (no tokens, no files, no env)
  * allowlisted app            -> component copied, mount dirs created, guard
                                  env vars appended, overlay tokens returned
  * stale configurator component named atlan-objectstore -> removed by NAME
    (even under a different filename), unrelated components kept
  * missing GITHUB_ENV         -> file ops still run, env export skipped
  * main() STDOUT              -> ONLY the compose tokens (diagnostics -> STDERR)
    so the caller's ``read -a`` builds a clean ``-f <overlay>`` chain
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import two_store_setup as mod


def _layout(tmp_path):
    """Build a throwaway (assets, components, workspace, github_env) layout."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "atlan-objectstore.yaml").write_text(
        "kind: Component\nmetadata:\n  name: atlan-objectstore\n"
    )
    (assets / "docker-compose.two-store.yml").write_text("services: {}\n")
    components = tmp_path / "ci-deploy" / "components"
    components.mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    github_env = tmp_path / "gh_env"
    github_env.write_text("")
    return assets, components, workspace, github_env


def test_non_allowlisted_app_is_noop(tmp_path):
    assets, components, workspace, gh_env = _layout(tmp_path)
    tokens = mod.setup(
        "snowflake",
        assets_dir=str(assets),
        components_dir=str(components),
        workspace=str(workspace),
        github_env=str(gh_env),
    )
    assert tokens == []
    assert list(components.iterdir()) == []  # nothing copied
    assert gh_env.read_text() == ""  # no env written
    assert not (workspace / "data").exists()  # no mount dirs


def test_allowlisted_app_full_setup(tmp_path):
    assets, components, workspace, gh_env = _layout(tmp_path)
    tokens = mod.setup(
        "mysql",
        assets_dir=str(assets),
        components_dir=str(components),
        workspace=str(workspace),
        github_env=str(gh_env),
    )
    assert tokens == ["-f", str(assets / "docker-compose.two-store.yml")]
    assert (components / "atlan-objectstore.yaml").exists()  # ours copied in
    for sub in ("data", "data-upstream"):
        d = workspace / sub
        assert d.is_dir()
        assert (d.stat().st_mode & 0o777) == 0o777  # host-writable mount
    env = gh_env.read_text()
    assert "SDR_REQUIRE_ASSETS_LANDED=true" in env
    assert "SDR_REQUIRE_UPSTREAM_ASSETS_LANDED=true" in env
    assert "SDR_EXTRACTED_OUTPUT_BASE_PATH=data/artifacts/apps/mysql/workflows" in env
    assert (
        "SDR_UPSTREAM_OUTPUT_BASE_PATH=data-upstream/artifacts/apps/mysql/workflows"
        in env
    )
    assert "PYTEST_ADDOPTS=-s" in env


def test_removes_configurator_atlan_objectstore_by_name(tmp_path):
    assets, components, workspace, gh_env = _layout(tmp_path)
    # A configurator component under a DIFFERENT filename that declares
    # metadata.name: atlan-objectstore must be removed so ours wins.
    stale = components / "40-objectstore.yaml"
    stale.write_text(
        "apiVersion: dapr.io/v1alpha1\nkind: Component\n"
        "metadata:\n  name: atlan-objectstore\nspec:\n  type: bindings.aws.s3\n"
    )
    other = components / "statestore.yaml"
    other.write_text("metadata:\n  name: statestore\n")
    mod.setup(
        "oracle",
        assets_dir=str(assets),
        components_dir=str(components),
        workspace=str(workspace),
        github_env=str(gh_env),
    )
    assert not stale.exists()  # stale one removed by name
    assert other.exists()  # unrelated component kept
    assert (components / "atlan-objectstore.yaml").exists()  # ours copied in


def test_missing_github_env_skips_env_write_but_still_sets_up(tmp_path):
    assets, components, workspace, _ = _layout(tmp_path)
    tokens = mod.setup(
        "looker",
        assets_dir=str(assets),
        components_dir=str(components),
        workspace=str(workspace),
        github_env=None,
    )
    assert tokens[0] == "-f"
    assert (components / "atlan-objectstore.yaml").exists()


def test_main_stdout_is_only_the_overlay_tokens(tmp_path, capsys, monkeypatch):
    assets, _components, workspace, gh_env = _layout(tmp_path)
    monkeypatch.chdir(tmp_path)  # cwd holds ci-deploy/components
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    monkeypatch.setenv("GITHUB_ENV", str(gh_env))
    rc = mod.main(["mysql", str(assets)])
    assert rc == 0
    out = capsys.readouterr()
    overlay = str(assets / "docker-compose.two-store.yml")
    # STDOUT carries ONLY the tokens; the "enabled" diagnostic goes to STDERR.
    assert out.out.strip().split() == ["-f", overlay]
    assert "enabled" not in out.out
    assert "Two-store SDR mode enabled for 'mysql'" in out.err


def test_main_non_allowlisted_prints_nothing(tmp_path, capsys, monkeypatch):
    assets, _components, workspace, gh_env = _layout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    monkeypatch.setenv("GITHUB_ENV", str(gh_env))
    rc = mod.main(["snowflake", str(assets)])
    assert rc == 0
    out = capsys.readouterr()
    # Empty stdout → the caller's `read -a` yields an empty array (no -f added).
    assert out.out.strip() == ""
