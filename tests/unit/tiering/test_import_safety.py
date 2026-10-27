"""Workflow code must be able to ask for a tier without loading the world.

These run in subprocesses on purpose: once a module is in ``sys.modules`` every
later import is free, so an in-process assertion about import cost measures
whatever the test session happened to import first.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from application_sdk.tiering import _OWNER
from application_sdk.tiering import __all__ as EXPORTS

#: Loading any of these means the package root stopped being cheap.
_HEAVY = ("pydantic", "httpx", "temporalio")


def _modules_after(statement: str) -> set[str]:
    """The modules a fresh interpreter loads to run ``statement``."""
    code = (
        "import sys, json\n"
        "before = set(sys.modules)\n"
        f"{statement}\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    import json

    return set(json.loads(done.stdout))


class TestTheLadderStaysCheapToImport:
    """The reason the package re-exports lazily rather than eagerly."""

    def test_importing_the_ladder_does_not_load_the_activity_side(self) -> None:
        """`queues` takes a module-level logger, which costs ~800 modules.

        An eager re-export in `__init__` would charge that to every workflow
        that imports a ladder.
        """
        loaded = _modules_after("from application_sdk.tiering import Ladder")
        assert "application_sdk.tiering.queues" not in loaded

    @pytest.mark.parametrize("heavy", _HEAVY)
    def test_importing_the_ladder_does_not_load_the_heavy_stack(
        self, heavy: str
    ) -> None:
        assert heavy not in _modules_after("from application_sdk.tiering import Ladder")

    def test_the_ladder_import_is_an_order_of_magnitude_cheaper(self) -> None:
        """A bound, not a target — it fails loudly if the lazy init regresses."""
        assert len(_modules_after("from application_sdk.tiering import Ladder")) < 200

    def test_the_activity_side_still_imports(self) -> None:
        """`resolve` is allowed to be expensive; an activity has already paid it."""
        loaded = _modules_after("from application_sdk.tiering import resolve")
        assert "application_sdk.tiering.queues" in loaded

    def test_every_export_resolves(self) -> None:
        import application_sdk.tiering as pkg

        for name in EXPORTS:
            assert getattr(pkg, name) is not None

    def test_an_unknown_attribute_still_raises(self) -> None:
        import application_sdk.tiering as pkg

        with pytest.raises(AttributeError, match="has no attribute"):
            pkg.not_a_real_symbol  # noqa: B018

    def test_the_lazy_table_and_the_public_api_agree(self) -> None:
        """A name in one and not the other is an export that cannot resolve."""
        assert set(_OWNER) == set(EXPORTS)

    def test_dir_lists_the_public_api(self) -> None:
        import application_sdk.tiering as pkg

        assert dir(pkg) == sorted(EXPORTS)


class TestTheSandboxPassesThePackageThrough:
    """Workflow code importing the package must not hit sandbox restrictions."""

    def test_the_package_is_covered_by_framework_passthrough(self) -> None:
        """Temporal walks parent prefixes, and `application_sdk` is in the set.

        Asserted rather than assumed: if that root entry were ever narrowed to
        specific submodules, `application_sdk.tiering` would need its own entry
        and a workflow importing a ladder would start failing in the sandbox.
        """
        from application_sdk.execution.sandbox import _FRAMEWORK_MODULES

        name = "application_sdk.tiering.ladder"
        covered = name in _FRAMEWORK_MODULES or any(
            name.startswith(f"{module}.") for module in _FRAMEWORK_MODULES
        )
        assert covered
