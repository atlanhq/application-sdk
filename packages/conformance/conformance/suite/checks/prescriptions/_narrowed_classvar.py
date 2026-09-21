"""P052 NarrowedSdkClassVar — app re-narrows an inherited SDK ``ClassVar``.

``App`` declares several configuration switches as ``ClassVar``-typed
``Literal`` unions (``preflight_gate_mode``, ``artifact_validation_mode``, …).
An app subclass that redeclares one of these with its own annotation instead
of leaving it inherited can *narrow* the type — e.g. restating
``preflight_gate_mode: ClassVar[Literal["hard", "soft"]] = "hard"`` drops the
:class:`~application_sdk.handler.contracts.PreflightGateMode` enum arm the SDK
actually declares (``ClassVar["PreflightGateMode | Literal['hard', 'soft']"]``).

``ClassVar`` is invariant, so a narrowed override is an incompatible override
under the pyright ``standard`` baseline the fleet is pinned to — nothing fails
at runtime (``coerce_gate_mode`` accepts either spelling), so this is WARN, not
BLOCK. The fix is to drop the annotation entirely — ``preflight_gate_mode =
"hard"`` inherits the SDK's declared type with no redeclaration at all — or to
restate the SDK's annotation exactly (redundant, but type-compatible).

Why AST + resolve-from-installed, not a text probe
----------------------------------------------------
A bare text probe on ``ClassVar[Literal["hard", "soft"]]`` would also match:

* The SDK's **own** declaration sites in ``application_sdk/app/base.py`` — both
  ``preflight_gate_mode`` and ``artifact_validation_mode`` are declared with
  this exact shape there. :func:`_collect_app_subclasses` seeds ``"App"`` as
  the sole defining class and never adds it to the derived set, so the
  defining class's own body is never visited as a candidate override.
* An app that **restates** ``artifact_validation_mode`` with the SDK's exact
  annotation — type-compatible (identical, so invariance holds), just
  redundant. :func:`_is_strict_narrowing` only fires when the app's literal
  set (plus "has another type arm" flag) is a *strict subset* of the SDK's.
* An app that **widens** an existing narrower annotation back up to the SDK's
  full union — also an exact match, so not flagged.
* Unrelated app-local ``hard``/``soft`` fields that touch no inherited SDK
  ``ClassVar`` name at all.

The Literal member sets this rule compares against are read from the
*installed* ``application_sdk`` package at scan time
(:func:`_sdk_app_classvar_shapes`), not a list of ``{"hard", "soft"}`` copied
into this checker — a hardcoded copy would silently drift the moment the SDK's
own declaration changes. Resolution prefers the scanned repo's own
``.venv`` (mirrors ``dependency_conformance._repo_site_packages``) and falls
back to the invoking interpreter's environment so the check still degrades
gracefully with no repo venv on disk. When neither resolves, the check is
silent — grading against a value we could not confirm is a worse failure mode
than staying quiet.

Single-file, like P002/P011/P012: the app-subclass closure is built per file.
A subclass of ``App`` declared in one file and re-narrowed from a second file
that imports it is out of scope — every carrying repo in the fleet sweep
redeclares directly on the ``App`` subclass in the same file.
"""

from __future__ import annotations

import ast
import importlib.util
from functools import lru_cache
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    make_finding,
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

#: The sole defining class for the ClassVars this rule polices. Never itself a
#: candidate for a finding — see module docstring.
_DEFINING_CLASS = "App"

#: Where the defining class lives inside the installed ``application_sdk``
#: package (both site-packages and an editable/source checkout use this
#: layout).
_APP_BASE_RELPATH: tuple[str, ...] = ("application_sdk", "app", "base.py")


