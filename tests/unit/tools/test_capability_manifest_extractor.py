"""Tests for the external-alias runtime-introspection fallback in
.claude/skills/capability-manifest/references/extractor.py.

Griffe only loads the application_sdk package, so it can never resolve an alias
pointing outside it (e.g. a re-exported temporalio class) -- extractor.py falls
back to importlib/inspect for those. That fallback has no test coverage of its
own elsewhere: it is exercised only by eyeballing the regenerated
docs/agents/sdk-capabilities.md. These tests pin its behavior directly.

extractor.py is a standalone script, not a package member, so it is loaded by
file path (same pattern used for .github/scripts/*.py in
.github/scripts/tests/).
"""

import sys
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[3]
        / ".claude"
        / "skills"
        / "capability-manifest"
        / "references"
    ),
)

import extractor  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures used as re-export targets for _import_external_target /
# _external_alias_symbol -- referenced by their real dotted module path, the
# same way a re-exported temporalio symbol would be.
# ---------------------------------------------------------------------------


class _SampleClassWithArgs:
    """A sample class with constructor args."""

    def __init__(self, value: int, *, flag: bool = False) -> None:
        self.value = value
        self.flag = flag


class _SampleZeroArgClass:
    """A sample class with no constructor args."""

    def __init__(self) -> None:
        pass


def _sample_function(x: int, y: int = 2) -> int:
    """A sample function."""
    return x + y


def _sample_function_no_doc(x: int) -> int:
    return x


class _SampleConstant:
    """A small sentinel object."""


_SAMPLE_CONSTANT = _SampleConstant()


def _dotted(obj: object) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"  # type: ignore[attr-defined]


class _FakeGriffeAlias:
    def __init__(
        self,
        is_alias: bool,
        target_path: str | None = None,
        raise_on_access: bool = False,
    ) -> None:
        self.is_alias = is_alias
        self._target_path = target_path
        self._raise_on_access = raise_on_access

    @property
    def target_path(self) -> str | None:
        if self._raise_on_access:
            raise RuntimeError("simulated AliasResolutionError")
        return self._target_path


# ---------------------------------------------------------------------------
# _external_alias_target
# ---------------------------------------------------------------------------


def test_external_alias_target_none_when_not_alias() -> None:
    assert extractor._external_alias_target(_FakeGriffeAlias(is_alias=False)) is None


def test_external_alias_target_none_when_is_alias_attr_missing() -> None:
    class Bare:
        pass

    assert extractor._external_alias_target(Bare()) is None


def test_external_alias_target_none_for_internal_target() -> None:
    obj = _FakeGriffeAlias(
        is_alias=True, target_path="application_sdk.execution.TemporalClient"
    )
    assert extractor._external_alias_target(obj) is None


def test_external_alias_target_none_for_empty_target_path() -> None:
    assert (
        extractor._external_alias_target(
            _FakeGriffeAlias(is_alias=True, target_path="")
        )
        is None
    )


def test_external_alias_target_none_on_resolution_error() -> None:
    obj = _FakeGriffeAlias(is_alias=True, raise_on_access=True)
    assert extractor._external_alias_target(obj) is None


def test_external_alias_target_returns_path_for_external_alias() -> None:
    obj = _FakeGriffeAlias(is_alias=True, target_path="temporalio.client.Client")
    assert extractor._external_alias_target(obj) == "temporalio.client.Client"


# ---------------------------------------------------------------------------
# _import_external_target
# ---------------------------------------------------------------------------


def test_import_external_target_resolves_class() -> None:
    assert (
        extractor._import_external_target(_dotted(_SampleClassWithArgs))
        is _SampleClassWithArgs
    )


def test_import_external_target_resolves_function() -> None:
    assert (
        extractor._import_external_target(_dotted(_sample_function)) is _sample_function
    )


def test_import_external_target_none_for_unimportable_module() -> None:
    assert extractor._import_external_target("nonexistent_pkg_xyz.Something") is None


