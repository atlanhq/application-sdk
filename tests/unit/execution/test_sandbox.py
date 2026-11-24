"""Tests for the workflow sandbox's passthrough configuration."""

from __future__ import annotations

import pkgutil
import sys

import application_sdk
from application_sdk.execution.sandbox import (
    _FRAMEWORK_MODULES,
    _SDK_PASSTHROUGH_PACKAGES,
    _SDK_SANDBOXED_PACKAGES,
    SandboxConfig,
    host_library_modules,
)


def _passthrough() -> set[str]:
    return SandboxConfig().to_temporal_restrictions().passthrough_modules


class TestFrameworkModules:
    def test_no_package_root_entry(self) -> None:
        """Temporal matches by prefix: a root entry would pass through all of it."""
        assert "application_sdk" not in _FRAMEWORK_MODULES
        assert "application_sdk" not in _passthrough()

    def test_templates_are_sandboxed(self) -> None:
        """Template run() bodies are SDK workflow code; the sandbox checks them."""
        passthrough = _passthrough()
        assert "application_sdk.templates" not in passthrough
        assert not any(m.startswith("application_sdk.templates") for m in passthrough)

    def test_every_sdk_top_level_module_is_classified(self) -> None:
        """A new top-level SDK module must be put in exactly one set.

        Unlisted, it would be re-imported in the sandbox for every run: a
        second copy of any registry, per-run state or error class it holds.
        """
        actual = {m.name for m in pkgutil.iter_modules(application_sdk.__path__)}
        classified = _SDK_PASSTHROUGH_PACKAGES | _SDK_SANDBOXED_PACKAGES
        assert not (_SDK_PASSTHROUGH_PACKAGES & _SDK_SANDBOXED_PACKAGES)
        assert actual - classified == set(), "classify these in sandbox.py"
        assert classified - actual == set(), "these no longer exist; remove them"

    def test_every_listed_module_exists(self) -> None:
        """Passthrough entries naming nothing are dead configuration."""
        for name in _FRAMEWORK_MODULES:
            if not name.startswith("application_sdk."):
                continue
            leaf = name.removeprefix("application_sdk.")
            assert leaf in _SDK_PASSTHROUGH_PACKAGES, name


class TestHostLibraryModules:
    def test_returns_top_level_names_of_imported_modules(self) -> None:
        libs = host_library_modules(())
        assert "pytest" in libs
        assert "pydantic" in libs
        assert all("." not in name for name in libs)

    def test_excludes_the_sandboxed_packages(self) -> None:
        assert "tests" in {name.partition(".")[0] for name in sys.modules}
        assert "tests" not in host_library_modules({"tests"})

    def test_never_returns_application_sdk_or_main(self) -> None:
        libs = host_library_modules(())
        assert "application_sdk" not in libs
        assert "__main__" not in libs


class TestOsPath:
    """One os.path rule on every Python version: string-only members allowed.

    temporalio refuses all of os.path below 3.14 and allows all of it from
    3.14, so an app passing on a 3.14 laptop would fail its workflow tasks on
    the 3.13 production image.
    """

    @staticmethod
    def _call_in_sandbox(tmp_path, monkeypatch, body: str) -> object:
        import pytest
        from temporalio import workflow
        from temporalio.worker.workflow_sandbox._importer import Importer
        from temporalio.worker.workflow_sandbox._restrictions import (
            RestrictionContext,
        )

        pkg = tmp_path / "sandbox_os_probe"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(f"import os\n\ndef probe():\n    {body}\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        context = RestrictionContext()
        importer = Importer(
            SandboxConfig().to_temporal_restrictions(),
            context,
            workflow_module_name="sandbox_os_probe",
        )
        namespace: dict[str, object] = {}
        workflow.unsafe._set_in_sandbox(True)
        try:
            with importer.applied():
                exec("import sandbox_os_probe", namespace, namespace)
                context.is_runtime = True
                return namespace["sandbox_os_probe"].probe()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — returned for the assertion
            return exc
        finally:
            workflow.unsafe._set_in_sandbox(False)
            pytest.MonkeyPatch().delitem(sys.modules, "sandbox_os_probe", raising=False)

    def test_os_path_join_runs_in_workflow_code(self, tmp_path, monkeypatch) -> None:
        result = self._call_in_sandbox(
            tmp_path, monkeypatch, 'return os.path.join("a", "b")'
        )
        assert result == "a/b" or result == "a\\b", result

    def test_os_path_filesystem_probe_is_refused(self, tmp_path, monkeypatch) -> None:
        """Only string-only os.path members are allowed: exists() reads the disk."""
        from temporalio.worker.workflow_sandbox import RestrictedWorkflowAccessError

        result = self._call_in_sandbox(
            tmp_path, monkeypatch, 'return os.path.exists("/")'
        )
        assert isinstance(result, RestrictedWorkflowAccessError), result

    def test_rest_of_os_is_still_refused(self, tmp_path, monkeypatch) -> None:
        from temporalio.worker.workflow_sandbox import RestrictedWorkflowAccessError

        result = self._call_in_sandbox(
            tmp_path, monkeypatch, 'return os.getenv("HOME")'
        )
        assert isinstance(result, RestrictedWorkflowAccessError), result
