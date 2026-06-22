"""Shared deprecation extractor — the single AST pass behind the whole B-series.

Every B-series view reads from this one extraction so detection never drifts
between the manifest generator and the authoring checks:

* the **manifest generator** (``conformance.tools.generate_deprecations``) records
  every *marked* symbol so B001 can flag app consumption of it fleet-wide;
* **B002** flags a marked symbol whose notice is missing a migration target
  and/or a removal version;
* **B003** flags a marked symbol whose stated removal version the SDK has already
  reached;
* **B004** flags a symbol whose docstring *claims* deprecation but carries no
  machine-readable marker at all.

What counts as a machine-readable marker (derived empirically from the SDK):

1. a ``@deprecated("…")`` decorator (PEP 702 / ``typing_extensions``) on a
   function / method / class — the message is its first string argument;
2. a class whose ``__init__`` or ``__init_subclass__`` body directly emits
   ``warnings.warn(<msg>, DeprecationWarning, …)`` — attributed to the *class*.

Everything else (a bare ``warnings.warn(DeprecationWarning)`` in some other
method, deprecated *parameters*, deprecated *modes*) is an intentional,
documented coverage limit: a warn call-site is not itself a symbol, and scraping
it would mis-attribute the deprecation to ``__init__`` / a parameter name.

Symbol extraction (``extract_sites`` — the manifest and B004) walks only the
module top level and one class level deep, so a ``@deprecated`` symbol *guarded*
by ``if sys.version_info >= …:`` / ``try/except ImportError:`` is invisible to it.
Notice extraction (``extract_notices`` — B002/B003) uses a full ``ast.walk`` and
so still sees guarded notices; only the importable-symbol surface has this limit.
This is acceptable: the SDK does not guard deprecated public symbols today.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

# Decorator / call names we recognise.
_DEPRECATED_DECORATORS: frozenset[str] = frozenset({"deprecated"})
_WARN_INIT_METHODS: frozenset[str] = frozenset({"__init__", "__init_subclass__"})

# A migration target is named when the notice points at a replacement.  Derived
# from every notice the SDK writes today: "use X", "Use X instead",
# "Migrate to Y", "Migrate now: …", "replaced by Z", "… instead".
_MIGRATION_RE = re.compile(
    r"\b(use|migrate|replaced\s+by|instead|switch\s+to|prefer)\b",
    re.IGNORECASE,
)

# A removal version is named as "removed in vX[.Y[.Z]]" / "will be removed in …".
_REMOVAL_RE = re.compile(
    r"removed\s+in\s+[vV]?(\d+(?:\.\d+)*)",
    re.IGNORECASE,
)

# A docstring claims deprecation when a line opens with "Deprecated:" (the SDK's
# convention) or carries the Sphinx ``.. deprecated::`` directive.  Requiring the
# colon (rather than a bare "Deprecated" word boundary) keeps free-form prose like
# "Deprecated APIs are filtered out here" from reading as a claim, while still
# matching every real notice ("Deprecated: use X — removed in v4.0").
_DOCSTRING_CLAIM_RE = re.compile(
    r"^\s*deprecated\s*:|^\s*\.\.\s*deprecated::",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DeprecationNotice:
    """One deprecation *notice* anywhere in a module — a ``@deprecated`` message or
    a ``warnings.warn(..., DeprecationWarning)`` call, regardless of what it
    deprecates.

    Notice-level (vs symbol-level :class:`DeprecationSite`) because authoring
    hygiene is a property of the *notice text and its promise*, not of an
    importable symbol: a deprecated **parameter** or **mode** still must name a
    replacement and a removal version, and its removal can still fall overdue.
    """

    message: str
    via: str
    """``"decorator"`` | ``"warn"``."""
    has_migration_target: bool
    removal_version_raw: str | None
    lineno: int


@dataclass(frozen=True)
class DeprecationSite:
    """A deprecated symbol discovered in one source module.

    ``marker_via`` is ``None`` for a *claim-only* site (docstring says deprecated
    but nothing enforces it) — that is exactly what B004 reports.  ``message`` is
    the empty string for a claim-only site or a marker whose message could not be
    statically extracted (e.g. a non-literal ``warnings.warn`` argument).
    """

    symbol: str
    """Public symbol name, e.g. ``"DiscoveryError"`` or ``"upload_to_atlan"``."""

    kind: str
    """``"function"`` | ``"method"`` | ``"class"``."""

    marker_via: str | None
    """``"decorator"`` | ``"warn"`` for a marked symbol; ``None`` if claim-only."""

    message: str
    """The deprecation notice text (best-effort static extraction)."""

    has_migration_target: bool
    """Whether *message* names a replacement (drives B002)."""

    removal_version_raw: str | None
    """The ``"removed in vX"`` version string, if the notice carries one."""

    docstring_claim: bool
    """Whether the symbol's docstring claims deprecation (drives B004)."""

    emits_warning: bool
    """Whether the symbol's own body emits a ``DeprecationWarning`` anywhere.  A
    claim-only symbol that *does* warn at runtime is still enforced, so B004 must
    not fire on it (the notice may be malformed — that is B002's job)."""

    lineno: int
    """Line of the symbol definition (1-based)."""


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------


