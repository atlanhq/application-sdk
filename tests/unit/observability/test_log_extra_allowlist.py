"""Structured log fields reach OTLP, or their drop is counted and reported.

``_build_extra_dict`` exports only keys in ``_KNOWN_EXTRA_KEYS`` or under a
passthrough prefix. These tests hold the SDK's own log calls to that allowlist
with a static scan, and pin the runtime behaviour for keys the scan cannot see:
each drop adds to the ``log.extra_key.dropped`` counter and is logged once per
key at DEBUG.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import application_sdk
from application_sdk.observability import logger_adaptor
from application_sdk.observability.logger_adaptor import (
    _KNOWN_EXTRA_KEYS,
    _PREFIXES_PASSTHROUGH,
    _build_extra_dict,
    get_logger,
)

_PACKAGE_ROOT = Path(application_sdk.__file__).parent

_LOG_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "log",
        "metric",
        "activity",
        "tracing",
    }
)

#: Functions that take a log call's fields as ``**kwargs`` and forward them.
_LOG_HELPERS = frozenset({"_safe_log"})

#: Keyword arguments the logging call consumes rather than binds as a field.
_CONSUMED_KWARGS = frozenset({"exc_info", "stack_info", "stacklevel"})

#: (file relative to the package, key) pairs allowed to be dropped.
_EXEMPT: frozenset[tuple[str, str]] = frozenset(
    {
        # ParquetFileWriter is deprecated; the delete-prefix debug line is left
        # as it is rather than allowlisting keys for a class being removed.
        ("storage/formats/parquet.py", "prefix"),
        ("storage/formats/parquet.py", "deleted_count"),
    }
)


def _receiver(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_receiver(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_receiver(node.func)}()"
    return ""


def _is_log_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _LOG_HELPERS
    if isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS:
        return "log" in _receiver(func.value).lower()
    return False


def _field_keys(call: ast.Call) -> Iterator[str]:
    """Each literal field name the call binds: keyword names, and the keys of a
    literal ``extra={...}`` dict (spread into fields by both stdlib loggers and
    ``AtlanLoggerAdapter``)."""
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in _CONSUMED_KWARGS:
            continue
        if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
            for key in keyword.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    yield key.value
            continue
        yield keyword.arg


def _exported(key: str) -> bool:
    return key in _KNOWN_EXTRA_KEYS or key.startswith(_PREFIXES_PASSTHROUGH)


def _dropped_fields(source: str, filename: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Call) and _is_log_call(node):
            for key in _field_keys(node):
                if not _exported(key) and (filename, key) not in _EXEMPT:
                    found.append(f"{filename}:{node.lineno} {key}")
    return found


class TestEveryLiteralLogFieldIsExported:
    def test_sdk_log_calls_only_pass_allowlisted_fields(self) -> None:
        dropped: list[str] = []
        for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
            relative = path.relative_to(_PACKAGE_ROOT).as_posix()
            dropped.extend(_dropped_fields(path.read_text(encoding="utf-8"), relative))
        assert not dropped, (
            "These log fields are neither in _KNOWN_EXTRA_KEYS nor under a "
            "passthrough prefix, so they never reach OTLP. Allowlist them in "
            "application_sdk/observability/logger_adaptor.py, rename them to an "
            "allowlisted key, or drop them:\n  " + "\n  ".join(dropped)
        )

    @pytest.mark.parametrize(
        "source",
        [
            "logger.info('x', not_allowlisted=1)",
            "self.logger.warning('x', not_allowlisted=1)",
            "workflow.logger.warning('x', extra={'not_allowlisted': 1})",
            "_log().debug('x', not_allowlisted=1)",
            "_safe_log('info', 'x', not_allowlisted=1)",
        ],
    )
    def test_scan_flags_a_dropped_field(self, source: str) -> None:
        assert _dropped_fields(source, "example.py") == ["example.py:1 not_allowlisted"]

    @pytest.mark.parametrize(
        "source",
        [
            "logger.info('x', workflow_id='w', exc_info=True)",
            "logger.info('x', extra={'atlan.anything': 1})",
            "logger.info('x', **fields)",
            "results.info('x', not_allowlisted=1)",
        ],
    )
    def test_scan_passes_exported_or_unknowable_fields(self, source: str) -> None:
        assert _dropped_fields(source, "example.py") == []


@pytest.fixture
def drop_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryMetricReader]:
    """The drop counter on an in-memory meter. The autouse
    ``_reset_dropped_log_keys`` fixture clears the drop bookkeeping."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        logger_adaptor,
        "_dropped_key_counter",
        provider.get_meter(logger_adaptor._METER_NAME).create_counter(
            logger_adaptor._DROPPED_KEY_METRIC
        ),
    )
    yield reader
    provider.shutdown()


def _dropped_counts(reader: InMemoryMetricReader) -> dict[str, int]:
    counts: dict[str, int] = {}
    data = reader.get_metrics_data()
    if data is None:
        return counts
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != logger_adaptor._DROPPED_KEY_METRIC:
                    continue
                for point in metric.data.data_points:
                    counts[str(point.attributes["key"])] = int(point.value)
    return counts