def test_import_external_target_none_for_missing_attribute() -> None:
    assert extractor._import_external_target(f"{__name__}.NoSuchAttribute") is None


# ---------------------------------------------------------------------------
# _truncate_signature
# ---------------------------------------------------------------------------


def test_truncate_signature_returns_short_signature_unchanged() -> None:
    full = "def foo(x: int) -> int"
    assert extractor._truncate_signature(full, "def foo", "(x: int)") == full


def test_truncate_signature_truncates_long_signature_to_first_arg() -> None:
    args = "(a: int, b: str, c: float)"
    full = "prefix" + args + " # " + "x" * 100  # force well over the 120-char threshold
    assert len(full) > 120
    assert extractor._truncate_signature(full, "prefix", args) == "prefix(a: int, ...)"


def test_truncate_signature_truncates_on_embedded_memory_address_even_if_short() -> (
    None
):
    args = "(count: int, obj=<Baz object at 0x7f0000000000>)"
    full = "prefix" + args
    assert len(full) <= 120
    assert (
        extractor._truncate_signature(full, "prefix", args) == "prefix(count: int, ...)"
    )


def test_truncate_signature_falls_back_to_ellipsis_when_first_arg_has_address() -> None:
    args = "(obj=<Baz object at 0x7f0000000000>)"
    full = "prefix" + args
    assert extractor._truncate_signature(full, "prefix", args) == "prefix(...)"


def test_truncate_signature_falls_back_to_ellipsis_when_no_args() -> None:
    full = "prefix() # " + "x" * 150  # force well over the 120-char threshold
    assert len(full) > 120
    assert extractor._truncate_signature(full, "prefix", "()") == "prefix(...)"


# ---------------------------------------------------------------------------
# _external_alias_symbol
# ---------------------------------------------------------------------------


def test_external_alias_symbol_class_with_args() -> None:
    path = _dotted(_SampleClassWithArgs)
    result = extractor._external_alias_symbol("AliasedClassName", path)
    assert result == {
        "name": "AliasedClassName",
        "kind": extractor.KIND_CLASS,
        "signature": "class AliasedClassName(value: int, *, flag: bool = False)",
        "summary": f"A sample class with constructor args. _(re-exported from `{path}`)_",
        "filepath": "",
    }


def test_external_alias_symbol_zero_arg_class() -> None:
    path = _dotted(_SampleZeroArgClass)
    result = extractor._external_alias_symbol("AliasedZeroArg", path)
    assert result is not None
    assert result["kind"] == extractor.KIND_CLASS
    assert result["signature"] == "class AliasedZeroArg()"


def test_external_alias_symbol_function() -> None:
    path = _dotted(_sample_function)
    result = extractor._external_alias_symbol("aliased_fn", path)
    assert result == {
        "name": "aliased_fn",
        "kind": extractor.KIND_FUNCTION,
        "signature": "aliased_fn(x: int, y: int = 2) -> int",
        "summary": f"A sample function. _(re-exported from `{path}`)_",
        "filepath": "",
    }


def test_external_alias_symbol_missing_docstring() -> None:
    path = _dotted(_sample_function_no_doc)
    result = extractor._external_alias_symbol("aliased_no_doc", path)
    assert result is not None
    assert result["summary"] == f"_(re-exported from `{path}`, no docstring)_"


def test_external_alias_symbol_constant() -> None:
    path = f"{__name__}._SAMPLE_CONSTANT"
    result = extractor._external_alias_symbol("AliasedConstant", path)
    assert result == {
        "name": "AliasedConstant",
        "kind": extractor.KIND_CONSTANT,
        "signature": "AliasedConstant",
        "summary": f"A small sentinel object. _(re-exported from `{path}`)_",
        "filepath": "",
    }


def test_external_alias_symbol_none_when_target_unimportable() -> None:
    assert (
        extractor._external_alias_symbol("Ghost", "nonexistent_pkg_xyz.Something")
        is None
    )
