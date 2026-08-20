"""Tests for the symbol-discovery and alias-resolution paths in
.claude/skills/capability-manifest/references/extractor.py.

Two things are pinned here, both otherwise checked only by eyeballing the
regenerated docs/agents/sdk-capabilities.md:

- The external-alias runtime-introspection fallback. Griffe only loads the
  application_sdk package, so it can never resolve an alias pointing outside it
  (e.g. a re-exported temporalio class) -- extractor.py falls back to
  importlib/inspect for those.
- Which modules the manifest indexes. Every public module declaring __all__ is
  discovered by walking the tree, submodules included; a hand-maintained list of
  subpackages is what made `run_in_thread` invisible in the manifest (FND-439).

extractor.py is a standalone script, not a package member, so it is loaded by
file path (same pattern used for .github/scripts/*.py in
.github/scripts/tests/).
"""

import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

_EXTRACTOR_DIR = (
    Path(__file__).resolve().parents[3]
    / ".claude"
    / "skills"
    / "capability-manifest"
    / "references"
)

sys.path.insert(0, str(_EXTRACTOR_DIR))

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


class _SampleClassWithNoCustomInit:
    """A sample class that doesn't define its own __init__ at all."""


class _SampleClassWithGenericFirstArg:
    """A sample class whose first constructor arg has a comma inside its annotation."""

    def __init__(
        self,
        config: Dict[str, Any],
        timeout_seconds: float = 1.0,
        retries: int = 3,
        label: str = "default",
        extra_flag: bool = False,
    ) -> None:
        self.config = config


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
        kind: str = "ATTRIBUTE",
    ) -> None:
        self.is_alias = is_alias
        self._target_path = target_path
        self._raise_on_access = raise_on_access
        # Only consulted by the griffe-based fallback path (_resolve_actual_obj /
        # _classify_symbol) when an alias's target turns out to be unimportable.
        self.kind = kind

    @property
    def target_path(self) -> str | None:
        if self._raise_on_access:
            raise RuntimeError("simulated AliasResolutionError")
        return self._target_path


class _FakeGriffeModule:
    """A node in griffe's member graph, for walking it without loading the SDK."""

    def __init__(self, members: Dict[str, Any]) -> None:
        self.members = members


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
#
# Takes a real inspect.Signature (not a pre-rendered string) so the first
# parameter can be pulled out structurally instead of by splitting on the
# first comma -- a generic annotation like Dict[str, Any] has a comma inside
# it, which a naive string split cuts in half (see the two regression tests
# in the _external_alias_symbol section below for that failure mode fixed).
# ---------------------------------------------------------------------------


def _sig_of(fn: object) -> inspect.Signature:
    return inspect.signature(fn)  # type: ignore[arg-type]


def test_truncate_signature_returns_short_signature_unchanged() -> None:
    def foo(x: int) -> None: ...

    full = "def foo(x: int) -> int"
    assert extractor._truncate_signature(full, "def foo", _sig_of(foo)) == full


def test_truncate_signature_ignores_sig_when_full_is_already_short() -> None:
    # The short-circuit only looks at `full`'s length/content -- a mismatched or
    # missing `sig` must not matter when no truncation is needed.
    full = "def foo(x: int) -> int"
    assert extractor._truncate_signature(full, "def foo", None) == full


def test_truncate_signature_truncates_long_signature_to_first_arg() -> None:
    def fn(a: int, b: str, c: float) -> None: ...

    sig = _sig_of(fn)
    full = "prefix" + str(sig) + " # " + "x" * 100  # force well over 120 chars
    assert len(full) > 120
    assert extractor._truncate_signature(full, "prefix", sig) == "prefix(a: int, ...)"


def test_truncate_signature_preserves_comma_bearing_generic_first_arg() -> None:
    """Regression: a naive split(",")[0] on the rendered string would cut
    `Dict[str, Any]` in half, producing an unbalanced-bracket signature."""

    def fn(config: Dict[str, Any], b: str, c: float, d: int) -> None: ...

    sig = _sig_of(fn)
    full = "prefix" + str(sig) + " # " + "x" * 100  # force well over 120 chars
    assert len(full) > 120
    assert (
        extractor._truncate_signature(full, "prefix", sig)
        == "prefix(config: Dict[str, Any], ...)"
    )


def test_truncate_signature_truncates_on_embedded_memory_address_even_if_short() -> (
    None
):
    sentinel = object()  # repr is "<object object at 0x...>" -- a real live address

    def fn(count: int, obj: object = sentinel) -> None: ...

    sig = _sig_of(fn)
    full = "prefix" + str(sig)
    assert extractor._MEMORY_ADDRESS_RE.search(full)
    assert (
        extractor._truncate_signature(full, "prefix", sig) == "prefix(count: int, ...)"
    )


def test_truncate_signature_falls_back_to_ellipsis_when_first_arg_has_address() -> None:
    sentinel = object()

    def fn(obj: object = sentinel) -> None: ...

    sig = _sig_of(fn)
    full = "prefix" + str(sig)
    assert extractor._truncate_signature(full, "prefix", sig) == "prefix(...)"


