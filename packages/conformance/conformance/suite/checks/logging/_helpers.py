"""AST helpers shared across L-series rule modules."""

from __future__ import annotations

import ast
from pathlib import PurePosixPath
from typing import Literal

from .._ast_common._exc_info import has_exc_info_traceback
from ._constants import ADAPTER_MARKERS, LOG_METHODS, LOGGER_NAMES

Framework = Literal["stdlib", "structlog", "loguru", "unknown"]

# Dev-harness exemption for the block-tier L002 rule (mirrors the S-series
# policy in ``checks/security``): ``run_dev*.py`` and anything under
# ``scripts/`` are local dev entry points, never shipped workflow code, so
# the correlation-ID/provenance argument behind L002 does not apply there.
_HARNESS_DIRS: frozenset[str] = frozenset({"scripts"})


def is_dev_harness(rel_path: str) -> bool:
    """True when *rel_path* is a dev harness (``scripts/`` or ``run_dev*.py``).

    *rel_path* is a repo-root-relative path string as carried on findings.
    Used by the L002 cross-file pass; test files are already excluded by the
    shared discovery walk and need no handling here.
    """
    parts = PurePosixPath(rel_path.replace("\\", "/")).parts
    if parts and parts[-1].startswith("run_dev"):
        return True
    return bool(set(parts[:-1]) & _HARNESS_DIRS)


# ---------------------------------------------------------------------------
# Framework detection
# ---------------------------------------------------------------------------


def detect_framework(tree: ast.Module) -> Framework:
    """Scan module-level imports and return the logging framework in use.

    Priority: structlog > loguru > stdlib.  When a file imports both stdlib
    ``logging`` and a higher-level framework (common in adapters), the
    higher-level framework wins.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root == "structlog":
                    found.add("structlog")
                elif root == "loguru":
                    found.add("loguru")
                elif root == "logging":
                    found.add("stdlib")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root == "structlog":
                found.add("structlog")
            elif root == "loguru":
                found.add("loguru")
            elif root == "logging":
                found.add("stdlib")
    if "structlog" in found:
        return "structlog"
    if "loguru" in found:
        return "loguru"
    if "stdlib" in found:
        return "stdlib"
    return "unknown"


def collect_logging_aliases(
    tree: ast.Module,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(logging_module_names, warn_function_names)`` for *tree*.

    *logging_module_names* — all names bound to the ``logging`` module itself,
    e.g. ``{"logging", "L"}`` after ``import logging as L``.  Always includes
    ``"logging"``.

    *warn_function_names* — all names bound to ``logging.warn`` via a
    ``from logging import warn [as X]`` import, e.g. ``{"warn", "log_warn"}``.
    """
    module_names: set[str] = {"logging"}
    warn_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "logging":
                    module_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "logging":
                for alias in node.names:
                    if alias.name == "warn":
                        warn_names.add(alias.asname or "warn")
    return frozenset(module_names), frozenset(warn_names)


# ---------------------------------------------------------------------------
# Logger-call recognition
# ---------------------------------------------------------------------------