def _static_str(node: ast.expr | None) -> str:
    """Best-effort static text of a string expression.

    Plain (and implicitly-concatenated) string literals come through as a single
    ``ast.Constant``.  f-strings parse as ``ast.JoinedStr``; we keep their
    constant parts and drop the interpolated ``{…}`` placeholders — enough to
    detect the migration / removal phrasing.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return ""


def has_migration_target(message: str) -> bool:
    """True if *message* points at a replacement."""
    return bool(_MIGRATION_RE.search(message))


def removal_version(message: str) -> str | None:
    """Return the ``removed in vX`` version string in *message*, else ``None``.

    Takes the *last* ``removed in vX`` match rather than the first, so a notice
    that mentions an earlier version before the operative one (e.g. "was removed
    in v2 internals, will be removed in v4 for callers") resolves to the version
    callers actually care about.
    """
    matches = _REMOVAL_RE.findall(message)
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# Marker detection
# ---------------------------------------------------------------------------


def _deprecated_decorator_message(node: ast.AST) -> str | None:
    """If *node* (a def/class) carries ``@deprecated(...)``, return its message.

    Returns the (possibly empty) message string when the decorator is present,
    ``None`` when it is absent.  Handles stacked decorators, both the bare
    ``@deprecated`` and called ``@deprecated("…")`` forms, and both the plain
    (``@deprecated``) and qualified (``@typing_extensions.deprecated``) names.
    """
    decorators = getattr(node, "decorator_list", [])
    for dec in decorators:
        if isinstance(dec, ast.Call):
            target = dec.func
            args = dec.args
        else:
            target = dec
            args = []
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.Attribute):
            name = target.attr  # qualified: te.deprecated / warnings.deprecated
        else:
            name = None
        if name in _DEPRECATED_DECORATORS:
            return _static_str(args[0]) if args else ""
    return None


def _is_deprecation_warn_call(node: ast.AST) -> str | None:
    """If *node* is ``warnings.warn(<msg>, DeprecationWarning, …)``, return msg.

    Recognises both ``warnings.warn(...)`` and a bare ``warn(...)`` (from
    ``from warnings import warn``).  ``DeprecationWarning`` may be the second
    positional argument or the ``category=`` keyword.  Returns ``None`` when the
    call is not a ``DeprecationWarning`` emission.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    is_warn = (isinstance(func, ast.Attribute) and func.attr == "warn") or (
        isinstance(func, ast.Name) and func.id == "warn"
    )
    if not is_warn:
        return None

    def _names_deprecation(expr: ast.expr | None) -> bool:
        return isinstance(expr, ast.Name) and expr.id == "DeprecationWarning"

    positional = len(node.args) >= 2 and _names_deprecation(node.args[1])
    keyword = any(
        kw.arg == "category" and _names_deprecation(kw.value) for kw in node.keywords
    )
    if not (positional or keyword):
        return None
    return _static_str(node.args[0]) if node.args else ""


def _class_warn_message(class_node: ast.ClassDef) -> str | None:
    """Return a class's deprecation message if ``__init__`` / ``__init_subclass__``
    directly emits a ``DeprecationWarning``, else ``None``.

    Walks each such method body (so a ``warn`` nested in a guard ``if`` still
    counts) but does **not** descend into other methods — the warn must belong to
    construction/subclassing of *this* class for the attribution to be sound.
    """
    for item in class_node.body:
        if (
            isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            and item.name in _WARN_INIT_METHODS
        ):
            for sub in ast.walk(item):
                message = _is_deprecation_warn_call(sub)
                if message is not None:
                    return message
    return None


def _docstring_claims(node: ast.AST) -> bool:
    """True if *node*'s docstring opens with "Deprecated" or a ``.. deprecated::``."""
    if not isinstance(
        node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ):
        return False
    doc = ast.get_docstring(node, clean=False)
    if not doc:
        return False
    return any(_DOCSTRING_CLAIM_RE.match(line) for line in doc.splitlines())


def _emits_deprecation_warning(node: ast.AST) -> bool:
    """True if *node*'s subtree emits a ``DeprecationWarning`` anywhere."""
    return any(_is_deprecation_warn_call(sub) is not None for sub in ast.walk(node))


def _build_site(
    *, symbol: str, kind: str, node: ast.AST, marker_via: str | None, message: str
) -> DeprecationSite:
    return DeprecationSite(
        symbol=symbol,
        kind=kind,
        marker_via=marker_via,
        message=message,
        has_migration_target=has_migration_target(message),
        removal_version_raw=removal_version(message),
        docstring_claim=_docstring_claims(node),
        emits_warning=_emits_deprecation_warning(node),
        lineno=getattr(node, "lineno", 1),
    )


# ---------------------------------------------------------------------------
# Module extraction
# ---------------------------------------------------------------------------


def _notice(message: str, via: str, lineno: int) -> DeprecationNotice:
    return DeprecationNotice(
        message=message,
        via=via,
        has_migration_target=has_migration_target(message),
        removal_version_raw=removal_version(message),
        lineno=lineno,
    )


def extract_notices(tree: ast.Module) -> list[DeprecationNotice]:
    """Return every deprecation *notice* in *tree* (decorator or warn call).

    Walks the whole module — including method bodies — so notices for deprecated
    parameters / modes (which are not importable symbols) are still subject to the
    authoring-hygiene checks B002 (format) and B003 (overdue removal).
    """
    notices: list[DeprecationNotice] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            dec_message = _deprecated_decorator_message(node)
            if dec_message is not None:
                notices.append(_notice(dec_message, "decorator", node.lineno))
        warn_message = _is_deprecation_warn_call(node)
        if warn_message is not None:
            notices.append(_notice(warn_message, "warn", node.lineno))
    return notices


def extract_sites(tree: ast.Module) -> list[DeprecationSite]:
    """Return every deprecated/claimed symbol in *tree*.

    Marked symbols come first conceptually but the list preserves source order.
    A symbol is emitted at most once even if it carries both a decorator and a
    docstring claim (the marker wins; ``docstring_claim`` is still recorded).
    """
    sites: list[DeprecationSite] = []

    def _visit_def(node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        dec_message = _deprecated_decorator_message(node)
        if dec_message is not None:
            sites.append(
                _build_site(
                    symbol=node.name,
                    kind=kind,
                    node=node,
                    marker_via="decorator",
                    message=dec_message,
                )
            )
        elif _docstring_claims(node):
            sites.append(
                _build_site(
                    symbol=node.name,
                    kind=kind,
                    node=node,
                    marker_via=None,
                    message="",
                )
            )

    def _visit_class(node: ast.ClassDef) -> None:
        dec_message = _deprecated_decorator_message(node)
        warn_message = _class_warn_message(node)
        if dec_message is not None:
            marker_via, message = "decorator", dec_message
        elif warn_message is not None:
            marker_via, message = "warn", warn_message
        else:
            marker_via, message = None, ""
        if marker_via is not None or _docstring_claims(node):
            sites.append(
                _build_site(
                    symbol=node.name,
                    kind="class",
                    node=node,
                    marker_via=marker_via,
                    message=message,
                )
            )

    # Walk top-level and one class level deep: top-level funcs/classes are
    # importable symbols; methods (one level inside a class) cover @deprecated
    # methods like ``upload_to_atlan``.  Deeper nesting is not an importable
    # surface and is out of scope.
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            _visit_def(node, "function")
        elif isinstance(node, ast.ClassDef):
            _visit_class(node)
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    _visit_def(item, "method")

    return sites
