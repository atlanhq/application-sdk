"""L001, L003, L011, L014, L018, L020 — log-format rules."""

from __future__ import annotations

import ast

from ._base import _MixinBase
from ._constants import LOGGER_NAMES, STDLIB_LOG_KWARGS_ALLOWED
from ._helpers import is_logger_call


class FormatMixin(_MixinBase):
    """Rule methods for L001, L003, L011, L014, L018, L020 (log-format category)."""

    # ── L001 FStringInLogMessage ──────────────────────────────────────────────

    def _check_l001(self, node: ast.Call) -> None:
        """Flag f-strings used as the first argument to a logger call."""
        if not is_logger_call(node, self._logging_module_names):
            return
        if not node.args:
            return
        first_arg = node.args[0]
        if isinstance(first_arg, ast.JoinedStr):
            self._add(
                "L001",
                node,
                "f-string in log message — breaks log grouping and aggregation. "
                "Rewrite as %-style: logger.info('value is %s', value).",
            )

    # ── L003 ExtraKwargsWrongFramework ────────────────────────────────────────

    def _check_l003(self, node: ast.Call) -> None:
        """Flag ``extra={}`` in structlog/loguru logger calls.

        For structlog and loguru, ``extra={}`` is the stdlib idiom — the data
        lands in an unindexed nested dict invisible to aggregation queries.
        Use direct kwargs (structlog) or %-style message body (loguru) instead.
        Stdlib is exempt: ``extra={}`` is the correct stdlib mechanism.
        """
        if self._framework not in ("structlog", "loguru"):
            return
        if not is_logger_call(node, self._logging_module_names):
            return
        if any(kw.arg == "extra" for kw in node.keywords):
            self._add(
                "L003",
                node,
                f"extra={{}} in {self._framework} logger call — data lands in an "
                "unindexed nested dict invisible to aggregation queries. "
                "Embed context in the %-style message body instead.",
            )

    # ── L011 StringConcatenationInLog ─────────────────────────────────────────

    def _check_l011(self, node: ast.Call) -> None:
        """Flag string concatenation as the first arg to a logger call."""
        if not is_logger_call(node, self._logging_module_names):
            return
        if not node.args:
            return
        first_arg = node.args[0]
        if isinstance(first_arg, ast.BinOp) and isinstance(first_arg.op, ast.Add):
            self._add(
                "L011",
                node,
                "String concatenation in log message — breaks log grouping. "
                "Rewrite as %-style: logger.info('value is %s', value).",
            )

    # ── L014 StructlogEventKwargOverwrite ─────────────────────────────────────

    def _check_l014(self, node: ast.Call) -> None:
        """Flag ``event=`` keyword in structlog logger calls.

        In structlog, the first positional arg is stored as the ``event`` key —
        it IS the log message.  Passing ``event=`` as a keyword silently
        overwrites the message with the domain value.
        """
        if self._framework != "structlog":
            return
        if not is_logger_call(node, self._logging_module_names):
            return
        if any(kw.arg == "event" for kw in node.keywords):
            self._add(
                "L014",
                node,
                "event= kwarg in structlog call silently overwrites the log message. "
                "Rename the domain field to avoid collision with the structlog 'event' key.",
            )

    # ── L018 KwargsInApplicationLogCalls ─────────────────────────────────────

    def _check_l018(self, node: ast.Call) -> None:
        """Flag arbitrary kwargs in non-stdlib logger calls.

        The SDK logging adapter auto-injects Temporal context as the only
        top-level indexed columns.  All other kwargs land in an unindexed JSON
        blob invisible to aggregation queries.  Exempt: exc_info=, and adapter
        files (the entire file, not just the shim method — see ``is_adapter_file``).
        """
        if self._framework == "stdlib":
            return  # stdlib kwargs crash (L013) — a separate rule
        if self._is_adapter_file:
            return
        if not is_logger_call(node, self._logging_module_names):
            return
        extra_kwargs = [
            kw
            for kw in node.keywords
            if kw.arg is not None and kw.arg not in STDLIB_LOG_KWARGS_ALLOWED
        ]
        if extra_kwargs:
            names = ", ".join(kw.arg for kw in extra_kwargs if kw.arg)  # type: ignore[misc]
            self._add(
                "L018",
                node,
                f"kwargs in application log call ({names}) — embed context in the "
                "%-style message body instead of keyword arguments.",
            )

    # ── L020 DeprecatedLoggingWarn ────────────────────────────────────────────

    def _check_l020(self, node: ast.Call) -> None:
        """Flag ``logger.warn(...)`` — deprecated alias for ``logger.warning(...)``.

        Covers three forms:
        * ``logger.warn(...)``   — receiver in ``LOGGER_NAMES``
        * ``logging.warn(...)``  — receiver is any name bound to the logging module
          (including aliases like ``import logging as L; L.warn(...)``)
        * ``warn(...)``          — bare call after ``from logging import warn``
        """
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr != "warn":
                return
            obj = func.value
            if isinstance(obj, ast.Name):
                if (
                    obj.id not in LOGGER_NAMES
                    and obj.id not in self._logging_module_names
                ):
                    return
            elif isinstance(obj, ast.Attribute):
                # self.logger.warn(...) — terminal attr must be a logger name
                if obj.attr not in LOGGER_NAMES:
                    return
            else:
                return
        elif isinstance(func, ast.Name):
            if func.id not in self._logging_warn_names:
                return
        else:
            return
        self._add(
            "L020",
            node,
            "logger.warn() is a deprecated alias — use logger.warning() instead.",
        )
