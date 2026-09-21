"""P052 NarrowedSdkClassVar — an app re-annotates an SDK-owned ``App``
``ClassVar`` with a type narrower than the SDK declares.

Setting the value is the supported way to choose a posture::

    class MyApp(App):
        preflight_gate_mode = "hard"

Re-stating the *annotation* is not. ``ClassVar`` is invariant, so a subclass
whose annotation omits a member the base declares is an incompatible
override — pyright reports it under the ``standard`` baseline D008 mandates
fleet-wide, and the omitted member becomes unusable in that app until the
annotation is edited. Nothing fails at runtime, which is why this is
``WARN``: the coercion helpers accept every spelling either way.

The shape is not carelessness. The SDK declared the plain ``Literal`` form
for ``preflight_gate_mode`` before the enum existed, and still declares it
for the sibling ``artifact_validation_mode``, so apps mirrored their base
class — ordinary practice that the SDK's own widening turned into a defect.

**Resolved against the installed SDK, never a hardcoded list.** The declared
type is read from ``application_sdk/app/base.py`` as it exists in the
environment being graded, by parsing it — no import, so no module side
effects. A literal copy of today's union baked in here would keep grading
apps against a type the SDK has since widened again, which is the failure
this rule exists to catch, one level up.

When the SDK cannot be located the check yields nothing. A rule that cannot
read what it grades against must not guess.
"""

from __future__ import annotations

import ast
import importlib.util
from functools import lru_cache
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

#: The class that owns the ClassVars this rule protects, and the module it
#: is declared in, relative to the installed package root.
_SDK_APP_MODULE = ("app", "base.py")
_SDK_APP_CLASS = "App"

#: Class names an app's own base list may carry for this rule to apply. An
#: app subclasses `App` directly or through one intermediate of its own, and
#: the intermediate is caught by the transitive pass below.
_APP_BASE_NAMES = frozenset({_SDK_APP_CLASS, "BaseApplication"})


def _annotation_members(annotation: ast.expr) -> frozenset[str] | None:
    """The members of a ``ClassVar[...]`` annotation, as normalised text.

    A union is split into its parts so a subset comparison means what it
    says. ``ClassVar["A | Literal['x']"]`` and ``ClassVar[A | Literal['x']]``
    give the same answer: the SDK writes the forward-reference form and an
    app typically writes the bare one, and a rule that distinguished them
    would grade quoting style.

    Returns None when the annotation is not a ``ClassVar[...]`` — those are
    not the shape this rule is about.
    """
    if not (
        isinstance(annotation, ast.Subscript)
        and _name_of(annotation.value) == "ClassVar"
    ):
        return None

    inner = annotation.slice
    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
        # A forward reference: re-parse its contents as an expression.
        try:
            inner = ast.parse(inner.value, mode="eval").body
        except SyntaxError:
            return None

    return frozenset(_union_parts(inner))


def _union_parts(node: ast.expr) -> list[str]:
    """One annotation's union members, flattened and normalised."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union_parts(node.left) + _union_parts(node.right)
    return [_normalise(node)]


def _normalise(node: ast.expr) -> str:
    """One annotation member as comparable text.

    Quote style and whitespace are stripped because they carry no meaning
    here: an app writing ``Literal['hard', 'soft']`` and the SDK writing
    ``Literal["hard", "soft"]`` declare the same type, and one index hit in
    the fleet sweep that found this shape was rejected on exactly that
    difference.
    """
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - unparse covers every real annotation
        return ""
    return text.replace('"', "'").replace(" ", "")


def _name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


@lru_cache(maxsize=1)
def sdk_app_classvars() -> dict[str, frozenset[str]]:
    """SDK-owned ``App`` ClassVars mapped to the members they declare.

    Parsed from the installed ``application_sdk`` rather than imported: the
    check runs inside a conformance pass over app source, and importing an
    app framework to read one annotation risks side effects that have
    nothing to do with grading.

    Empty when the SDK is not installed or its shape has changed, which
    makes the rule silent rather than wrong.
    """
    spec = importlib.util.find_spec("application_sdk")
    if spec is None or not spec.submodule_search_locations:
        return {}

    base = Path(next(iter(spec.submodule_search_locations))).joinpath(*_SDK_APP_MODULE)
    try:
        tree = ast.parse(base.read_text(encoding="utf-8"), filename=str(base))
    except (OSError, SyntaxError):
        return {}

    declared: dict[str, frozenset[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == _SDK_APP_CLASS):
            continue
        for stmt in node.body:
            if not (
                isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            ):
                continue
            members = _annotation_members(stmt.annotation)
            if members:
                declared[stmt.target.id] = members
    return declared


def _app_subclasses(tree: ast.Module) -> frozenset[str]:
    """Class names in this file that reach ``App`` through their bases.

    Iterated to a fixed point so an app's own intermediate base — a shared
    `BaseConnectorApp` in the same module — does not hide the declaration
    from the rule.
    """
    derived: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name in derived:
                continue
            for base in node.bases:
                name = _name_of(base)
                if name and (name in _APP_BASE_NAMES or name in derived):
                    derived.add(node.name)
                    changed = True
                    break
    return frozenset(derived)


def check_p052(
    tree: ast.Module, file: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Flag app-side annotations that drop a member the SDK declares."""
    declared = sdk_app_classvars()
    if not declared:
        return []

    # The defining class is not a carrier of its own declaration. Without
    # this the rule fires on the SDK it is defending, every run.
    subclasses = _app_subclasses(tree) - {_SDK_APP_CLASS}
    if not subclasses:
        return []

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name in subclasses):
            continue
        for stmt in node.body:
            if not (
                isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            ):
                continue
            attribute = stmt.target.id
            sdk_members = declared.get(attribute)
            if sdk_members is None:
                continue
            app_members = _annotation_members(stmt.annotation)
            if app_members is None:
                continue

            missing = sdk_members - app_members
            if not missing:
                # An exact restatement, or a widening. Redundant, and not a
                # defect: the declared type still admits everything the SDK
                # admits.
                continue

            findings.append(
                make_finding(
                    filename=file,
                    rule_id="P052",
                    node=stmt,
                    message=(
                        f"Class '{node.name}' re-annotates the SDK-owned `{attribute}` ClassVar and "
                        f"drops {', '.join(sorted(missing))}. ClassVar is invariant, so this is an "
                        "incompatible override under pyright's `standard` baseline, and the dropped "
                        "member cannot be used in this app until the annotation is edited. Set the "
                        f"value without re-annotating it — `{attribute} = ...` — and inherit the "
                        "SDK's declared type. If the narrowing is deliberate, justify it with an "
                        f"inline '# conformance: ignore[P052] <reason>' at the declaration site."
                    ),
                    directives=directives,
                )
            )
    return findings