def _terminal_name(node: ast.expr) -> str | None:
    """Return the terminal name of a ``Name``/``Attribute`` expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _annotation_shape(node: ast.expr | None) -> tuple[frozenset[str], bool]:
    """Reduce an annotation to ``(string-literal values, has a non-Literal arm)``.

    Unwraps ``ClassVar[...]``, a quoted forward-ref string (parsed as its own
    expression), and ``X | Y`` unions — so
    ``ClassVar["PreflightGateMode | Literal['hard', 'soft']"]`` and its
    unquoted, unwrapped equivalent reduce to the same shape.  A bare
    name/attribute (``PreflightGateMode``) is an opaque, non-``Literal`` arm:
    this checker cannot enumerate its members, so it contributes no literal
    values but its presence is recorded (``has_other=True``) so an override
    that drops it is still seen as narrower.
    """
    if node is None:
        return frozenset(), True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            parsed = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return frozenset(), True
        return _annotation_shape(parsed)
    if isinstance(node, ast.Subscript):
        base = _terminal_name(node.value)
        if base == "ClassVar":
            return _annotation_shape(node.slice)
        if base == "Literal":
            elts = (
                list(node.slice.elts)
                if isinstance(node.slice, ast.Tuple)
                else [node.slice]
            )
            lits = frozenset(
                e.value
                for e in elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
            has_non_str_member = any(
                not (isinstance(e, ast.Constant) and isinstance(e.value, str))
                for e in elts
            )
            return lits, has_non_str_member
        # Any other subscripted form (Optional[...], a generic, ...) is opaque.
        return frozenset(), True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left_lits, left_other = _annotation_shape(node.left)
        right_lits, right_other = _annotation_shape(node.right)
        return left_lits | right_lits, left_other or right_other
    # A bare name/attribute (an enum, a TypeAlias, ...) or anything else this
    # checker does not recognise: opaque, contributes no literal values.
    return frozenset(), True


def _candidate_app_base_paths(root: Path) -> list[Path]:
    """``application_sdk/app/base.py`` candidates inside *root*'s own ``.venv``.

    Mirrors ``dependency_conformance._repo_site_packages``: prefer the target
    repo's own environment so the resolved type reflects the SDK version that
    repo actually has installed, not whichever interpreter happens to invoke
    the suite.
    """
    venv = root / ".venv"
    if not venv.is_dir():
        return []
    site_dirs = [
        *sorted(venv.glob("lib/python*/site-packages")),
        venv / "Lib" / "site-packages",  # Windows layout
    ]
    return [d.joinpath(*_APP_BASE_RELPATH) for d in site_dirs if d.is_dir()]


def _resolve_app_base_source(root: Path) -> str | None:
    """Read the installed ``App`` class source, or ``None`` when unresolvable.

    Resolution order: the scanned repo's own ``.venv``, then the invoking
    interpreter's environment (``importlib.util.find_spec`` — locates the file
    without executing the package, same caution as the D-series resolvers).
    """
    for candidate in _candidate_app_base_paths(root):
        if candidate.is_file():
            source = safe_read_text(candidate)
            if source is not None:
                return source
    try:
        spec = importlib.util.find_spec("application_sdk.app.base")
    except (ImportError, ValueError, ModuleNotFoundError):
        spec = None
    if spec is not None and spec.origin:
        origin = Path(spec.origin)
        if origin.is_file():
            return safe_read_text(origin)
    return None


@lru_cache(maxsize=8)
def _sdk_app_classvar_shapes(root_str: str) -> dict[str, tuple[frozenset[str], bool]]:
    """``{ClassVar name: shape}`` for every Literal-typed ClassVar on ``App``.

    Read live from the installed SDK (see module docstring) rather than a
    hardcoded name/value list, so the check tracks the SDK's declaration as it
    evolves instead of grading against a snapshot. Cached per repo root — this
    is called once per file scanned.
    """
    source = _resolve_app_base_source(Path(root_str))
    if source is None:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == _DEFINING_CLASS:
            shapes: dict[str, tuple[frozenset[str], bool]] = {}
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                if not isinstance(stmt.target, ast.Name):
                    continue
                lits, has_other = _annotation_shape(stmt.annotation)
                if lits:  # only ClassVars whose type includes a Literal arm
                    shapes[stmt.target.id] = (lits, has_other)
            return shapes
    return {}


def _collect_app_subclasses(tree: ast.Module) -> frozenset[str]:
    """Collect class names that transitively subclass ``App`` within *tree*.

    Same fixed-point closure as ``_category_override._collect_apperror_subclasses``:
    a second-generation subclass is caught even when the intermediate class is
    not itself in scope. The defining class name is never added — it is
    checked for membership, not derived from itself.
    """
    derived: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name == _DEFINING_CLASS or node.name in derived:
                continue
            for base in node.bases:
                name = _terminal_name(base)
                if name and (name == _DEFINING_CLASS or name in derived):
                    derived.add(node.name)
                    changed = True
                    break
    return frozenset(derived)


def _is_strict_narrowing(
    app_shape: tuple[frozenset[str], bool],
    sdk_shape: tuple[frozenset[str], bool],
) -> bool:
    """True when *app_shape* is a strict subset of *sdk_shape*.

    ``app_lits`` must be a subset of ``sdk_lits``, and the app may only claim
    a non-Literal ("other") arm when the SDK's own type has one too — an app
    cannot widen past what the SDK declares. A shape with no literal values
    and no other arm means the annotation could not be resolved at all
    (an opaque subscript, an unresolvable forward ref); it is left alone
    rather than guessed at.
    """
    app_lits, app_other = app_shape
    sdk_lits, sdk_other = sdk_shape
    if not app_lits and not app_other:
        return False
    subset = app_lits <= sdk_lits and (not app_other or sdk_other)
    if not subset:
        return False
    return app_lits != sdk_lits or app_other != sdk_other


def check_p052(
    file_trees: dict[Path, ast.AST],
    file_directives: dict[Path, dict[int, _IgnoreDirective]],
    root: Path,
) -> list[Finding]:
    """Emit P052 for each inherited SDK ``ClassVar`` an ``App`` subclass narrows."""
    sdk_shapes = _sdk_app_classvar_shapes(str(root))
    if not sdk_shapes:
        # Could not resolve the installed SDK's declaration — stay silent
        # rather than grade against a guess.
        return []

    findings: list[Finding] = []
    for path, tree in file_trees.items():
        if not isinstance(tree, ast.Module):
            continue
        app_subclasses = _collect_app_subclasses(tree)
        if not app_subclasses:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        filename = str(rel)
        directives = file_directives.get(path, {})

        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue
            if class_node.name not in app_subclasses:
                continue
            for stmt in class_node.body:
                if not isinstance(stmt, ast.AnnAssign) or stmt.value is None:
                    # Bare `name = value` (no annotation) inherits the SDK's
                    # type outright; annotation-only (no value) redeclares
                    # nothing bound.  Neither is this shape.
                    continue
                if not isinstance(stmt.target, ast.Name):
                    continue
                name = stmt.target.id
                sdk_shape = sdk_shapes.get(name)
                if sdk_shape is None:
                    continue
                app_shape = _annotation_shape(stmt.annotation)
                if not _is_strict_narrowing(app_shape, sdk_shape):
                    continue
                example = (
                    f"`{name} = {stmt.value.value!r}`"
                    if isinstance(stmt.value, ast.Constant)
                    else f"`{name} = <the same value>`"
                )
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P052",
                        node=stmt,
                        message=(
                            f"Class '{class_node.name}' redeclares the inherited "
                            f"`{name}` ClassVar with a narrower annotation than "
                            "application_sdk.app.base.App declares. ClassVar "
                            "overrides are invariant, so this narrowed annotation is "
                            "an incompatible override under the pyright 'standard' "
                            f"baseline. Drop the redeclaration — {example} with no "
                            "annotation inherits the SDK's declared type — or restate "
                            "the SDK's full annotation exactly if an explicit "
                            "annotation is genuinely required. Suppress with "
                            "'# conformance: ignore[P052] <reason>' at the assignment "
                            "site."
                        ),
                        directives=directives,
                    )
                )
    return findings
