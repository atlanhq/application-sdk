"""The shared deprecation mechanism (``application_sdk._deprecation``) and its reach."""

from __future__ import annotations

import ast
import types
import warnings
from pathlib import Path

import pytest

from application_sdk import _deprecation, _env
from application_sdk._deprecation import (
    deprecated_module_getattr,
    deprecation_message,
    lazy_resolver,
    warn_deprecated,
)

_SDK_ROOT = Path(__file__).resolve().parents[2] / "application_sdk"


def _module_with(getattr_fn: object) -> types.ModuleType:
    module = types.ModuleType("example_mod")
    module.__getattr__ = getattr_fn  # type: ignore[attr-defined]  # PEP 562 hook set on a synthetic module
    return module


class TestMessage:
    def test_names_the_replacement_and_a_full_removal_version(self) -> None:
        assert deprecation_message("OLD", "pkg.NEW", "4.0.0", note="moved.") == (
            "OLD is deprecated; use pkg.NEW instead — moved. Will be removed in v4.0.0."
        )

    def test_without_a_note(self) -> None:
        assert deprecation_message("OLD", "NEW", "4.0.0") == (
            "OLD is deprecated; use NEW instead. Will be removed in v4.0.0."
        )

    @pytest.mark.parametrize("version", ["4.0", "4", "v4.0.0", "4.0.0.1", ""])
    def test_rejects_a_version_that_is_not_major_minor_patch(
        self, version: str
    ) -> None:
        # "4.0" and "4.0.0" name one release; allowing both made the SDK use both.
        with pytest.raises(ValueError, match="major.minor.patch"):
            deprecation_message("OLD", "NEW", version)


class TestWarnDeprecated:
    def test_warns_at_the_line_that_called_the_deprecated_function(self) -> None:
        def deprecated_api() -> None:
            warn_deprecated("deprecated_api", "new_api", "4.0.0")

        with pytest.warns(DeprecationWarning) as record:
            deprecated_api()

        assert str(record[0].message) == (
            "deprecated_api is deprecated; use new_api instead. "
            "Will be removed in v4.0.0."
        )
        assert record[0].filename == __file__

    def test_stacklevel_counts_like_warnings_warn(self) -> None:
        def inner() -> None:
            warn_deprecated("x", "y", "4.0.0", stacklevel=1)

        with pytest.warns(DeprecationWarning) as record:
            inner()
        # stacklevel=1 is the line inside ``inner`` that called the helper.
        assert record[0].lineno == inner.__code__.co_firstlineno + 1