class TestDroppedFieldsAreCounted:
    def test_each_drop_adds_to_the_counter_by_key(
        self, drop_state: InMemoryMetricReader
    ) -> None:
        _build_extra_dict({"workflow_id": "w", "not_allowlisted": 1})
        _build_extra_dict({"not_allowlisted": 2, "also_not_allowlisted": 3})

        assert _dropped_counts(drop_state) == {
            "not_allowlisted": 2,
            "also_not_allowlisted": 1,
        }

    def test_exported_fields_and_empty_prefixed_fields_are_not_counted(
        self, drop_state: InMemoryMetricReader
    ) -> None:
        _build_extra_dict(
            {"logger_name": "x", "workflow_id": "w", "atlan.a": 1, "atlan.b": None}
        )

        assert _dropped_counts(drop_state) == {}

    def test_the_key_label_is_capped(
        self, drop_state: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(logger_adaptor, "_DROPPED_KEY_LABEL_CAP", 2)

        _build_extra_dict({"k1": 1, "k2": 1, "k3": 1, "k4": 1})
        _build_extra_dict({"k1": 1})

        assert _dropped_counts(drop_state) == {"k1": 2, "k2": 1, "_other": 2}

    def test_once_only_tracking_is_bounded(
        self,
        drop_state: InMemoryMetricReader,
        monkeypatch: pytest.MonkeyPatch,
        loguru_capture: list[dict[str, Any]],
    ) -> None:
        monkeypatch.setattr(logger_adaptor, "_DROPPED_KEY_TRACK_CAP", 2)
        monkeypatch.setattr(logger_adaptor, "_DROPPED_KEY_LABEL_CAP", 2)

        _build_extra_dict({"k1": 1, "k2": 1, "k3": 1, "k4": 1})
        get_logger("tests.log_extra_allowlist").info("drain")

        assert logger_adaptor._dropped_keys_seen == {"k1", "k2"}
        assert _dropped_counts(drop_state) == {"k1": 1, "k2": 1, "_other": 2}
        reported = [
            r["message"] for r in loguru_capture if "export allowlist" in r["message"]
        ]
        assert len(reported) == 2

    def test_the_counter_is_created_on_the_sdk_meter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        monkeypatch.setattr(logger_adaptor, "_dropped_key_counter", None)
        monkeypatch.setattr(
            "opentelemetry.metrics.get_meter",
            lambda name, *a, **k: provider.get_meter(name),
        )

        _build_extra_dict({"not_allowlisted": 1})

        assert _dropped_counts(reader) == {"not_allowlisted": 1}
        provider.shutdown()


class TestDroppedFieldsAreReportedOnce:
    def test_a_dropped_key_is_logged_once_at_debug(
        self, drop_state: InMemoryMetricReader, loguru_capture: list[dict[str, Any]]
    ) -> None:
        log = get_logger("tests.log_extra_allowlist")
        # The sink sees the record, but drops happen in ``_build_extra_dict``;
        # drive it the way the SDK sink does.
        _build_extra_dict({"not_allowlisted": 1})
        log.info("first")
        _build_extra_dict({"not_allowlisted": 2})
        log.info("second")

        reports = [
            r
            for r in loguru_capture
            if r["level"].name == "DEBUG" and "'not_allowlisted'" in r["message"]
        ]
        assert len(reports) == 1
        assert "_KNOWN_EXTRA_KEYS" in reports[0]["message"]


class TestStdlibExtraIsSpreadIntoFields:
    """The SDK installs ``AtlanLoggerAdapter`` as ``workflow.logger``, so callers
    written for a stdlib logger pass ``extra={...}``."""

    def test_extra_dict_entries_become_exported_fields(
        self, loguru_capture: list[dict[str, Any]]
    ) -> None:
        get_logger("tests.log_extra_allowlist").warning(
            "evicted",
            extra={"eviction_attempts": 1, "max_eviction_retries": 3},
        )

        record = next(r for r in loguru_capture if r["message"] == "evicted")
        exported = _build_extra_dict(record["extra"])
        assert exported["eviction_attempts"] == 1
        assert exported["max_eviction_retries"] == 3
        assert "extra" not in record["extra"]

    def test_an_explicit_field_wins_over_extra(
        self, loguru_capture: list[dict[str, Any]]
    ) -> None:
        get_logger("tests.log_extra_allowlist").info(
            "both", reason="explicit", extra={"reason": "from-extra"}
        )

        record = next(r for r in loguru_capture if r["message"] == "both")
        assert record["extra"]["reason"] == "explicit"


def test_input_summaries_are_never_exported() -> None:
    """``Input._log_summary`` filters by field name only; a value such as a URL
    with userinfo passes through it, so the summary must not reach OTLP or the
    object-store log sink under either name it has been logged as."""
    for key in ("input", "input_summary"):
        assert not _exported(key), key
