"""L012, L013, L015, L016 (per-file), L019 — log-config rules."""

from __future__ import annotations

import ast

from ._base import _MixinBase
from ._constants import STDLIB_LOG_KWARGS_ALLOWED, STDLIB_LOG_RECORD_RESERVED
from ._helpers import is_logger_call


class ConfigMixin(_MixinBase):
    """Rule methods for L012, L013, L015, L019 (log-config and log-crash categories).

    L016 (BasicConfigNoopAfterFirstCall) requires cross-file context and is
    implemented in ``scan_all`` inside ``__init__.py``.  This mixin exposes
    a helper to *collect* basicConfig call locations per file.
    """

    # ── L012 StdlibExtraReservedKeyCollision ──────────────────────────────────

    def _check_l012(self, node: ast.Call) -> None:
        """Flag ``extra={}`` where a key collides with a stdlib LogRecord attribute.

        ``stdlib``-only: ``Logger.makeRecord()`` raises ``KeyError`` when any
        key in ``extra={}`` matches a ``LogRecord`` attribute, propagating
        directly to the caller — not caught by ``handleError()``.
        """
        if self._framework != "stdlib":
            return
        if not is_logger_call(node):
            return
        for kw in node.keywords:
            if kw.arg != "extra":
                continue
            extra_value = kw.value
            if not isinstance(extra_value, ast.Dict):
                continue
            for key_node in extra_value.keys:
                if not isinstance(key_node, ast.Constant):
                    continue
                key = key_node.value
                if isinstance(key, str) and key in STDLIB_LOG_RECORD_RESERVED:
                    self._add(
                        "L012",
                        node,
                        f"extra={{'{key}': ...}} collides with a stdlib LogRecord "
                        f"attribute — raises KeyError at the logger.info() call site. "
                        f"Rename the key.",
                    )
                    return  # one finding per call site

    # ── L013 StdlibArbitraryKwargs ────────────────────────────────────────────

    def _check_l013(self, node: ast.Call) -> None:
        """Flag kwargs not in the stdlib allowlist on a stdlib logger call.

        ``stdlib``-only: ``logger.info()`` raises ``TypeError`` immediately for
        any kwarg outside ``{exc_info, extra, stack_info, stacklevel}``.  Very
        common when migrating from structlog/loguru — call sites look identical
        but fail at runtime.
        """
        if self._framework != "stdlib":
            return
        if not is_logger_call(node):
            return
        bad = [
            kw.arg
            for kw in node.keywords
            if kw.arg is not None and kw.arg not in STDLIB_LOG_KWARGS_ALLOWED
        ]
        if bad:
            names = ", ".join(str(n) for n in bad)
            self._add(
                "L013",
                node,
                f"Arbitrary kwargs ({names}) in stdlib logger call — raises TypeError "
                f"at runtime. Only {sorted(STDLIB_LOG_KWARGS_ALLOWED)} are accepted. "
                f"Migrate to exc_info=True or embed context in the message body.",
            )

    # ── L015 DictConfigDisableExistingLoggers ─────────────────────────────────

    def _check_l015(self, node: ast.Call) -> None:
        """Flag ``dictConfig(...)`` calls that don't set ``disable_existing_loggers=False``.

        ``stdlib``-only: ``dictConfig()`` defaults ``disable_existing_loggers``
        to ``True``, silently disabling every logger created before the call.
        The checker fires when the argument is a literal dict without the key or
        with it set to ``True``.  When the argument is a variable (unknown at
        static analysis time), the rule is skipped to avoid false positives.
        """
        if self._framework != "stdlib":
            return
        func = node.func
        # Accept both ``logging.config.dictConfig(...)`` and bare ``dictConfig(...)``
        if isinstance(func, ast.Attribute):
            if func.attr != "dictConfig":
                return
        elif isinstance(func, ast.Name):
            if func.id != "dictConfig":
                return
        else:
            return

        if not node.args:
            return
        cfg = node.args[0]
        if not isinstance(cfg, ast.Dict):
            return  # variable arg — cannot analyse statically

        # Look for "disable_existing_loggers" key
        for key_node, val_node in zip(cfg.keys, cfg.values):
            if not isinstance(key_node, ast.Constant):
                continue
            if key_node.value != "disable_existing_loggers":
                continue
            # Found the key — fire only if it is True (or any truthy non-False literal)
            if isinstance(val_node, ast.Constant) and val_node.value is False:
                return  # explicitly set to False — correct
            self._add(
                "L015",
                node,
                "dictConfig() with disable_existing_loggers not set to False — "
                "silently disables all loggers created before this call. "
                'Add "disable_existing_loggers": False to the config dict.',
            )
            return

        # Key is absent → defaults to True
        self._add(
            "L015",
            node,
            "dictConfig() missing disable_existing_loggers=False — defaults to True, "
            "silently disabling all loggers created before this call. "
            'Add "disable_existing_loggers": False to the config dict.',
        )

    # ── L016 BasicConfigNoopAfterFirstCall (per-file collector) ───────────────

    def _collect_basicconfig_calls(self, tree: ast.Module) -> list[ast.Call]:
        """Return all ``logging.basicConfig(...)`` / ``basicConfig(...)`` calls."""
        calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "basicConfig":
                calls.append(node)
            elif isinstance(func, ast.Name) and func.id == "basicConfig":
                calls.append(node)
        return calls

    # ── L019 DiscardedBindResult ──────────────────────────────────────────────

    def _check_l019(self, node: ast.Call) -> None:
        """Flag bare ``logger.bind(...)`` calls whose return value is discarded.

        ``structlog`` and ``loguru`` ``bind()`` returns a *new* logger with the
        bound context — the original is unchanged.  A bare call (result not
        assigned) is always a bug: the context is constructed and immediately
        discarded.
        """
        func = node.func
        if not isinstance(func, ast.Attribute):
            return
        if func.attr != "bind":
            return
        # The call is a bare statement — this is checked by the Checker's
        # visit_Expr which calls _check_l019 only for Expr-level calls.
        self._add(
            "L019",
            node,
            "logger.bind() result is discarded — bind() returns a new logger "
            "with the context; the original is unchanged. "
            "Assign the result: log = logger.bind(key=value).",
        )