def test_truncate_signature_falls_back_to_ellipsis_when_no_args() -> None:
    def fn() -> None: ...

    sig = _sig_of(fn)
    full = "prefix() # " + "x" * 150  # force well over the 120-char threshold
    assert len(full) > 120
    assert extractor._truncate_signature(full, "prefix", sig) == "prefix(...)"


def test_truncate_signature_falls_back_to_ellipsis_when_sig_is_none() -> None:
    full = "prefix() # " + "x" * 150
    assert extractor._truncate_signature(full, "prefix", None) == "prefix(...)"


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


def test_external_alias_symbol_class_with_no_custom_init() -> None:
    """Regression: manually stripping a hardcoded "(self, "/"(self)" prefix off
    obj.__init__'s signature mis-rendered any class with no custom __init__ (it
    inherits object.__init__'s "(self, /, *args, **kwargs)" slot-wrapper signature,
    which doesn't match either hardcoded prefix) as "(/, *args, **kwargs)". Calling
    inspect.signature() on the class itself avoids the issue entirely."""
    path = _dotted(_SampleClassWithNoCustomInit)
    result = extractor._external_alias_symbol("AliasedNoInit", path)
    assert result is not None
    assert result["kind"] == extractor.KIND_CLASS
    assert result["signature"] == "class AliasedNoInit()"


def test_external_alias_symbol_class_with_generic_first_arg() -> None:
    """Regression: a first arg annotated Dict[str, Any] has a comma inside the
    annotation itself -- naive string-splitting on the first comma used to render
    this as the unbalanced "AliasedGeneric(config: Dict[str, ...)"."""
    path = _dotted(_SampleClassWithGenericFirstArg)
    result = extractor._external_alias_symbol("AliasedGeneric", path)
    assert result is not None
    assert result["kind"] == extractor.KIND_CLASS
    assert result["signature"] == "class AliasedGeneric(config: Dict[str, Any], ...)"


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


# ---------------------------------------------------------------------------
# _symbol_for -- the per-symbol branch cmd_dump() delegates to. Exercises the
# two branches this PR adds: taking the external-alias fast path, and falling
# back to the pre-existing griffe-based path when the alias target can't be
# imported (e.g. an optional dependency isn't installed).
# ---------------------------------------------------------------------------


def test_symbol_for_uses_external_alias_fast_path_when_importable() -> None:
    path = _dotted(_sample_function)
    griffe_obj = _FakeGriffeAlias(is_alias=True, target_path=path)
    assert extractor._symbol_for(
        "aliased_fn", griffe_obj
    ) == extractor._external_alias_symbol("aliased_fn", path)


def test_symbol_for_falls_back_to_griffe_path_when_alias_unimportable() -> None:
    # is_alias=True + an unimportable target_path takes _external_alias_symbol's
    # None branch, which must fall through to the griffe-based path (kind=
    # "ATTRIBUTE" here) rather than raise or silently drop the symbol.
    griffe_obj = _FakeGriffeAlias(
        is_alias=True, target_path="nonexistent_pkg_xyz.Something", kind="ATTRIBUTE"
    )
    result = extractor._symbol_for("ghost_symbol", griffe_obj)
    assert result == {
        "name": "ghost_symbol",
        "kind": extractor.KIND_CONSTANT,
        "signature": "ghost_symbol",
        "summary": "_(no docstring)_",
        "filepath": "",
    }


def test_symbol_for_uses_griffe_path_directly_when_not_an_alias() -> None:
    griffe_obj = _FakeGriffeAlias(is_alias=False, kind="ATTRIBUTE")
    result = extractor._symbol_for("plain_symbol", griffe_obj)
    assert result["name"] == "plain_symbol"
    assert result["kind"] == extractor.KIND_CONSTANT


# ---------------------------------------------------------------------------
# Public-module discovery (FND-439). A hand-maintained list of subpackages read
# only package __init__ files, so anything a *submodule* declared in __all__ was
# absent from the manifest -- which is how
# `from application_sdk.execution.heartbeat import run_in_thread` came to be
# undiscoverable there while resolving perfectly at runtime.
# ---------------------------------------------------------------------------


def test_discover_public_modules_finds_submodule_all() -> None:
    found = {dotted for _, dotted, _ in extractor.discover_public_modules()}
    assert "application_sdk.execution.heartbeat" in found
    assert "application_sdk.execution" in found


def test_discover_public_modules_reports_the_offload_seam() -> None:
    """The specific regression: run_in_thread is named in a submodule's __all__ and in
    no package __init__ at all, its implementation having moved to the private
    _runtime substrate under ADR-0019."""
    names = {
        dotted: all_names
        for _, dotted, all_names in extractor.discover_public_modules()
    }
    assert "run_in_thread" in names["application_sdk.execution.heartbeat"]
    assert "run_in_thread" not in names["application_sdk.execution"]


def test_discover_public_modules_skips_private_modules() -> None:
    found = {dotted for _, dotted, _ in extractor.discover_public_modules()}
    # The seam's real home is deliberately unpublished: its public path is the
    # re-export from execution/heartbeat.py.
    assert "application_sdk._runtime.offload" not in found
    # Nor any module under a private parent (execution._temporal declares __all__).
    assert not any("._" in dotted for dotted in found)


