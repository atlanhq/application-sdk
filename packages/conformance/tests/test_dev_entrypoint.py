"""Tests for T004 DevEntrypointRequiresAppModule (BLDX-1520).

T004 is per-file (scans only the root main.py, no cross-file resolution), so
scan_text is sufficient for all detection tests.  Separate discovery tests
confirm main.py is found at the repo root and the check no-ops when absent.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.dev_entrypoint import SERIES, discover, main, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier

# ── helpers ───────────────────────────────────────────────────────────────────


def _rule(src: str, file: str = "main.py") -> list:
    return [f for f in scan_text(src, file) if f.rule_id == "T004"]


# ── series metadata ───────────────────────────────────────────────────────────


def test_series_letter() -> None:
    assert SERIES == "T"


# ── fires on production-entrypoint call patterns ──────────────────────────────


class TestT004Fires:
    def test_fires_on_from_import_call(self) -> None:
        src = "from application_sdk.main import main\nmain()\n"
        fs = _rule(src)
        assert len(fs) == 1
        assert "application_sdk.main.main" in fs[0].message

    def test_fires_on_aliased_from_import_call(self) -> None:
        src = "from application_sdk.main import main as sdk_main\nsdk_main()\n"
        assert len(_rule(src)) == 1

    def test_fires_on_aliased_module_import_call(self) -> None:
        src = "import application_sdk.main as sdkmain\nsdkmain.main()\n"
        assert len(_rule(src)) == 1

    def test_fires_on_bare_dotted_module_call(self) -> None:
        src = "import application_sdk.main\napplication_sdk.main.main()\n"
        assert len(_rule(src)) == 1

    def test_fires_inside_dunder_main_guard(self) -> None:
        src = (
            "from application_sdk.main import main\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        assert len(_rule(src)) == 1


# ── silent on conformant / unrelated patterns ─────────────────────────────────


class TestT004Silent:
    def test_silent_on_run_dev_delegation(self) -> None:
        """The proven fix: delegate to a local dev entrypoint."""
        src = (
            "import asyncio\n"
            "from app.run_dev import main\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    asyncio.run(main())\n"
        )
        assert _rule(src) == []

    def test_silent_on_run_dev_combined_direct_call(self) -> None:
        src = (
            "import asyncio\n"
            "from application_sdk.main import run_dev_combined\n"
            "from app.my_app import MyApp\n"
            "\n"
            "async def main():\n"
            "    await run_dev_combined(MyApp)\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    asyncio.run(main())\n"
        )
        assert _rule(src) == []

    def test_silent_on_unimported_main_call(self) -> None:
        """A local main() with no application_sdk.main origin must not fire."""
        src = "def main():\n    pass\n\nmain()\n"
        assert _rule(src) == []

    def test_silent_on_unrelated_module_main(self) -> None:
        """main() from an unrelated module sharing the name must not fire."""
        src = "from myapp.cli import main\nmain()\n"
        assert _rule(src) == []

    def test_silent_on_import_without_call(self) -> None:
        """Importing 'main' without calling it is not itself the violation."""
        src = "from application_sdk.main import main\n"
        assert _rule(src) == []


# ── suppression ────────────────────────────────────────────────────────────────


class TestT004Suppression:
    def test_suppressed_inline(self) -> None:
        src = (
            "from application_sdk.main import main\n"
            "main()  # conformance: ignore[T004] utility app, no dev entrypoint\n"
        )
        fs = _rule(src)
        assert len(fs) == 1 and fs[0].suppressed
        assert fs[0].suppression_justification == "utility app, no dev entrypoint"

    def test_suppressed_comment_line_above(self) -> None:
        # The directive must appear on the comment-only line directly *above*
        # the offending call for make_finding to honour it.
        src = (
            "from application_sdk.main import main\n"
            "# conformance: ignore[T004] utility app, no dev entrypoint\n"
            "main()\n"
        )
        fs = _rule(src)
        assert any(f.suppressed for f in fs)


# ── tier and disposition ──────────────────────────────────────────────────────


class TestT004TierAndDisposition:
    def test_is_warn_tier(self) -> None:
        assert get_rule("T004").tier is EnforcementTier.WARN

    def test_warn_violation_passes_gate(self, tmp_path: Path) -> None:
        """An unsuppressed T004 (WARN) does not fail the gate (exit 0)."""
        main_py = tmp_path / "main.py"
        main_py.write_text("from application_sdk.main import main\nmain()\n")
        code = main(["--root", str(tmp_path), str(main_py)])
        assert code == 0


# ── discover() ─────────────────────────────────────────────────────────────────


def test_discover_finds_main_py_at_root(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("from application_sdk.main import main\n")
    found = discover(tmp_path)
    assert len(found) == 1
    assert found[0].name == "main.py"


def test_discover_empty_when_no_main_py(tmp_path: Path) -> None:
    assert discover(tmp_path) == []


def test_discover_ignores_nested_main_py(tmp_path: Path) -> None:
    """Only the repo-root main.py is in scope, not a same-named nested file."""
    nested = tmp_path / "app" / "main.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("from application_sdk.main import main\nmain()\n")
    assert discover(tmp_path) == []


# ── rule metadata wiring ────────────────────────────────────────────────────────


def test_t004_rule_metadata() -> None:
    rule = get_rule("T004")
    assert rule.name == "DevEntrypointRequiresAppModule"
    assert rule.category == "dev-entrypoint"
    assert rule.autofixable is False
