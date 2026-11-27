"""The SDK uses only OpenTelemetry's public, non-deprecated surface.

The OTel logs signal is unstable and the SDK floats ``opentelemetry-sdk<2.0.0``,
so a routine 1.x bump can move an ``_internal`` module or drop a deprecated
overload. Neither fails in CI first: an ``_internal`` path is not a public
contract, and ``LogRecord``'s ``trace_id=``/``span_id=``/``trace_flags=``
overload is marked with ``typing_extensions.deprecated`` on an ``@overload``,
which never warns at runtime. These guards read the source instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SDK_ROOT = Path(__file__).resolve().parents[3] / "application_sdk"

#: ``LogRecord`` keywords deprecated since opentelemetry-api 1.35 ("Use context
#: instead").
_DEPRECATED_LOG_RECORD_KWARGS = frozenset({"trace_id", "span_id", "trace_flags"})


def _sdk_trees() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(_SDK_ROOT.rglob("*.py"))
    ]


def _is_otel_internal(module: str) -> bool:
    parts = module.split(".")
    return parts[0] == "opentelemetry" and "_internal" in parts


def _internal_imports(tree: ast.Module) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _is_otel_internal(node.module):
                found.append((node.lineno, node.module))
            elif node.module.split(".")[0] == "opentelemetry":
                found.extend(
                    (node.lineno, f"{node.module}.{alias.name}")
                    for alias in node.names
                    if alias.name == "_internal"
                )
        elif isinstance(node, ast.Import):
            found.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if _is_otel_internal(alias.name)
            )
    return found


def _log_record_names(tree: ast.Module) -> set[str]:
    """Local names bound to OTel ``LogRecord``, aliases included.

    ``from opentelemetry._logs import LogRecord as OTelLogRecord`` binds
    ``OTelLogRecord``; matching on the literal ``LogRecord`` alone would let
    that alias reintroduce the deprecated keywords unseen.
    """
    names = {"LogRecord"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] == "opentelemetry"
        ):
            names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "LogRecord"
            )
    return names


def _deprecated_log_record_calls(tree: ast.Module) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    log_record_names = _log_record_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id not in log_record_names:
                continue
        elif isinstance(func, ast.Attribute):
            # ``mod.LogRecord(...)`` under any module alias. ``logging.LogRecord``
            # is the stdlib class, unrelated to OTel.
            if func.attr != "LogRecord" or (
                isinstance(func.value, ast.Name) and func.value.id == "logging"
            ):
                continue
        else:
            continue
        found.extend(
            (node.lineno, kw.arg)
            for kw in node.keywords
            if kw.arg in _DEPRECATED_LOG_RECORD_KWARGS
        )
    return found


def test_sdk_tree_is_scanned() -> None:
    """The guards below pass vacuously if the path is wrong; pin that it is not."""
    paths = {path.relative_to(_SDK_ROOT).as_posix() for path, _ in _sdk_trees()}
    assert "observability/logger_adaptor.py" in paths


def test_no_opentelemetry_internal_imports() -> None:
    offenders = [
        f"{path.relative_to(_SDK_ROOT.parent)}:{line} imports {module}"
        for path, tree in _sdk_trees()
        for line, module in _internal_imports(tree)
    ]
    assert not offenders, (
        "Import OpenTelemetry from its public modules (e.g. "
        "opentelemetry.sdk._logs.export), not an _internal path a 1.x minor "
        "may move:\n" + "\n".join(offenders)
    )


def test_no_deprecated_log_record_trace_kwargs() -> None:
    offenders = [
        f"{path.relative_to(_SDK_ROOT.parent)}:{line} passes {kwarg}="
        for path, tree in _sdk_trees()
        for line, kwarg in _deprecated_log_record_calls(tree)
    ]
    assert not offenders, (
        "Build an OTel LogRecord with context= (a Context holding the span); "
        "trace_id=/span_id=/trace_flags= are deprecated since "
        "opentelemetry-api 1.35 and never warn at runtime:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from opentelemetry.sdk._logs._internal.export import X",
            [(1, "opentelemetry.sdk._logs._internal.export")],
        ),
        (
            "import opentelemetry.sdk._logs._internal",
            [(1, "opentelemetry.sdk._logs._internal")],
        ),
        (
            "def f():\n    from opentelemetry.sdk._logs import _internal",
            [(2, "opentelemetry.sdk._logs._internal")],
        ),
        ("from opentelemetry.sdk._logs.export import X", []),
        ("from mylib._internal import X", []),
    ],
)
def test_internal_import_detector(source: str, expected: list[tuple[int, str]]) -> None:
    assert _internal_imports(ast.parse(source)) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("LogRecord(trace_id=1, body='x')", [(1, "trace_id")]),
        (
            "_logs.LogRecord(span_id=1, trace_flags=f)",
            [(1, "span_id"), (1, "trace_flags")],
        ),
        (
            "from opentelemetry._logs import LogRecord as OTelLogRecord\n"
            "OTelLogRecord(trace_id=1, body='x')",
            [(2, "trace_id")],
        ),
        (
            "import opentelemetry._logs as otel_logs\n"
            "otel_logs.LogRecord(trace_flags=f)",
            [(2, "trace_flags")],
        ),
        (
            "from mylib import LogRecord as Other\nOther(trace_id=1)",
            [],
        ),
        ("LogRecord(context=ctx, body='x')", []),
        ("logging.LogRecord('n', 10, 'p', 1, 'm', None, None)", []),
    ],
)
def test_deprecated_kwarg_detector(
    source: str, expected: list[tuple[int, str]]
) -> None:
    assert _deprecated_log_record_calls(ast.parse(source)) == expected