def test_discover_public_modules_orders_package_before_its_submodules() -> None:
    """The caller treats the first module to export a name as the canonical import
    path, so a package __init__ has to be visited before anything beneath it."""
    execution = [
        dotted
        for subpkg, dotted, _ in extractor.discover_public_modules()
        if subpkg == "execution"
    ]
    assert execution[0] == "application_sdk.execution"
    assert len(execution) > 1


def test_discover_public_modules_groups_every_module_under_its_subpackage() -> None:
    for subpkg, dotted, _ in extractor.discover_public_modules():
        assert dotted == f"application_sdk.{subpkg}" or dotted.startswith(
            f"application_sdk.{subpkg}."
        )


def test_discover_public_modules_ignores_modules_without_all() -> None:
    found = {dotted for _, dotted, _ in extractor.discover_public_modules()}
    # transformers/ declares no __all__ anywhere: declaring one is how a module
    # opts into the manifest.
    assert not any(d.startswith("application_sdk.transformers") for d in found)


# ---------------------------------------------------------------------------
# _rendered_as_contract
# ---------------------------------------------------------------------------

_CONTRACTS: Dict[str, Any] = {
    "application_sdk.templates.contracts": [{"name": "UploadInput"}],
}


def test_rendered_as_contract_matches_the_namespace_itself() -> None:
    assert extractor._rendered_as_contract(
        "application_sdk.templates.contracts", "UploadInput", _CONTRACTS
    )


def test_rendered_as_contract_matches_a_descendant_module() -> None:
    """A submodule re-exporting a contract that its parent namespace already renders
    with full field detail must not be emitted again as a bare class signature."""
    assert extractor._rendered_as_contract(
        "application_sdk.templates.contracts.incremental_sql",
        "UploadInput",
        _CONTRACTS,
    )


def test_rendered_as_contract_false_for_a_non_contract_name() -> None:
    """contracts/compat.py exports functions, which the Contracts section -- Pydantic
    models only -- never renders. Skipping whole modules would have dropped them."""
    assert not extractor._rendered_as_contract(
        "application_sdk.contracts.compat", "assert_backwards_compatible", _CONTRACTS
    )


def test_rendered_as_contract_false_for_an_unrelated_module() -> None:
    assert not extractor._rendered_as_contract(
        "application_sdk.execution.heartbeat", "UploadInput", _CONTRACTS
    )


def test_rendered_as_contract_does_not_prefix_match_a_sibling_module() -> None:
    assert not extractor._rendered_as_contract(
        "application_sdk.templates.contracts_extra", "UploadInput", _CONTRACTS
    )


# ---------------------------------------------------------------------------
# _navigate_module
# ---------------------------------------------------------------------------


def test_navigate_module_walks_to_a_nested_module() -> None:
    leaf = _FakeGriffeModule({})
    tree = _FakeGriffeModule({"execution": _FakeGriffeModule({"heartbeat": leaf})})
    assert (
        extractor._navigate_module(tree, "application_sdk.execution.heartbeat") is leaf
    )


def test_navigate_module_returns_none_for_a_missing_module() -> None:
    """None is the signal that an ancestor directory has no __init__.py, making the
    module an implicit namespace package that griffe never descends into. cmd_dump
    treats it as fatal rather than skipping the module silently."""
    tree = _FakeGriffeModule({"execution": _FakeGriffeModule({})})
    assert (
        extractor._navigate_module(tree, "application_sdk.execution.heartbeat") is None
    )
    assert extractor._navigate_module(tree, "application_sdk.server.mcp") is None


# ---------------------------------------------------------------------------
# Source encoding
# ---------------------------------------------------------------------------


def test_extract_all_from_init_reads_utf8_regardless_of_ambient_encoding(
    tmp_path: Path,
) -> None:
    """SDK source is UTF-8; reading it through the *ambient* encoding fails wherever
    that isn't UTF-8. On Windows it is cp1252, and one em dash in a docstring was
    enough: `UnicodeDecodeError: 'charmap' codec can't decode byte 0x90`.

    Run in a subprocess under an ASCII locale, since the ambient encoding is read by
    CPython's C-level io machinery and cannot be monkeypatched from inside the test.
    That reproduces the Windows failure on Linux and macOS too, so this pins the fix
    on every platform rather than only on the one that happened to expose it.
    """
    module = tmp_path / "sample_module.py"
    module.write_text(
        '"""A docstring — with an em dash."""\n__all__ = ["thing"]\nthing = 1\n',
        encoding="utf-8",
    )
    probe = (
        "import sys;"
        f"sys.path.insert(0, {str(_EXTRACTOR_DIR)!r});"
        "import extractor;"
        f"print(extractor.extract_all_from_init(__import__('pathlib').Path({str(module)!r})))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "LC_ALL": "C",
            "PYTHONUTF8": "0",
            "PYTHONCOERCECLOCALE": "0",
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['thing']"
