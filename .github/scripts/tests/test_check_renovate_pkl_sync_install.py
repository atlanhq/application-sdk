"""Tests for .github/scripts/check_renovate_pkl_sync_install.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import check_renovate_pkl_sync_install as guard

DRIVER_WITH_SIBLING = "from pkl_contract_layout import x, y\n"

WORKFLOW_MISSING_SIBLING = (
    "run: sudo install -m 0755 .github/scripts/renovate_pkl_sync.py "
    "/usr/local/bin/renovate-pkl-sync\n"
)

WORKFLOW_WITH_SIBLING = (
    "run: |\n"
    "  sudo install -m 0755 .github/scripts/renovate_pkl_sync.py "
    "/usr/local/bin/renovate-pkl-sync\n"
    "  sudo install -m 0644 .github/scripts/pkl_contract_layout.py "
    "/usr/local/bin/pkl_contract_layout.py\n"
)


class TestLocalSiblingImports:
    def test_flags_import_matching_a_real_local_file(self):
        assert "pkl_contract_layout" in guard.local_sibling_imports(DRIVER_WITH_SIBLING)

    def test_ignores_import_with_no_matching_local_file(self):
        assert "not_a_real_module" not in guard.local_sibling_imports(
            "from not_a_real_module import z\n"
        )

    def test_ignores_stdlib_imports(self):
        assert guard.local_sibling_imports("import os\nimport sys\n") == set()

    def test_flags_plain_import_matching_a_real_local_file(self):
        assert "pkl_contract_layout" in guard.local_sibling_imports(
            "import pkl_contract_layout\n"
        )

    def test_flags_aliased_import_matching_a_real_local_file(self):
        assert "pkl_contract_layout" in guard.local_sibling_imports(
            "import pkl_contract_layout as layout\n"
        )


class TestInstalledModuleNames:
    def test_captures_installed_source_stem(self):
        assert "renovate_pkl_sync" in guard.installed_module_names(
            WORKFLOW_MISSING_SIBLING
        )

    def test_captures_multiple_install_lines(self):
        names = guard.installed_module_names(WORKFLOW_WITH_SIBLING)
        assert {"renovate_pkl_sync", "pkl_contract_layout"} <= names


class TestMissingInstalls:
    def test_flags_uninstalled_sibling(self):
        assert guard.missing_installs(
            DRIVER_WITH_SIBLING, WORKFLOW_MISSING_SIBLING
        ) == {"pkl_contract_layout"}

    def test_clean_when_sibling_is_installed(self):
        assert (
            guard.missing_installs(DRIVER_WITH_SIBLING, WORKFLOW_WITH_SIBLING) == set()
        )

    def test_clean_against_the_real_driver_and_workflow(self):
        # Regression pin: this is the exact bug that shipped once (contract-
        # toolkit 0.20.0 rollout) -- pkl_contract_layout.py split out of
        # renovate_pkl_sync.py without updating the fleet runner's install step.
        assert (
            guard.missing_installs(guard.DRIVER.read_text(), guard.WORKFLOW.read_text())
            == set()
        )


class TestMain:
    def test_exits_nonzero_on_missing_install(self, monkeypatch, tmp_path):
        driver = tmp_path / "renovate_pkl_sync.py"
        driver.write_text(DRIVER_WITH_SIBLING)
        (tmp_path / "pkl_contract_layout.py").write_text("x = 1\n")
        workflows_dir = tmp_path.parent / "workflows"
        workflows_dir.mkdir(exist_ok=True)
        workflow = workflows_dir / "renovate.yaml"
        workflow.write_text(WORKFLOW_MISSING_SIBLING)

        monkeypatch.setattr(guard, "SCRIPTS_DIR", tmp_path)
        monkeypatch.setattr(guard, "DRIVER", driver)
        monkeypatch.setattr(guard, "WORKFLOW", workflow)

        assert guard.main() == 1

    def test_exits_zero_against_the_real_repo_state(self):
        assert guard.main() == 0
