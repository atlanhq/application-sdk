"""B001 ``DeprecatedSdkSymbolUsage`` — flag app consumption of a deprecated symbol.

Runs against *consumer apps* (scope ``app``).  Reads the committed manifest and
flags four surfaces:

* **importing** a deprecated class/function — ``from application_sdk.x import Foo``;
* **subclassing** a deprecated base — ``class MyExtractor(BaseMetadataExtractor)``;
* **calling** a deprecated method by attribute — ``obj.upload_to_atlan(...)``;
* **reading** a deprecated enum member — ``DataframeType.daft``.

**Module-aware matching.**  A class/function match requires both the symbol name
*and* the import module to agree with a manifest entry — because a deprecated
name can collide with its own recommended replacement.  The canonical case:
``application_sdk.app.AppError`` is deprecated and its notice says "use
``application_sdk.errors.AppError``" — same bare name.  Matching on name alone
would flag the *correct* migration target on every app.  So an import is only
flagged when the manifest entry's module is the imported module or a submodule of
it (``from application_sdk.app import AppError`` → matches ``app.base``;
``from application_sdk.errors import AppError`` → does not).  Method calls remain
attribute-name-anchored (a method is not importable), an accepted false-positive
risk at WARN.  A deprecated ``property`` is never matched: it is read, not called,
so a call ``x.client(...)`` cannot be a use of ``BaseE2ETest.client`` — matching it
by name only ever hit boto3's ``session.client("s3")`` factory (FND-2711).

Enum members are module-aware too, via the enum class they hang off: the member
is only matched when its enum was imported from a module the manifest entry
agrees with, so an app's own ``DataframeType`` never picks up the SDK's
deprecated members.

The finding message carries the SDK's own migration guidance from the notice, so
the remediation loop can propose the concrete replacement.

Complement to E013 ``LegacyAtlanErrorRaise``: E013 owns the ``raise AtlanError``
site; B001 owns import / construct / subclass.  The surfaces do not overlap.

Coverage limits (intentional, documented): module-qualified *usage* without a
``from`` import (``import application_sdk.discovery as d; d.DiscoveryError(...)``)
and sibling re-export aliasing can produce false negatives — biased toward zero
false positives at WARN.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Callable, Iterator
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.extensions import SafeSwap, SafeSwapRequires
from conformance.suite.schema.findings import Finding

from ._manifest import SDK_IMPORT_ROOT, DeprecatedSymbol, Manifest

_RULE_ID = "B001"


def _hint(message: str) -> str:
    """Render the SDK's migration guidance as a trailing hint, if present."""
    return f" {message}" if message else ""


def _is_sdk_module(name: str) -> bool:
    return name == SDK_IMPORT_ROOT or name.startswith(SDK_IMPORT_ROOT + ".")


def _module_matches(import_mod: str, entry_module: str) -> bool:
    """True if a symbol defined in *entry_module* is reachable via *import_mod*.

    Exact module, or *entry_module* is a submodule of *import_mod* (covers
    re-export from a parent package).  Deliberately one-directional: importing
    from a *sibling* module (a different re-export) does not match.
    """
    return entry_module == import_mod or entry_module.startswith(import_mod + ".")


#: Class that owns every :data:`PUBLIC_RENAMES` method.  A call is only marked
#: when its receiver is proven to be this class or an instance of an app class
#: whose only base chain ends in it.
RENAME_OWNER = "SqlApp"

#: Deprecated SqlApp method -> (public method it delegates to, first SDK version with it).
PUBLIC_RENAMES: dict[str, tuple[str, str]] = {
    "_resolve_credential_ref": ("resolve_credential_ref", "3.33.2"),
    "_build_transform_input": ("build_transform_input", "3.33.2"),
}

_SKIP_DIRS = frozenset(
    {
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        "site-packages",
        "dist-packages",
    }
)


def _skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.startswith(".")