def is_logger_call(
    node: ast.Call,
    module_names: frozenset[str] = frozenset({"logging"}),
) -> bool:
    """True if *node* is a recognised ``logger.<method>(...)`` call.

    Matches:
    * Named-variable receivers in ``LOGGER_NAMES`` (``logger.info(...)``)
    * Module-level stdlib form — any name in *module_names*
      (``logging.info(...)``, ``L.info(...)`` after ``import logging as L``)
    * Attribute receivers whose terminal attr is in ``LOGGER_NAMES``
      (``self.logger.error(...)``, ``cls._logger.warning(...)``)

    Pass ``module_names=self._logging_module_names`` from mixin call sites so
    that files using ``import logging as L`` are not silently skipped.

    Note: receiver must be a simple ``Name`` or a one-level ``Attribute``
    (e.g. ``self.logger``).  Deeper chains (``self.x.y.logger``) are not
    matched to keep false-positive risk low.
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in LOG_METHODS:
        return False
    obj = func.value
    if isinstance(obj, ast.Name):
        return obj.id in LOGGER_NAMES or obj.id in module_names
    # Accept self.logger / cls._logger — one-level attribute receiver
    if isinstance(obj, ast.Attribute):
        return obj.attr in LOGGER_NAMES
    return False


def get_logger_method(
    node: ast.Call,
    module_names: frozenset[str] = frozenset({"logging"}),
) -> str | None:
    """Return the method name when *node* is a logger call, else ``None``.

    Pass ``module_names=self._logging_module_names`` from mixin call sites so
    that aliased stdlib imports (``import logging as L``) are not skipped.
    """
    if not isinstance(node.func, ast.Attribute):
        return None
    attr = node.func.attr
    if attr not in LOG_METHODS:
        return None
    obj = node.func.value
    if isinstance(obj, ast.Name) and (obj.id in LOGGER_NAMES or obj.id in module_names):
        return attr
    if isinstance(obj, ast.Attribute) and obj.attr in LOGGER_NAMES:
        return attr
    return None


def has_exc_info_true(call: ast.Call, exception_name: str | None = None) -> bool:
    """True if the call has literal or current-exception ``exc_info``.

    Thin alias over :func:`_ast_common.has_exc_info_traceback` — the E-series
    shares the same predicate, and keeping one definition is what stops
    ``exc_info=exc`` being silent for one series and a finding for the other.
    """
    return has_exc_info_traceback(call, exception_name)


def has_exc_info_kwarg(call: ast.Call) -> bool:
    """True if the call has any ``exc_info=`` keyword (regardless of value)."""
    return any(kw.arg == "exc_info" for kw in call.keywords)


# ---------------------------------------------------------------------------
# Adapter-file detection
# ---------------------------------------------------------------------------


def is_adapter_file(tree: ast.Module) -> bool:
    """True if this file defines the logging adapter.

    Files that define ``AtlanLoggerAdapter`` or ``get_logger`` at module
    top-level are the logging infrastructure itself and are entirely exempt
    from L017 (.exception() shim) and L018 (kwargs in the factory / adapter
    body).  The exemption is whole-file, not method-scoped.

    Only top-level definitions are checked (``tree.body``): a nested method
    named ``get_logger`` inside an unrelated class must not exempt the file.
    """
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in ADAPTER_MARKERS:
                return True
    return False


# ---------------------------------------------------------------------------
# Loop-bound detection
# ---------------------------------------------------------------------------


def loop_is_bounded(loop_node: ast.For | ast.AsyncFor) -> bool:
    """True if the loop is clearly bounded to ≤ 10 iterations.

    Covers:
    * ``for x in range(n)`` — literal *n* ≤ 10
    * ``for x in [a, b, …]`` / ``(a, b, …)`` — literal collection ≤ 10 items
    """
    iter_ = loop_node.iter
    # range(n) with a single literal int argument
    if isinstance(iter_, ast.Call):
        func = iter_.func
        func_name = func.id if isinstance(func, ast.Name) else None
        if func_name == "range" and len(iter_.args) == 1:
            arg = iter_.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                return arg.value <= 10
    # Literal sequence with a known small number of elements
    if isinstance(iter_, (ast.List, ast.Tuple, ast.Set)):
        return len(iter_.elts) <= 10
    return False


# ---------------------------------------------------------------------------
# __main__ block detection
# ---------------------------------------------------------------------------


def main_block_lines(tree: ast.Module) -> frozenset[int]:
    """Return all line numbers that fall inside ``if __name__ == '__main__':``."""
    lines: set[int] = set()
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        left = test.left
        if not (isinstance(left, ast.Name) and left.id == "__name__"):
            continue
        for comp in test.comparators:
            if isinstance(comp, ast.Constant) and comp.value == "__main__":
                for child in ast.walk(node):
                    if hasattr(child, "lineno"):
                        lines.add(child.lineno)  # type: ignore[attr-defined]
                break
    return frozenset(lines)


# ---------------------------------------------------------------------------
# Credential-name heuristics
# ---------------------------------------------------------------------------


def _is_credential_value_name(name: str) -> bool:
    """True if *name* looks like a credential *value* identifier.

    A name matches when it ends with one of the credential-value suffixes and
    does NOT also end with a label suffix (``_name``, ``_type``, ``_id``, …)
    that indicates the variable holds a label rather than the secret itself.
    """
    from ._constants import CREDENTIAL_LABEL_SUFFIXES, CREDENTIAL_VALUE_SUFFIXES

    lower = name.lower()
    # If the name ends with a label suffix, it is a label, not a value.
    if any(lower.endswith(suf) for suf in CREDENTIAL_LABEL_SUFFIXES):
        return False
    return any(lower.endswith(suf) or lower == suf for suf in CREDENTIAL_VALUE_SUFFIXES)


# ---------------------------------------------------------------------------
# Script/CLI detection (L005 exemption) and redaction-placeholder tracking
# (L010 exemption) — FND-61 follow-up: rule-level fixes so fleet code does not
# need inline suppressions for these shapes.
# ---------------------------------------------------------------------------

#: Substrings that mark a string constant as a redaction placeholder.
_REDACTION_MARKERS: tuple[str, ...] = ("redact", "masked", "***", "xxxx")


def is_script_file(text: str, tree: ast.Module) -> bool:
    """True when the module is a standalone script/CLI rather than app code.

    Signals (either suffices):

    * a shebang line — the file is meant to be executed directly;
    * an ``if __name__ == "__main__":`` guard at module level — the file has a
      direct-execution entry point.

    A bare ``argparse`` import is **not** sufficient evidence on its own: a
    mixed library/CLI module imports ``argparse`` for its ``__main__`` block
    yet keeps most of its ``print()`` calls in reusable library functions
    unrelated to the CLI path, and exempting the whole file would hide those.
    Such a module almost always pairs the import with a ``__main__`` guard to
    actually run the parser, so requiring the guard keeps the exemption
    targeted at real scripts without exempting library code.

    For such files stdout is the user interface; ``print()`` is not a logging
    bypass (L005).
    """
    if text.startswith("#!"):
        return True
    for node in tree.body:
        if isinstance(node, ast.If):
            t = node.test
            if (
                isinstance(t, ast.Compare)
                and isinstance(t.left, ast.Name)
                and t.left.id == "__name__"
                and len(t.comparators) == 1
                and isinstance(t.comparators[0], ast.Constant)
                and t.comparators[0].value == "__main__"
            ):
                return True
    return False


def _is_redaction_placeholder_expr(value: ast.expr) -> bool:
    """True for ``"[REDACTED]"``-style constants, or conditionals whose every
    branch is such a constant or ``None`` (``x if cond else None``)."""
    if isinstance(value, ast.Constant):
        if value.value is None:
            return False  # bare None alone is not a placeholder
        if isinstance(value.value, str):
            lowered = value.value.lower()
            return any(m in lowered for m in _REDACTION_MARKERS)
        return False
    if isinstance(value, ast.IfExp):
        branches = (value.body, value.orelse)
        placeholderish = 0
        for b in branches:
            if isinstance(b, ast.Constant) and b.value is None:
                continue
            if _is_redaction_placeholder_expr(b):
                placeholderish += 1
            else:
                return False
        return placeholderish >= 1
    return False


def _iter_bound_names(target: ast.expr):
    """Yield every plain name bound by an assignment *target*.

    Recurses into tuple/list/starred destructuring so that
    ``password, tok = a, b`` and ``(password, *rest) = ...`` both yield their
    leaf names; attribute/subscript targets (``self.x = …``) bind no plain name
    and are skipped.
    """
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Starred):
        yield from _iter_bound_names(target.value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _iter_bound_names(elt)


def _iter_arg_names(args: ast.arguments):
    """Yield every parameter name bound by a function/lambda signature."""
    for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        yield a.arg
    if args.vararg is not None:
        yield args.vararg.arg
    if args.kwarg is not None:
        yield args.kwarg.arg


def _iter_match_capture_names(pattern: ast.AST):
    """Yield every name captured by a ``match`` *pattern*.

    ``case {"pw": password}:`` binds ``password``; ``case [first, *rest]:``
    binds ``first``/``rest``; ``case Point(x=px):`` binds ``px``.  ``MatchAs``
    (``case password:`` / ``case … as password:``) and ``MatchStar``
    (``case [*password]:``) carry the bound name directly; the wrapper patterns
    (``MatchValue``/``MatchSingleton``/``MatchSequence``/``MatchMapping``/
    ``MatchClass``/``MatchOr``) recurse into their sub-patterns.  ``ast.walk``
    over the pattern reaches every nested node, so the two name-bearing types
    are all that need explicit handling.
    """
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchAs):
            if node.name is not None:
                yield node.name
        elif isinstance(node, ast.MatchStar):
            if node.name is not None:
                yield node.name
        # MatchMapping.rest / MatchAs capture via .name are covered above; a
        # bare ``**rest`` in a mapping pattern lands on MatchMapping.rest.
        if isinstance(node, ast.MatchMapping) and node.rest is not None:
            yield node.rest


def collect_redacted_names(tree: ast.Module) -> frozenset[str]:
    """Names *safely* bound to a redaction placeholder, scope- and order-aware.

    ``password = "[REDACTED]" if credentials.get("password") else None`` marks
    ``password`` as a *presence indicator*: logging it discloses nothing, so
    L010 must not flag it (the check matches argument names, not values).

    The set is deliberately conservative — a name is exempt only when **every**
    binding of that name anywhere in the module is a redaction placeholder
    (or an ``Annotated``/plain annotation with no value).  This closes the
    cross-scope and rebinding bypasses:

    * **Cross-function** — a name is only exempt in the lexical scope that
      bound it to a placeholder.  ``password = "[REDACTED]"`` in ``connect``
      must not exempt ``password = creds.get("password")`` in ``reconnect``:
      the real credential value would go unflagged.  Because the two bindings
      live in different scopes, ``reconnect``'s binding is a non-placeholder,
      so the name is dropped from the exempt set entirely.

    * **Rebinding** — if a name is assigned a placeholder once and a real
      value anywhere else (either order), the placeholder does not reliably
      reach the log call, so the exemption is invalidated.  A name that is
      *only* ever rebound between placeholder literals (``x = "[REDACTED]"`` …
      ``x = "***masked***"``) stays exempt.  Note that a bare ``x = None``
      counts as a **real** binding here (``None`` alone is not a placeholder),
      so it too invalidates the exemption — the conservative, BLOCK-tier-safe
      direction.

    Every binding form counts, not just ``x = <value>``: an ``AugAssign``
    (``password += creds.get(...)``), a ``for``/``with … as``/comprehension
    target, an ``import`` binding, tuple/list/starred unpacking, an
    ``except … as`` target, a function/lambda parameter, or a ``match``
    capture pattern all (re)bind a name to a non-placeholder value, so any of
    them drops the name from the exempt set.  L010 is BLOCK tier, so the
    conservative (over-flagging) direction is the safe one — a missed exemption
    is a false positive, a kept one a leak.

    A single source-ordered walk records each binding as placeholder /
    non-placeholder / annotation; a name survives only if it has at least one
    placeholder binding and **no** non-placeholder binding.
    """
    # name -> [saw_placeholder_binding, saw_real_binding]
    state: dict[str, list[bool]] = {}

    def record(name: str, placeholder: bool | None) -> None:
        saw_placeholder, saw_real = state.get(name, [False, False])
        if placeholder is True:
            saw_placeholder = True
        elif placeholder is False:
            saw_real = True
        # placeholder is None → bare annotation; leaves both flags unchanged
        state[name] = [saw_placeholder, saw_real]

    def record_target(target: ast.expr, placeholder: bool) -> None:
        for name in _iter_bound_names(target):
            record(name, placeholder)

    class _Binder(ast.NodeVisitor):
        """Visit bindings in source order, tracking placeholder-ness.

        Every visitor ends in ``generic_visit``, so nested scopes are entered
        and every function/class body is walked — a binding in any scope
        counts toward that name's module-wide state.  This is what makes the
        check cross-function: a non-placeholder binding in *any* scope
        disqualifies the name everywhere.

        Only ``Assign``/``AnnAssign``/``NamedExpr`` can carry a placeholder
        *value*, so they alone may mark a name exempt.  Every other binding
        form is recorded as a real binding and disqualifies the name.  The set
        of forms is exhaustive rather than allow-listed one node type at a
        time — ``AugAssign``, ``for``/``with``/comprehension targets, imports,
        ``except … as``, function/lambda parameters, ``match`` capture
        patterns, and definition names (``def``/``class``/``type``) all rebind
        a name to a non-placeholder value, so recording them ends the
        "one new form per round" class instead of chasing it.
        """

        def visit_Assign(self, node: ast.Assign) -> None:
            placeholder = _is_redaction_placeholder_expr(node.value)
            for target in node.targets:
                record_target(target, placeholder)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is None:
                # bare ``x: str`` — no value yet; do not disqualify the name
                for name in _iter_bound_names(node.target):
                    record(name, None)
            else:
                record_target(node.target, _is_redaction_placeholder_expr(node.value))
            self.generic_visit(node)

        # ``password += creds.get(...)`` — the value is mutated from a
        # non-placeholder source, so the name no longer holds a placeholder.
        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            record_target(node.target, False)
            self.generic_visit(node)

        # Walrus in any expression position binds a name to a real value.
        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            record_target(node.target, _is_redaction_placeholder_expr(node.value))
            self.generic_visit(node)

        # ``for password in …`` / ``async for`` — loop targets bind real values.
        def visit_For(self, node: ast.For) -> None:
            record_target(node.target, False)
            self.generic_visit(node)

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
            record_target(node.target, False)
            self.generic_visit(node)

        # ``with open() as password:`` — the as-target binds a real value.
        def visit_With(self, node: ast.With) -> None:
            for item in node.items:
                if item.optional_vars is not None:
                    record_target(item.optional_vars, False)
            self.generic_visit(node)

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            for item in node.items:
                if item.optional_vars is not None:
                    record_target(item.optional_vars, False)
            self.generic_visit(node)

        # Comprehension loop variables bind real values (``[p for p in …]``).
        def visit_comprehension(self, node: ast.comprehension) -> None:
            record_target(node.target, False)
            self.generic_visit(node)

        # ``import password`` / ``from x import password`` bind module names.
        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                record(alias.asname or alias.name.split(".")[0], False)
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for alias in node.names:
                record(alias.asname or alias.name, False)
            self.generic_visit(node)

        # ``except Exception as password:`` binds the caught exception object.
        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name:
                record(node.name, False)
            self.generic_visit(node)

        # ``def connect(password):`` / ``lambda password: …`` — every parameter
        # binds a real argument value at call time, and the definition name
        # itself (``def password():``) binds the function object.
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            record(node.name, False)  # the definition name binds the function object
            for name in _iter_arg_names(node.args):
                record(name, False)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            record(node.name, False)
            for name in _iter_arg_names(node.args):
                record(name, False)
            self.generic_visit(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            # A lambda has no name of its own — only its parameters bind.
            for name in _iter_arg_names(node.args):
                record(name, False)
            self.generic_visit(node)

        # ``class password:`` binds the class object to the name.
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            record(node.name, False)
            self.generic_visit(node)

        # ``type password = str`` (Python ≥ 3.12) binds the alias object.
        def visit_TypeAlias(self, node: ast.TypeAlias) -> None:  # type: ignore[attr-defined]
            record_target(node.name, False)
            self.generic_visit(node)

        # ``case {"pw": password}:`` — capture patterns bind real values.
        def visit_Match(self, node: ast.Match) -> None:
            for case in node.cases:
                for name in _iter_match_capture_names(case.pattern):
                    record(name, False)
            self.generic_visit(node)

    _Binder().visit(tree)
    return frozenset(
        name
        for name, (saw_placeholder, saw_real) in state.items()
        if saw_placeholder and not saw_real
    )