class TestModuleGetattr:
    def test_warns_at_the_accessing_line_and_returns_the_resolved_value(self) -> None:
        module = _module_with(
            deprecated_module_getattr(
                "example_mod",
                {"OLD": ("example.NEW", "renamed")},
                remove_in="4.0.0",
                resolve=lambda name: f"value of {name}",
            )
        )

        with pytest.warns(DeprecationWarning) as record:
            value = module.OLD

        assert value == "value of OLD"
        assert str(record[0].message) == (
            "OLD is deprecated; use example.NEW instead — renamed. "
            "Will be removed in v4.0.0."
        )
        assert record[0].filename == __file__

    def test_default_resolve_imports_the_replacement_path(self) -> None:
        module = _module_with(
            deprecated_module_getattr(
                "example_mod",
                {"DEPLOY": ("application_sdk.constants.DEPLOYMENT_NAME", "moved")},
                remove_in="4.0.0",
            )
        )
        from application_sdk.constants import DEPLOYMENT_NAME

        with pytest.warns(DeprecationWarning):
            assert module.DEPLOY == DEPLOYMENT_NAME

    def test_default_resolve_follows_a_class_attribute(self) -> None:
        module = _module_with(
            deprecated_module_getattr(
                "example_mod",
                {"VALUE": ("pathlib.PurePath.as_posix", "moved")},
                remove_in="4.0.0",
            )
        )
        with pytest.warns(DeprecationWarning):
            assert module.VALUE is __import__("pathlib").PurePath.as_posix

    def test_a_string_entry_is_the_notice_qualified_by_the_module(self) -> None:
        module = _module_with(
            deprecated_module_getattr(
                "example_mod",
                {"FLAG": "FLAG is deprecated and has no effect; remove it."},
                remove_in="4.0.0",
                resolve=lambda name: False,
            )
        )
        with pytest.warns(DeprecationWarning) as record:
            assert module.FLAG is False
        assert str(record[0].message) == (
            "example_mod.FLAG is deprecated and has no effect; remove it."
        )

    def test_an_unknown_name_raises_attribute_error_without_a_warning(self) -> None:
        module = _module_with(
            deprecated_module_getattr("example_mod", {}, remove_in="4.0.0")
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(AttributeError, match="'example_mod' has no attribute"):
                module.MISSING

    def test_fallback_serves_names_outside_the_table_without_warning(self) -> None:
        module = _module_with(
            deprecated_module_getattr(
                "example_mod",
                {"OLD": ("NEW", "renamed")},
                remove_in="4.0.0",
                resolve=lambda name: "old",
                fallback=lambda name: f"lazy {name}",
            )
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert module.OTHER == "lazy OTHER"

    def test_rejects_a_short_removal_version_at_definition(self) -> None:
        with pytest.raises(ValueError, match="major.minor.patch"):
            deprecated_module_getattr("example_mod", {}, remove_in="4.0")

    def test_lazy_resolver_imports_on_access(self) -> None:
        resolve = lazy_resolver({"Path": ("pathlib", "Path")})
        assert resolve("Path") is Path


def _defines_getattr(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "__getattr__"
        for node in tree.body
    )


def _has_deprecation_table(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Assign | ast.AnnAssign)
        and any(
            isinstance(target, ast.Name) and target.id == "_DEPRECATED_CONSTANTS"
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        )
        for node in tree.body
    )


def test_every_deprecation_shim_uses_the_shared_factory() -> None:
    """A hand-written ``def __getattr__`` next to ``_DEPRECATED_CONSTANTS`` is the copy
    FND-4300 removed from 40 modules. Build the shim with
    ``_deprecation.deprecated_module_getattr`` instead."""
    hand_rolled = sorted(
        str(path.relative_to(_SDK_ROOT.parent))
        for path in _SDK_ROOT.rglob("*.py")
        if _has_deprecation_table(tree := ast.parse(path.read_text(encoding="utf-8")))
        and _defines_getattr(tree)
    )
    assert hand_rolled == []


def test_the_scan_above_sees_the_shims() -> None:
    shims = [
        path
        for path in _SDK_ROOT.rglob("*.py")
        if _has_deprecation_table(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert len(shims) > 30


class TestEnvRejectionsReachTheLogger:
    def test_a_rejection_before_the_sink_is_held_and_then_logged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_env, "_pending", [])
        monkeypatch.setattr(_env, "_log_sink", None)
        monkeypatch.setenv("FND_4300_PROBE", "nope")
        with pytest.warns(RuntimeWarning, match="FND_4300_PROBE"):
            _env.env_int("FND_4300_PROBE", 3)

        logged: list[str] = []
        _env.set_log_sink(logged.append)

        assert logged == [
            "FND_4300_PROBE='nope' is not an integer; using the default 3"
        ]
        assert _env._pending == []

    def test_after_the_sink_is_set_each_rejection_is_logged_and_still_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logged: list[str] = []
        monkeypatch.setattr(_env, "_pending", [])
        monkeypatch.setattr(_env, "_log_sink", logged.append)
        monkeypatch.setenv("FND_4300_PROBE", "maybe")
        with pytest.warns(RuntimeWarning):
            assert _env.env_bool("FND_4300_PROBE", True) is True
        assert len(logged) == 1 and "FND_4300_PROBE" in logged[0]

    def test_warn_rejected_keeps_the_category_and_points_at_the_callers_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logged: list[str] = []
        monkeypatch.setattr(_env, "_log_sink", logged.append)

        def load_setting() -> None:
            _env.warn_rejected("BAD=1 is not valid", UserWarning)

        with pytest.warns(UserWarning, match="BAD=1") as record:
            load_setting()
        assert record[0].filename == __file__
        assert logged == ["BAD=1 is not valid"]

    def test_the_sdk_logger_installs_the_sink(self) -> None:
        import application_sdk.observability.logger_adaptor  # noqa: F401 — import is the act under test

        assert _env._log_sink is not None


def test_deprecated_is_the_pep_702_decorator() -> None:
    from typing_extensions import deprecated

    assert _deprecation.deprecated is deprecated