def _repo_python_files(root: Path) -> Iterator[tuple[Path, tuple[str, ...]]]:
    """Yield ``(path, parts relative to root)`` for every repo ``.py`` file.

    Skipped directories are pruned during the walk, so an in-tree virtualenv
    or ``site-packages`` is never enumerated.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
        base = Path(dirpath)
        prefix = base.relative_to(root).parts
        for filename in filenames:
            if filename.endswith(".py"):
                yield base / filename, (*prefix, filename)


def _is_test_file(parts: tuple[str, ...]) -> bool:
    name = parts[-1]
    return (
        "tests" in parts[:-1]
        or "test" in parts[:-1]
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def _bound_names(tree: ast.Module, watched: set[str]) -> set[str]:
    """Names in *watched* that *tree* binds, defines, or names as a string.

    Covers ``def``/``class`` of the name, assignment to it as a name or an
    attribute (``_resolve_credential_ref = custom`` in a class body,
    ``SqlApp._resolve_credential_ref = f``), an import bound to it, and a
    string constant equal to it (``setattr(..., "<name>", ...)``).  Any of
    these can change which body a call of the name runs.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name: str | None = node.name
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            name = node.id
        elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            name = node.attr
        elif isinstance(node, ast.alias):
            name = node.asname or node.name.rpartition(".")[2]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value
        else:
            continue
        if name in watched:
            bound.add(name)
    return bound


def unsafe_renames(root: Path) -> frozenset[str]:
    """Deprecated names in :data:`PUBLIC_RENAMES` whose call is not a safe swap in *root*.

    Unsafe when any Python file in the repo, tests included, binds the old or
    the new name (a ``def``, a class-level alias, an attribute assignment, an
    import, or a string naming it — any of which can change which body runs),
    or a test file mentions the old name (a patch of it stops reaching the
    call). The whole repo is read, so a scan of a few files gives the same
    answer as a full one.
    """
    watched = {
        name for pair in PUBLIC_RENAMES.items() for name in (pair[0], pair[1][0])
    }
    unsafe: set[str] = set()
    for path, rel in _repo_python_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        present = {name for name in watched if name in text}
        if not present:
            continue
        if _is_test_file(rel):
            unsafe |= present & PUBLIC_RENAMES.keys()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            unsafe |= present & PUBLIC_RENAMES.keys()
            continue
        bound = _bound_names(tree, present)
        unsafe |= {
            old
            for old, (new, _) in PUBLIC_RENAMES.items()
            if old in bound or new in bound
        }
    return frozenset(unsafe)


def _rename_swap(name: str, unsafe: frozenset[str]) -> SafeSwap | None:
    if name not in PUBLIC_RENAMES or name in unsafe:
        return None
    public, since = PUBLIC_RENAMES[name]
    return SafeSwap(
        id="public-rename",
        find=f".{name}(",
        replace=f".{public}(",
        requires=SafeSwapRequires(package="atlan-application-sdk", min=since),
    )


