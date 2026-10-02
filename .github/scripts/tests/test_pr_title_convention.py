"""Tests for .github/scripts/pr_title_convention.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pr_title_convention as ptc

# ---------------------------------------------------------------------------
# is_version_bump_pr
# ---------------------------------------------------------------------------


class TestIsVersionBumpPr:
    def test_bump_version_branch(self):
        assert ptc.is_version_bump_pr("chore: unrelated", "bump-version-1.2.3")

    def test_release_title(self):
        assert ptc.is_version_bump_pr("chore: release 1.2.3", "feature/foo")

    def test_scoped_release_title(self):
        assert ptc.is_version_bump_pr("chore(release): release 1.2.3", "feature/foo")

    def test_bump_version_title(self):
        assert ptc.is_version_bump_pr("Bump version to 1.2.3", "feature/foo")

    def test_regular_pr_is_not_a_bump(self):
        assert not ptc.is_version_bump_pr("fix: handle edge case", "feature/foo")


# ---------------------------------------------------------------------------
# classify_files
# ---------------------------------------------------------------------------


class TestClassifyFiles:
    def test_application_sdk_wins(self):
        assert ptc.classify_files(["application_sdk/foo.py"]) == "sdk"

    def test_sdk_takes_precedence_over_docker(self):
        assert ptc.classify_files(["application_sdk/foo.py", "entrypoint.sh"]) == "sdk"

    def test_dockerfile(self):
        assert ptc.classify_files(["Dockerfile"]) == "docker-img"

    def test_entrypoint_sh(self):
        assert ptc.classify_files(["entrypoint.sh"]) == "docker-img"

    def test_nested_dockerfile_does_not_match(self):
        # Only the root-level Dockerfile/entrypoint.sh count as image inputs.
        assert ptc.classify_files(["docker/Dockerfile"]) == "other"

    def test_contract_toolkit_core(self):
        assert ptc.classify_files(["contract-toolkit/src/foo.py"]) == "ct-core"

    @pytest.mark.parametrize(
        "path",
        [
            "contract-toolkit/docs/readme.md",
            "contract-toolkit/examples/basic.py",
            "contract-toolkit/scripts/build.sh",
            "contract-toolkit/tests/test_foo.py",
            "contract-toolkit/.github/workflows/ci.yaml",
        ],
    )
    def test_contract_toolkit_exempt_subfolders(self, path):
        assert ptc.classify_files([path]) == "other"

    def test_conformance_core(self):
        assert ptc.classify_files(["packages/conformance/conformance/foo.py"]) == (
            "cf-core"
        )

    def test_conformance_tests_are_exempt(self):
        assert ptc.classify_files(["packages/conformance/tests/test_foo.py"]) == "other"

    @pytest.mark.parametrize(
        "path",
        [
            "packages/conformance/uv.lock",
            "packages/conformance/ui/package-lock.json",
            "packages/conformance/ui/yarn.lock",
        ],
    )
    def test_conformance_lock_files_are_deps(self, path):
        # Lock files classify as a dependency update; both zones require chore/ci.
        assert ptc.classify_files([path]) == "deps"

    def test_server_core(self):
        assert ptc.classify_files(["packages/server/server_sdk/server.py"]) == (
            "sv-core"
        )

    def test_server_tests_are_exempt(self):
        assert ptc.classify_files(["packages/server/tests/test_foo.py"]) == "other"

    @pytest.mark.parametrize(
        "path",
        [
            "packages/server/uv.lock",
            "packages/server/ui/package-lock.json",
            "packages/server/ui/yarn.lock",
        ],
    )
    def test_server_lock_files_are_deps(self, path):
        assert ptc.classify_files([path]) == "deps"

    @pytest.mark.parametrize(
        "files",
        [
            ["pyproject.toml", "uv.lock"],
            ["packages/conformance/pyproject.toml", "packages/conformance/uv.lock"],
            ["packages/conformance/conformance/package.json"],
            ["contract-toolkit/pyproject.toml"],
            ["requirements.txt"],
        ],
    )
    def test_dependency_manifests_only_is_deps(self, files):
        assert ptc.classify_files(files) == "deps"

    def test_manifest_plus_source_is_not_deps(self):
        # A real source change that also touches a manifest keeps its zone.
        assert ptc.classify_files(
            ["packages/conformance/pyproject.toml", "packages/conformance/foo.py"]
        ) == ("cf-core")
        assert ptc.classify_files(
            ["packages/server/pyproject.toml", "packages/server/server_sdk/x.py"]
        ) == ("sv-core")
        assert ptc.classify_files(["pyproject.toml", "entrypoint.sh"]) == "docker-img"

    def test_docs_only_is_other(self):
        assert ptc.classify_files(["docs/foo.md"]) == "other"

    def test_empty_list_is_other(self):
        assert ptc.classify_files([]) == "other"


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_sdk_zone_never_violates(self):
        assert ptc.validate("sdk", "chore: anything") == (False, "")
        assert ptc.validate("sdk", "fix: anything") == (False, "")

    @pytest.mark.parametrize(
        "title",
        ["fix: handle SIGTERM", "feat: add flag", "fix(entrypoint): x", "fix!: x"],
    )
    def test_docker_img_accepts_feat_fix(self, title):
        assert ptc.validate("docker-img", title) == (False, "")

    def test_docker_img_rejects_chore(self):
        assert ptc.validate("docker-img", "chore: tweak entrypoint") == (
            True,
            "docker-img",
        )

    def test_ct_core_requires_exact_scope(self):
        assert ptc.validate("ct-core", "fix(contract-toolkit): x") == (False, "")
        assert ptc.validate("ct-core", "fix: x") == (True, "ct-core")
        assert ptc.validate("ct-core", "fix(other-scope): x") == (True, "ct-core")

    def test_cf_core_requires_exact_scope(self):
        assert ptc.validate("cf-core", "feat(conformance): x") == (False, "")
        assert ptc.validate("cf-core", "feat: x") == (True, "cf-core")

    def test_deps_accepts_chore_or_ci(self):
        assert ptc.validate(
            "deps", "chore(deps): update dependency ruff to v0.16.7"
        ) == (
            False,
            "",
        )
        assert ptc.validate("deps", "ci: pin uv") == (False, "")

    def test_deps_rejects_feat_fix(self):
        assert ptc.validate("deps", "feat(conformance): bump ruff") == (True, "deps")

    def test_other_accepts_chore_or_ci(self):
        assert ptc.validate("other", "chore: tidy") == (False, "")
        assert ptc.validate("other", "ci(publish): tidy") == (False, "")

    def test_other_rejects_feat_fix(self):
        assert ptc.validate("other", "fix: tidy") == (True, "chore-ci")


# ---------------------------------------------------------------------------
# run() / main() integration
# ---------------------------------------------------------------------------


def _write_changed_files(tmp_path: Path, files: list) -> Path:
    p = tmp_path / "changed_files.txt"
    p.write_text("\n".join(files) + ("\n" if files else ""))
    return p


class TestRun:
    def test_version_bump_pr_short_circuits(self, tmp_path: Path):
        changed = _write_changed_files(tmp_path, ["entrypoint.sh"])
        comment_out = tmp_path / "comment.md"
        violation, expected = ptc.run(
            "chore: release 1.2.3", "main", str(changed), str(comment_out)
        )
        assert (violation, expected) == (False, "")
        assert not comment_out.exists()

    def test_no_changed_files(self, tmp_path: Path):
        changed = _write_changed_files(tmp_path, [])
        comment_out = tmp_path / "comment.md"
        violation, expected = ptc.run(
            "fix: whatever", "feature/foo", str(changed), str(comment_out)
        )
        assert (violation, expected) == (False, "")
        assert not comment_out.exists()

    def test_entrypoint_with_chore_title_is_a_violation(self, tmp_path: Path):
        changed = _write_changed_files(tmp_path, ["entrypoint.sh"])
        comment_out = tmp_path / "comment.md"
        violation, expected = ptc.run(
            "chore: tweak entrypoint", "feature/foo", str(changed), str(comment_out)
        )
        assert (violation, expected) == (True, "docker-img")
        assert "feat`/`fix` title" in comment_out.read_text()

    def test_conformance_dependency_bump_accepts_chore_deps(self, tmp_path: Path):
        changed = _write_changed_files(
            tmp_path,
            ["packages/conformance/pyproject.toml", "packages/conformance/uv.lock"],
        )
        comment_out = tmp_path / "comment.md"
        violation, expected = ptc.run(
            "chore(deps): update dependency ruff to v0.16.7",
            "renovate/non-critical-python-dependencies",
            str(changed),
            str(comment_out),
        )
        assert (violation, expected) == (False, "")
        assert not comment_out.exists()

    def test_conformance_dependency_bump_rejects_feat(self, tmp_path: Path):
        changed = _write_changed_files(
            tmp_path,
            ["packages/conformance/pyproject.toml", "packages/conformance/uv.lock"],
        )
        comment_out = tmp_path / "comment.md"
        violation, expected = ptc.run(
            "feat(conformance): update dependency ruff",
            "renovate/non-critical-python-dependencies",
            str(changed),
            str(comment_out),
        )
        assert (violation, expected) == (True, "deps")
        assert "`chore`/`ci` title" in comment_out.read_text()

    def test_entrypoint_with_fix_title_passes(self, tmp_path: Path):
        changed = _write_changed_files(tmp_path, ["entrypoint.sh"])
        comment_out = tmp_path / "comment.md"
        violation, expected = ptc.run(
            "fix: handle SIGTERM gracefully",
            "feature/foo",
            str(changed),
            str(comment_out),
        )
        assert (violation, expected) == (False, "")
        assert not comment_out.exists()


class TestMain(object):
    def test_writes_github_output_and_comment(self, tmp_path: Path, monkeypatch):
        changed = _write_changed_files(tmp_path, ["entrypoint.sh"])
        comment_out = tmp_path / "comment.md"
        github_output = tmp_path / "github_output.txt"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pr_title_convention.py",
                "--pr-title",
                "chore: tweak entrypoint",
                "--head-ref",
                "feature/foo",
                "--changed-files",
                str(changed),
                "--comment-out",
                str(comment_out),
            ],
        )
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

        assert ptc.main() == 0

        output = github_output.read_text()
        assert "violation=true" in output
        assert "expected=docker-img" in output
        assert "error_message=" in output
        assert comment_out.exists()

    def test_passing_title_reports_no_violation(self, tmp_path: Path, monkeypatch):
        changed = _write_changed_files(tmp_path, ["docs/foo.md"])
        comment_out = tmp_path / "comment.md"
        github_output = tmp_path / "github_output.txt"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pr_title_convention.py",
                "--pr-title",
                "chore: update docs",
                "--head-ref",
                "feature/foo",
                "--changed-files",
                str(changed),
                "--comment-out",
                str(comment_out),
            ],
        )
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

        assert ptc.main() == 0

        output = github_output.read_text()
        assert "violation=false" in output
        assert "expected=" in output
        assert not comment_out.exists()