def _single_exact_match(call: ast.Call, find: str, lines: list[str]) -> bool:
    """Whether *find* occurs once on the call's line, at this call's ``.name(``.

    The swap is applied as a text replace on the finding's line, so it is only
    sound when that line holds exactly one occurrence and it is this call's.
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.end_lineno != call.lineno:
        return False
    if not 0 < call.lineno <= len(lines):
        return False
    line = lines[call.lineno - 1].encode("utf-8")
    needle = find.encode("utf-8")
    end = func.end_col_offset
    if end is None or line.count(needle) != 1:
        return False
    # ``find`` is ``.<attr>(``: it must start one byte before the attribute
    # name and end on the byte right after the attribute expression.
    return line.find(needle) == end - len(needle) + 1


def _owner_receiver_calls(
    tree: ast.Module, is_owner_ref: Callable[[ast.expr], bool]
) -> frozenset[int]:
    """``id()`` of each method call whose receiver is proven to be the owner.

    Proven receivers, and nothing else:

    * the owner class itself, or an *owner class* of this file
      (``SqlApp._build_transform_input(...)``);
    * the first parameter of a non-static method defined directly in an owner
      class's body, when the method never rebinds it (``self._x(...)``).

    An *owner class* is a class of this file with exactly one base and no
    class keywords, where that base is the owner or another owner class.  A
    second base could supply its own method of the name ahead of the owner's
    in the MRO, and a base defined in another file cannot be seen here, so
    both leave the call unproven.
    """
    classes: dict[str, list[ast.ClassDef]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.setdefault(node.name, []).append(node)
    owner_classes: dict[str, ast.ClassDef] = {}
    changed = True
    while changed:
        changed = False
        for name, defs in classes.items():
            if name in owner_classes or len(defs) != 1:
                continue
            cls = defs[0]
            if len(cls.bases) != 1 or cls.keywords:
                continue
            base = cls.bases[0]
            if is_owner_ref(base) or (
                isinstance(base, ast.Name) and base.id in owner_classes
            ):
                owner_classes[name] = cls
                changed = True

    def _is_class_ref(expr: ast.expr) -> bool:
        return is_owner_ref(expr) or (
            isinstance(expr, ast.Name) and expr.id in owner_classes
        )

    calls: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _is_class_ref(node.func.value)
        ):
            calls.add(id(node))
    for cls in owner_classes.values():
        for stmt in cls.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(
                isinstance(d, ast.Name) and d.id == "staticmethod"
                for d in stmt.decorator_list
            ):
                continue
            params = [*stmt.args.posonlyargs, *stmt.args.args]
            if not params:
                continue
            first = params[0]
            receiver = first.arg
            rebound = any(
                (
                    isinstance(n, ast.Name)
                    and n.id == receiver
                    and not isinstance(n.ctx, ast.Load)
                )
                or (isinstance(n, ast.arg) and n.arg == receiver and n is not first)
                for n in ast.walk(stmt)
            )
            if rebound:
                continue
            for n in ast.walk(stmt):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == receiver
                ):
                    calls.add(id(n))
    return frozenset(calls)


def scan_consumer(
    tree: ast.Module,
    file: str,
    manifest: Manifest,
    directives: dict[int, _IgnoreDirective],
    unsafe: frozenset[str] | None = None,
    source: str | None = None,
) -> list[Finding]:
    """Return B001 findings for *tree* against the deprecated-symbol *manifest*.

    A call of a :data:`PUBLIC_RENAMES` method is marked as a safe swap only when
    *unsafe* (from :func:`unsafe_renames`) is given and does not name it, the
    receiver is proven to be the SDK's :data:`RENAME_OWNER` (see
    :func:`_owner_receiver_calls`), and *source* is given and the call's line
    holds exactly one occurrence of the swap's ``find`` text, at this call.
    """
    if not manifest.symbols:
        return []
    class_func: dict[str, list[DeprecatedSymbol]] = {}
    method: dict[str, DeprecatedSymbol] = {}
    # Deprecated enum members, keyed by the enum they belong to:
    # ``{"DataframeType": {"daft": <record>}}``.  The manifest stores them
    # qualified (``DataframeType.daft``) because the bare member name is not
    # importable and would collide across enums.
    enum_member: dict[str, dict[str, DeprecatedSymbol]] = {}
    for record in manifest.symbols:
        # ``constant`` rides in the class/function bucket because a module
        # constant is imported by exactly the same statement and matched by
        # exactly the same module-aware rule — ``from application_sdk.x import
        # CLASSIFICATION_VERDICT`` differs from importing a class only in what
        # the name happens to be bound to (FND-2388). It is never subclassed or
        # called, so the downstream construct/subclass passes simply never hit.
        if record.kind in ("class", "function", "constant"):
            class_func.setdefault(record.symbol, []).append(record)
        elif record.kind == "method":
            method[record.symbol] = record
        elif record.kind == "enum_member" and "." in record.symbol:
            enum_name, _, member = record.symbol.rpartition(".")
            enum_member.setdefault(enum_name, {})[member] = record

    findings: list[Finding] = []
    # Local name bound (via from-import) to a deprecated symbol's record.
    deprecated_bindings: dict[str, DeprecatedSymbol] = {}
    # Local alias bound to an application_sdk *module* (for `mod.Symbol` access),
    # mapped to that module's full dotted path.
    sdk_module_aliases: dict[str, str] = {}
    # Local name bound to an enum class that has deprecated members, mapped to
    # that enum's name in the manifest.
    enum_bindings: dict[str, str] = {}
    # Local names bound (via from-import) to the SDK's RENAME_OWNER class.
    owner_bindings: set[str] = set()
    owner_modules = {method[name].module for name in PUBLIC_RENAMES if name in method}

    def _match_class_func(name: str, import_mod: str) -> DeprecatedSymbol | None:
        for record in class_func.get(name, ()):
            if _module_matches(import_mod, record.module):
                return record
        return None

    def _enum_declares_members(name: str, import_mod: str) -> bool:
        """Whether *name*, imported from *import_mod*, is a manifest enum.

        Module-aware for the same reason class/function matching is: an app's
        own ``DataframeType`` must not pick up the SDK's deprecated members.
        """
        return any(
            _module_matches(import_mod, record.module)
            for record in enum_member.get(name, {}).values()
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_sdk_module(alias.name):
                    continue
                if alias.asname:
                    sdk_module_aliases[alias.asname] = alias.name
                else:
                    # `import application_sdk.x` binds the top package name.
                    top = alias.name.split(".")[0]
                    sdk_module_aliases[top] = top
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if not _is_sdk_module(mod):
                continue
            for alias in node.names:
                if alias.name == RENAME_OWNER and any(
                    _module_matches(mod, m) for m in owner_modules
                ):
                    owner_bindings.add(alias.asname or alias.name)
                if _enum_declares_members(alias.name, mod):
                    enum_bindings[alias.asname or alias.name] = alias.name
                record = _match_class_func(alias.name, mod)
                if record is None:
                    continue
                deprecated_bindings[alias.asname or alias.name] = record
                findings.append(
                    make_finding(
                        filename=file,
                        rule_id=_RULE_ID,
                        node=node,
                        message=(
                            f"Imports deprecated SDK symbol '{alias.name}' "
                            f"({record.module}).{_hint(record.message)}"
                        ),
                        directives=directives,
                    )
                )

    def _resolve_base(base: ast.expr) -> DeprecatedSymbol | None:
        if isinstance(base, ast.Name):
            return deprecated_bindings.get(base.id)
        if (
            isinstance(base, ast.Attribute)
            and isinstance(base.value, ast.Name)
            and base.value.id in sdk_module_aliases
        ):
            return _match_class_func(base.attr, sdk_module_aliases[base.value.id])
        return None

    def _resolve_enum_member(node: ast.Attribute) -> DeprecatedSymbol | None:
        """Resolve ``Enum.member`` access to a deprecated-member record.

        Covers both spellings the import loop can bind: a directly-imported enum
        (``DataframeType.daft``) and a module-qualified one
        (``types.DataframeType.daft``).
        """
        receiver = node.value
        if isinstance(receiver, ast.Name):
            enum_name = enum_bindings.get(receiver.id)
        elif (
            isinstance(receiver, ast.Attribute)
            and isinstance(receiver.value, ast.Name)
            and receiver.value.id in sdk_module_aliases
            and _enum_declares_members(
                receiver.attr, sdk_module_aliases[receiver.value.id]
            )
        ):
            enum_name = receiver.attr
        else:
            return None
        if enum_name is None:
            return None
        return enum_member.get(enum_name, {}).get(node.attr)

    def _is_owner_ref(expr: ast.expr) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in owner_bindings
        return (
            isinstance(expr, ast.Attribute)
            and expr.attr == RENAME_OWNER
            and isinstance(expr.value, ast.Name)
            and expr.value.id in sdk_module_aliases
            and any(
                _module_matches(sdk_module_aliases[expr.value.id], m)
                for m in owner_modules
            )
        )

    owner_calls = (
        _owner_receiver_calls(tree, _is_owner_ref)
        if unsafe is not None and source is not None
        else frozenset()
    )
    lines = source.splitlines() if source is not None else []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                record = _resolve_base(base)
                if record is not None:
                    findings.append(
                        make_finding(
                            filename=file,
                            rule_id=_RULE_ID,
                            node=node,
                            message=(
                                f"Subclasses deprecated SDK symbol '{record.symbol}' "
                                f"({record.module}).{_hint(record.message)}"
                            ),
                            directives=directives,
                        )
                    )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in method
        ):
            record = method[node.func.attr]
            swap = (
                _rename_swap(node.func.attr, unsafe)
                if unsafe is not None and id(node) in owner_calls
                else None
            )
            if swap is not None and not _single_exact_match(node, swap.find, lines):
                swap = None
            findings.append(
                make_finding(
                    filename=file,
                    rule_id=_RULE_ID,
                    node=node,
                    message=(
                        f"Calls deprecated SDK method '{record.symbol}' "
                        f"({record.module}).{_hint(record.message)}"
                    ),
                    directives=directives,
                    properties=swap.to_properties() if swap else None,
                )
            )
        elif isinstance(node, ast.Attribute):
            record = _resolve_enum_member(node)
            if record is not None:
                findings.append(
                    make_finding(
                        filename=file,
                        rule_id=_RULE_ID,
                        node=node,
                        message=(
                            f"Uses deprecated SDK enum member '{record.symbol}' "
                            f"({record.module}).{_hint(record.message)}"
                        ),
                        directives=directives,
                    )
                )

    return findings
