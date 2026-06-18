"""Tests for the L-series logging checks (L001–L021).

These checks are shipped in the conformance package and fanned out across the
fleet — a buggy check false-positives across hundreds of apps and triggers
spurious remediations.  Each rule is tested to fire *exactly* when it should
and stay silent otherwise: both false positives and false negatives are guarded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite.checks.logging import scan_all, scan_path, scan_text
from conformance.suite.checks.logging._constants import (
    CREDENTIAL_LABEL_SUFFIXES,
    CREDENTIAL_VALUE_SUFFIXES,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema import SarifReport, derive_disposition, validate_sarif
from conformance.suite.schema.disposition import Disposition, EnforcementTier

# Smoke-check that the constants are tuples (guards against silent import errors)
assert isinstance(CREDENTIAL_VALUE_SUFFIXES, tuple)
assert isinstance(CREDENTIAL_LABEL_SUFFIXES, tuple)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ids(src: str) -> list[str]:
    """Return sorted rule IDs of all active (non-suppressed) findings."""
    return sorted(f.rule_id for f in scan_text(src, "x.py") if not f.suppressed)


def _ids_unsorted(src: str) -> list[str]:
    """Return rule IDs in line order (for tests that check a specific finding exists)."""
    return [f.rule_id for f in scan_text(src, "x.py") if not f.suppressed]


def _scan_one(tmp_path: Path, src: str) -> list:
    """Write *src* to a single file and run the cross-file scanner."""
    p = tmp_path / "m.py"
    p.write_text(src)
    return scan_all([p], tmp_path)


def _scan_files(tmp_path: Path, files: dict[str, str]) -> list:
    """Write each entry in *files* and run the cross-file scanner."""
    paths: list[Path] = []
    for name, src in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)
        paths.append(path)
    return scan_all(paths, tmp_path)


# ---------------------------------------------------------------------------
# L001 FStringInLogMessage
# ---------------------------------------------------------------------------


def test_l001_fires_on_fstring_in_info() -> None:
    src = 'from loguru import logger\nlogger.info(f"value is {x}")\n'
    assert "L001" in _ids(src)


def test_l001_fires_on_fstring_in_error() -> None:
    src = 'from loguru import logger\nlogger.error(f"failed: {e}")\n'
    assert "L001" in _ids(src)


def test_l001_silent_on_pct_style() -> None:
    src = 'from loguru import logger\nlogger.info("value is %s", x)\n'
    assert "L001" not in _ids(src)


def test_l001_silent_on_plain_string() -> None:
    src = 'from loguru import logger\nlogger.info("hello world")\n'
    assert "L001" not in _ids(src)


def test_l001_block_tier() -> None:
    assert get_rule("L001").tier == EnforcementTier.BLOCK


# ---------------------------------------------------------------------------
# L003 ExtraKwargsWrongFramework
# ---------------------------------------------------------------------------


def test_l003_fires_for_structlog() -> None:
    src = 'import structlog\nlogger = structlog.get_logger()\nlogger.info("msg", extra={"k": "v"})\n'
    assert "L003" in _ids(src)


def test_l003_fires_for_loguru() -> None:
    src = 'from loguru import logger\nlogger.info("msg", extra={"k": "v"})\n'
    assert "L003" in _ids(src)


def test_l003_silent_for_stdlib() -> None:
    src = 'import logging\nlogger = logging.getLogger(__name__)\nlogger.info("msg", extra={"k": "v"})\n'
    assert "L003" not in _ids(src)


# ---------------------------------------------------------------------------
# L004 ExceptBlockMissingExcInfoLog
# ---------------------------------------------------------------------------


def test_l004_fires_on_error_without_exc_info() -> None:
    src = (
        "import logging\nlogger = logging.getLogger(__name__)\n"
        "try:\n    x()\nexcept Exception:\n    logger.error('failed')\n"
    )
    assert "L004" in _ids(src)


def test_l004_fires_on_warning_without_exc_info() -> None:
    src = (
        "import logging\nlogger = logging.getLogger(__name__)\n"
        "try:\n    x()\nexcept Exception:\n    logger.warning('degraded')\n"
    )
    assert "L004" in _ids(src)


def test_l004_silent_with_exc_info_true() -> None:
    src = (
        "import logging\nlogger = logging.getLogger(__name__)\n"
        "try:\n    x()\nexcept Exception:\n    logger.error('failed', exc_info=True)\n"
    )
    assert "L004" not in _ids(src)


def test_l004_silent_for_exception_method() -> None:
    # logger.exception() implicitly attaches exc_info — exempt from L004
    src = (
        "import logging\nlogger = logging.getLogger(__name__)\n"
        "try:\n    x()\nexcept Exception:\n    logger.exception('failed')\n"
    )
    assert "L004" not in _ids(src)


def test_l004_block_tier() -> None:
    assert get_rule("L004").tier == EnforcementTier.BLOCK


# ---------------------------------------------------------------------------
# L005 PrintInProductionCode
# ---------------------------------------------------------------------------


def test_l005_fires_on_bare_print() -> None:
    assert "L005" in _ids('print("hello")\n')


def test_l005_silent_in_main_guard() -> None:
    src = 'if __name__ == "__main__":\n    print("main output")\n'
    assert "L005" not in _ids(src)


def test_l005_suppressed_with_directive() -> None:
    src = 'print("cli output")  # conformance: ignore[L005] CLI progress indicator\n'
    findings = scan_text(src, "x.py")
    assert all(f.suppressed for f in findings if f.rule_id == "L005")


# ---------------------------------------------------------------------------
# L006 InfoInTightLoop
# ---------------------------------------------------------------------------


def test_l006_fires_in_unbounded_for() -> None:
    src = (
        "from loguru import logger\nfor item in items:\n    logger.info('processing')\n"
    )
    assert "L006" in _ids(src)


def test_l006_fires_in_large_range() -> None:
    src = "from loguru import logger\nfor i in range(100):\n    logger.info('item')\n"
    assert "L006" in _ids(src)


def test_l006_fires_in_while_loop() -> None:
    src = "from loguru import logger\nwhile True:\n    logger.info('tick')\n"
    assert "L006" in _ids(src)


def test_l006_silent_in_bounded_range() -> None:
    src = "from loguru import logger\nfor i in range(5):\n    logger.info('item')\n"
    assert "L006" not in _ids(src)


def test_l006_silent_for_debug_in_loop() -> None:
    src = "from loguru import logger\nfor item in items:\n    logger.debug('processing')\n"
    assert "L006" not in _ids(src)


# ---------------------------------------------------------------------------
# L007 LoggerCriticalUsage
# ---------------------------------------------------------------------------


def test_l007_fires_on_critical() -> None:
    src = "from loguru import logger\nlogger.critical('fatal!')\n"
    assert "L007" in _ids(src)


def test_l007_silent_on_error() -> None:
    src = "from loguru import logger\nlogger.error('failed')\n"
    assert "L007" not in _ids(src)


# ---------------------------------------------------------------------------
# L008 UnguardedExpensiveDebug
# ---------------------------------------------------------------------------


def test_l008_fires_on_repr_in_debug() -> None:
    src = "from loguru import logger\ndef f(x):\n    logger.debug('data', repr(x))\n"
    assert "L008" in _ids(src)


def test_l008_fires_on_model_dump_in_debug() -> None:
    src = "from loguru import logger\ndef f(obj):\n    logger.debug('state', obj.model_dump())\n"
    assert "L008" in _ids(src)


def test_l008_silent_on_plain_debug() -> None:
    src = "from loguru import logger\ndef f(x):\n    logger.debug('hello %s', x)\n"
    assert "L008" not in _ids(src)


@pytest.mark.parametrize("level", ["info", "warning", "error", "critical"])
def test_l008_silent_for_non_debug_level(level: str) -> None:
    # Expensive calls in non-debug levels are not flagged — the guard
    # requirement only applies to logger.debug() which is a no-op in production.
    src = f"from loguru import logger\ndef f(obj):\n    logger.{level}('state', obj.model_dump())\n"
    assert "L008" not in _ids(src)


# ---------------------------------------------------------------------------
# L009 WarnThenRaiseDuplication
# ---------------------------------------------------------------------------


def test_l009_fires_on_warning_before_raise() -> None:
    src = (
        "from loguru import logger\n"
        "def f():\n"
        "    logger.warning('about to fail')\n"
        "    raise ValueError('bad')\n"
    )
    assert "L009" in _ids(src)


def test_l009_fires_on_error_before_raise() -> None:
    src = (
        "from loguru import logger\n"
        "def f():\n"
        "    logger.error('oops')\n"
        "    raise RuntimeError('oops')\n"
    )
    assert "L009" in _ids(src)


def test_l009_silent_when_not_adjacent() -> None:
    # More than 3 statements between log and raise
    src = (
        "from loguru import logger\n"
        "def f():\n"
        "    logger.warning('starting')\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
        "    raise ValueError('unrelated raise')\n"
    )
    assert "L009" not in _ids(src)


def test_l009_silent_on_debug_before_raise() -> None:
    src = (
        "from loguru import logger\n"
        "def f():\n"
        "    logger.debug('context')\n"
        "    raise ValueError('bad')\n"
    )
    assert "L009" not in _ids(src)


def test_l009_suppressed_with_directive() -> None:
    src = (
        "from loguru import logger\n"
        "def f():\n"
        "    # conformance: ignore[L009] adds context not visible to caller\n"
        "    logger.warning('retrying with fallback config')\n"
        "    raise ValueError('bad')\n"
    )
    findings = scan_text(src, "x.py")
    l009 = [f for f in findings if f.rule_id == "L009"]
    assert all(f.suppressed for f in l009)


# ---------------------------------------------------------------------------
# L010 CredentialInLogOutput
# ---------------------------------------------------------------------------


def test_l010_fires_on_password_kwarg() -> None:
    src = "from loguru import logger\nlogger.error('auth failed', password=pwd)\n"
    assert "L010" in _ids(src)


def test_l010_fires_on_token_positional_arg() -> None:
    src = "from loguru import logger\nlogger.warning('using %s', token)\n"
    assert "L010" in _ids(src)


@pytest.mark.parametrize("suffix", CREDENTIAL_VALUE_SUFFIXES)
def test_l010_fires_on_all_value_suffixes(suffix: str) -> None:
    # Every entry in CREDENTIAL_VALUE_SUFFIXES must trigger L010 as a kwarg.
    src = f"from loguru import logger\nlogger.error('auth failed', {suffix}=val)\n"
    assert "L010" in _ids(src)


def test_l010_silent_on_credential_name_label() -> None:
    # token_name is a label, not a value
    src = "from loguru import logger\nlogger.info('using credential %s', token_name)\n"
    assert "L010" not in _ids(src)


@pytest.mark.parametrize("suffix", CREDENTIAL_LABEL_SUFFIXES)
def test_l010_silent_on_all_label_suffixes(suffix: str) -> None:
    # Every entry in CREDENTIAL_LABEL_SUFFIXES must NOT trigger L010.
    name = f"credential{suffix}"
    src = f"from loguru import logger\nlogger.info('using %s', {name})\n"
    assert "L010" not in _ids(src)


def test_l010_silent_for_non_logger_call() -> None:
    # L010 must only fire on recognised logger calls; arbitrary function calls
    # that happen to carry credential-named kwargs must not produce findings.
    src = "db.connect(password=secret)\n"
    assert "L010" not in _ids(src)


def test_l010_block_tier() -> None:
    assert get_rule("L010").tier == EnforcementTier.BLOCK


# ---------------------------------------------------------------------------
# L011 StringConcatenationInLog
# ---------------------------------------------------------------------------


def test_l011_fires_on_concat_in_info() -> None:
    src = 'from loguru import logger\nlogger.info("val: " + x)\n'
    assert "L011" in _ids(src)


def test_l011_fires_on_chained_concat_in_error() -> None:
    # Chained concat (BinOp whose left is also a BinOp) must still fire.
    src = 'from loguru import logger\nlogger.error("val: " + x + " end")\n'
    assert "L011" in _ids(src)


def test_l011_silent_for_concat_in_non_first_arg() -> None:
    # Concat in a format argument (not the message itself) must NOT fire —
    # L011 only targets the message/format string position (args[0]).
    src = 'from loguru import logger\nlogger.info("msg: %s", "prefix" + x)\n'
    assert "L011" not in _ids(src)


def test_l011_silent_on_pct_style() -> None:
    src = 'from loguru import logger\nlogger.info("val: %s", x)\n'
    assert "L011" not in _ids(src)


def test_l011_suppressed_by_inline_directive() -> None:
    src = 'from loguru import logger\nlogger.info("val: " + x)  # conformance: ignore[L011] intentional\n'
    findings = scan_text(src, "x.py")
    l011 = [f for f in findings if f.rule_id == "L011"]
    assert l011 and all(f.suppressed for f in l011)


def test_l011_block_tier() -> None:
    assert get_rule("L011").tier == EnforcementTier.BLOCK


# ---------------------------------------------------------------------------
# L012 StdlibExtraReservedKeyCollision
# ---------------------------------------------------------------------------


def test_l012_fires_on_reserved_key() -> None:
    src = (
        "import logging\nlogger = logging.getLogger(__name__)\n"
        'logger.info("msg", extra={"name": "alice"})\n'
    )
    assert "L012" in _ids(src)


def test_l012_fires_on_module_key() -> None:
    src = (
        "import logging\nlogger = logging.getLogger(__name__)\n"
        'logger.info("msg", extra={"module": "mymod"})\n'
    )
    assert "L012" in _ids(src)


def test_l012_silent_on_safe_key() -> None:
    src = (
        "import logging\nlogger = logging.getLogger(__name__)\n"
        'logger.info("msg", extra={"user_id": 42})\n'
    )
    assert "L012" not in _ids(src)


def test_l012_silent_for_non_stdlib() -> None:
    # L012 is stdlib-only
    src = 'from loguru import logger\nlogger.info("msg", extra={"name": "alice"})\n'
    assert "L012" not in _ids(src)


def test_l012_block_tier() -> None:
    assert get_rule("L012").tier == EnforcementTier.BLOCK


# ---------------------------------------------------------------------------
# L013 StdlibArbitraryKwargs
# ---------------------------------------------------------------------------


def test_l013_fires_on_custom_kwarg() -> None:
    src = (
        "import logging\nlogger = logging.getLogger(__name__)\n"
        'logger.info("msg", user_id=123)\n'
    )
    assert "L013" in _ids(src)


def test_l013_silent_for_exc_info() -> None:
    src = (
        "import logging\nlogger = logging.getLogger(__name__)\n"
        'logger.error("msg", exc_info=True)\n'
    )
    assert "L013" not in _ids(src)


def test_l013_silent_for_extra() -> None:
    src = (
        "import logging\nlogger = logging.getLogger(__name__)\n"
        'logger.info("msg", extra={"k": "v"})\n'
    )
    assert "L013" not in _ids(src)


def test_l013_silent_for_non_stdlib() -> None:
    # L013 is stdlib-only; non-stdlib fires L018 instead
    src = 'from loguru import logger\nlogger.info("msg", user_id=123)\n'
    assert "L013" not in _ids(src)


def test_l013_block_tier() -> None:
    assert get_rule("L013").tier == EnforcementTier.BLOCK


# ---------------------------------------------------------------------------
# L014 StructlogEventKwargOverwrite
# ---------------------------------------------------------------------------


def test_l014_fires_on_event_kwarg() -> None:
    src = 'import structlog\nlogger = structlog.get_logger()\nlogger.info("msg", event=x)\n'
    assert "L014" in _ids(src)


def test_l014_silent_for_non_structlog() -> None:
    src = 'from loguru import logger\nlogger.info("msg", event=x)\n'
    assert "L014" not in _ids(src)


# ---------------------------------------------------------------------------
# L015 DictConfigDisableExistingLoggers
# ---------------------------------------------------------------------------


def test_l015_fires_when_key_absent() -> None:
    src = (
        "import logging.config\n"
        'logging.config.dictConfig({"version": 1, "handlers": {}})\n'
    )
    assert "L015" in _ids(src)


def test_l015_fires_when_key_true() -> None:
    src = (
        "import logging.config\n"
        'logging.config.dictConfig({"version": 1, "disable_existing_loggers": True})\n'
    )
    assert "L015" in _ids(src)


def test_l015_silent_when_key_false() -> None:
    src = (
        "import logging.config\n"
        'logging.config.dictConfig({"version": 1, "disable_existing_loggers": False})\n'
    )
    assert "L015" not in _ids(src)


def test_l015_silent_for_variable_arg() -> None:
    # Can't analyse statically — skip to avoid false positives
    src = "import logging.config\nlogging.config.dictConfig(config)\n"
    assert "L015" not in _ids(src)


# ---------------------------------------------------------------------------
# L016 BasicConfigNoopAfterFirstCall — cross-file
# ---------------------------------------------------------------------------


def test_l016_silent_for_single_call(tmp_path: Path) -> None:
    src = "import logging\nlogging.basicConfig(level=logging.INFO)\n"
    findings = _scan_one(tmp_path, src)
    assert not any(f.rule_id == "L016" for f in findings)


def test_l016_fires_for_two_calls_across_files(tmp_path: Path) -> None:
    files = {
        "a.py": "import logging\nlogging.basicConfig(level=logging.INFO)\n",
        "b.py": "import logging\nlogging.basicConfig(format='%(message)s')\n",
    }
    findings = _scan_files(tmp_path, files)
    l016 = [f for f in findings if f.rule_id == "L016"]
    assert len(l016) == 1  # only the 2nd call is flagged


def test_l016_silent_for_main_block_call(tmp_path: Path) -> None:
    src = (
        "import logging\n"
        'if __name__ == "__main__":\n'
        "    logging.basicConfig(level=logging.DEBUG)\n"
    )
    findings = _scan_one(tmp_path, src)
    assert not any(f.rule_id == "L016" for f in findings)


# ---------------------------------------------------------------------------
# L017 LoggerExceptionUsage
# ---------------------------------------------------------------------------


def test_l017_fires_on_exception_call() -> None:
    src = (
        "from loguru import logger\n"
        "try:\n    x()\nexcept Exception:\n    logger.exception('failed')\n"
    )
    assert "L017" in _ids(src)


def test_l017_silent_in_adapter_file() -> None:
    # Files that define AtlanLoggerAdapter are the adapter itself — exempt.
    # The source includes a real logger.exception() call so the exemption is
    # actually exercised (vacuous tests that can't fire prove nothing).
    src = (
        "from loguru import logger\n"
        "class AtlanLoggerAdapter:\n"
        "    def exception(self, msg, *args, **kwargs):\n"
        "        self.error(msg, *args, exc_info=True, **kwargs)\n"
        "try:\n"
        "    x()\n"
        "except Exception:\n"
        "    logger.exception('failed in adapter')\n"
    )
    assert "L017" not in _ids(src)


def test_l017_silent_for_error_method() -> None:
    src = "from loguru import logger\nlogger.error('failed', exc_info=True)\n"
    assert "L017" not in _ids(src)


# ---------------------------------------------------------------------------
# L018 KwargsInApplicationLogCalls
# ---------------------------------------------------------------------------


def test_l018_fires_for_loguru_kwargs() -> None:
    src = "from loguru import logger\nlogger.info('msg', user_id=123)\n"
    assert "L018" in _ids(src)


def test_l018_silent_for_exc_info() -> None:
    src = "from loguru import logger\nlogger.error('failed', exc_info=True)\n"
    assert "L018" not in _ids(src)


def test_l018_silent_for_stdlib() -> None:
    # stdlib fires L013 instead
    src = "import logging\nlogger = logging.getLogger(__name__)\nlogger.info('msg')\n"
    assert "L018" not in _ids(src)


def test_l018_silent_in_adapter_file() -> None:
    # get_logger definition marks this as the adapter file — exempt.
    # The source includes a real loguru call with non-allowlist kwargs so
    # the exemption is actually exercised (not a vacuous non-firing test).
    src = (
        "from loguru import logger\n"
        "def get_logger(name):\n"
        "    return logger.bind(name=name)\n"
        "logger.info('msg', user_id=123)\n"
    )
    assert "L018" not in _ids(src)


# ---------------------------------------------------------------------------
# L019 DiscardedBindResult
# ---------------------------------------------------------------------------


def test_l019_fires_on_bare_bind() -> None:
    src = "from loguru import logger\nlogger.bind(user='alice')\n"
    assert "L019" in _ids(src)


def test_l019_silent_when_assigned() -> None:
    src = "from loguru import logger\nlog = logger.bind(user='alice')\n"
    assert "L019" not in _ids(src)


def test_l019_silent_when_chained() -> None:
    src = "from loguru import logger\nlogger.bind(user='alice').info('msg')\n"
    # The bind result is used (chained) — not a bare expression
    assert "L019" not in _ids(src)


# ---------------------------------------------------------------------------
# L020 DeprecatedLoggingWarn
# ---------------------------------------------------------------------------


def test_l020_fires_on_logger_warn() -> None:
    src = "from loguru import logger\nlogger.warn('deprecated')\n"
    assert "L020" in _ids(src)


def test_l020_fires_on_logging_warn() -> None:
    src = "import logging\nlogging.warn('deprecated')\n"
    assert "L020" in _ids(src)


def test_l020_fires_on_logging_module_alias() -> None:
    # import logging as L; L.warn(...) must fire — L is an alias for logging
    src = "import logging as L\nL.warn('deprecated')\n"
    assert "L020" in _ids(src)


def test_l020_fires_on_bare_warn_from_import() -> None:
    # from logging import warn; warn(...) must fire
    src = "from logging import warn\nwarn('deprecated')\n"
    assert "L020" in _ids(src)


def test_l020_silent_on_warning() -> None:
    src = "from loguru import logger\nlogger.warning('correct')\n"
    assert "L020" not in _ids(src)


# ---------------------------------------------------------------------------
# L004 duplicate-finding regression
# ---------------------------------------------------------------------------


def test_l004_no_duplicate_in_nested_try() -> None:
    # A logger.error() inside a nested try/except within an outer except block
    # must produce exactly one L004 finding, not two (one from each handler).
    src = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "try:\n"
        "    x()\n"
        "except ValueError:\n"
        "    try:\n"
        "        y()\n"
        "    except TypeError:\n"
        "        logger.error('inner')\n"
    )
    findings = [f for f in scan_text(src, "x.py") if f.rule_id == "L004"]
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# Framework gating (key cross-rule checks)
# ---------------------------------------------------------------------------


def test_stdlib_only_rules_silent_for_structlog() -> None:
    """L012, L013, L015 must not fire when the framework is structlog."""
    src = (
        "import structlog\n"
        "logger = structlog.get_logger()\n"
        "logger.info('msg', user_id=123)\n"
        'logger.info("msg", extra={"name": "alice"})\n'
    )
    ids = _ids(src)
    assert "L012" not in ids
    assert "L013" not in ids


def test_l014_only_fires_for_structlog() -> None:
    """event= kwarg is normal in loguru; only structlog has the overwrite problem."""
    structlog_src = "import structlog\nlogger = structlog.get_logger()\nlogger.info('msg', event=x)\n"
    loguru_src = "from loguru import logger\nlogger.info('msg', event=x)\n"
    assert "L014" in _ids(structlog_src)
    assert "L014" not in _ids(loguru_src)


# ---------------------------------------------------------------------------
# Inline suppression
# ---------------------------------------------------------------------------


def test_suppression_on_same_line() -> None:
    src = 'from loguru import logger\nlogger.info(f"hi {x}")  # conformance: ignore[L001] intentional\n'
    findings = scan_text(src, "x.py")
    l001 = [f for f in findings if f.rule_id == "L001"]
    assert l001 and all(f.suppressed for f in l001)


def test_suppression_on_line_above() -> None:
    src = (
        "from loguru import logger\n"
        "# conformance: ignore[L001] dynamic message needed\n"
        'logger.info(f"hi {x}")\n'
    )
    findings = scan_text(src, "x.py")
    l001 = [f for f in findings if f.rule_id == "L001"]
    assert l001 and all(f.suppressed for f in l001)


def test_suppression_requires_justification() -> None:
    # Bare directive with no justification text is not honoured
    src = 'from loguru import logger\nlogger.info(f"hi {x}")  # conformance: ignore[L001]\n'
    findings = scan_text(src, "x.py")
    l001 = [f for f in findings if f.rule_id == "L001"]
    assert l001 and all(not f.suppressed for f in l001)


# ---------------------------------------------------------------------------
# SARIF pipeline + disposition
# ---------------------------------------------------------------------------


def test_sarif_roundtrip(tmp_path: Path) -> None:
    """Findings round-trip through the SARIF builder and validate against schema."""
    from conformance.suite.schema.findings import findings_to_report

    src = (
        "from loguru import logger\n"
        'logger.info(f"broken {x}")\n'
        'logger.critical("critical")\n'
    )
    findings = scan_text(src, "x.py")
    assert findings

    report = findings_to_report(findings, tool_version="0.0.0")
    try:
        validate_sarif(report)
    except ImportError:
        pytest.skip("jsonschema not installed — run: uv sync --group conformance")

    payload = json.dumps(report.model_dump(by_alias=True, exclude_none=True))
    loaded = SarifReport.model_validate(json.loads(payload))
    assert loaded.runs

    run = loaded.runs[0]
    # Tool name must identify the conformance suite driver
    assert run.tool.driver.name == "atlan-conformance"
    # At least L001 (f-string) and L007 (critical) must appear
    result_rule_ids = {r.rule_id for r in (run.results or [])}
    assert "L001" in result_rule_ids
    assert "L007" in result_rule_ids
    # Finding count matches what scan_text returned
    active = [f for f in findings if not f.suppressed]
    assert len(run.results or []) == len(active)


def test_l001_block_disposition_failing(tmp_path: Path) -> None:
    """L001 is BLOCK-tier — active findings must map to FAILING disposition."""
    from conformance.suite.schema.findings import findings_to_report

    src = 'from loguru import logger\nlogger.info(f"broken {x}")\n'
    findings = [f for f in scan_text(src, "x.py") if not f.suppressed]
    assert findings

    report = findings_to_report(findings, tool_version="0.0.0")
    results = report.runs[0].results or []
    l001_results = [r for r in results if r.rule_id == "L001"]
    assert l001_results
    for result in l001_results:
        disp = derive_disposition(result)
        assert disp == Disposition.FAILING


def test_l007_warn_disposition(tmp_path: Path) -> None:
    """L007 is WARN-tier — active findings must map to WARNING disposition."""
    from conformance.suite.schema.findings import findings_to_report

    src = "from loguru import logger\nlogger.critical('fatal')\n"
    findings = [f for f in scan_text(src, "x.py") if not f.suppressed]

    report = findings_to_report(findings, tool_version="0.0.0")
    results = report.runs[0].results or []
    l007_results = [r for r in results if r.rule_id == "L007"]
    assert l007_results
    for result in l007_results:
        disp = derive_disposition(result)
        assert disp == Disposition.WARNING


# ---------------------------------------------------------------------------
# L002 NonCanonicalLoggerFactory — absolute SDK adapter enforcement
# ---------------------------------------------------------------------------

_SDK_LOGGER = (
    "from application_sdk.observability.logger_adaptor import get_logger\n"
    "logger = get_logger(__name__)\n"
)


def test_l002_silent_sdk_adapter(tmp_path: Path) -> None:
    files = {"a.py": _SDK_LOGGER, "b.py": _SDK_LOGGER}
    findings = _scan_files(tmp_path, files)
    assert not any(f.rule_id == "L002" for f in findings)


def test_l002_fires_stdlib_getlogger(tmp_path: Path) -> None:
    src = "import logging\nlogger = logging.getLogger(__name__)\n"
    findings = _scan_files(tmp_path, {"a.py": src})
    l002 = [f for f in findings if f.rule_id == "L002"]
    assert l002
    assert "stdlib" in l002[0].message


def test_l002_fires_structlog(tmp_path: Path) -> None:
    src = "import structlog\nlogger = structlog.get_logger()\n"
    findings = _scan_files(tmp_path, {"a.py": src})
    assert any(f.rule_id == "L002" for f in findings)


def test_l002_fires_loguru_direct(tmp_path: Path) -> None:
    src = "from loguru import logger\n"
    findings = _scan_files(tmp_path, {"a.py": src})
    assert any(f.rule_id == "L002" for f in findings)


def test_l002_silent_adapter_definition_file(tmp_path: Path) -> None:
    """The file defining get_logger is exempt even though it uses loguru."""
    src = (
        "from loguru import logger\n"
        "def get_logger(name: str):\n"
        "    return logger.bind(name=name)\n"
    )
    findings = _scan_files(tmp_path, {"logger_adaptor.py": src})
    assert not any(f.rule_id == "L002" for f in findings)


def test_l002_fires_mixed_only_non_sdk_flagged(tmp_path: Path) -> None:
    """SDK-adapter files are silent; non-canonical files are each flagged."""
    files = {
        "a.py": _SDK_LOGGER,
        "b.py": _SDK_LOGGER,
        "c.py": "import logging\nlogger = logging.getLogger(__name__)\n",
    }
    findings = _scan_files(tmp_path, files)
    l002 = [f for f in findings if f.rule_id == "L002"]
    assert len(l002) == 1
    assert "c.py" in l002[0].file


def test_l002_suppression_directive(tmp_path: Path) -> None:
    src = (
        "# conformance: ignore[L002] legacy module, migrating in next sprint\n"
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
    )
    findings = _scan_files(tmp_path, {"legacy.py": src})
    l002 = [f for f in findings if f.rule_id == "L002"]
    assert all(f.suppressed for f in l002)


# ---------------------------------------------------------------------------
# scan_path round-trip
# ---------------------------------------------------------------------------


def test_scan_path_matches_scan_text(tmp_path: Path) -> None:
    src = 'from loguru import logger\nlogger.info(f"hi {x}")\n'
    path = tmp_path / "m.py"
    path.write_text(src)
    from_path = scan_path(path, tmp_path)
    from_text = scan_text(src, "m.py")
    assert [f.rule_id for f in from_path] == [f.rule_id for f in from_text]


# ---------------------------------------------------------------------------
# L021 MissingLoggingLintRules — pyproject.toml ruff config
# ---------------------------------------------------------------------------


def _l021_findings(tmp_path: Path, toml_content: str) -> list:
    """Write a pyproject.toml and run scan_all with no Python files."""
    (tmp_path / "pyproject.toml").write_text(toml_content)
    return [f for f in scan_all([], tmp_path) if f.rule_id == "L021"]


def test_l021_fires_when_no_ruff_config(tmp_path: Path) -> None:
    toml = '[project]\nname = "my-app"\n'
    findings = _l021_findings(tmp_path, toml)
    assert findings
    assert findings[0].rule_id == "L021"


def test_l021_fires_when_rules_missing(tmp_path: Path) -> None:
    toml = '[project]\nname = "my-app"\n[tool.ruff.lint]\nselect = ["E", "F"]\n'
    findings = _l021_findings(tmp_path, toml)
    assert findings
    msg = findings[0].message
    assert "G001" in msg
    assert "G004" in msg
    assert "T201" in msg


def test_l021_silent_all_in_select(tmp_path: Path) -> None:
    toml = (
        '[project]\nname = "my-app"\n'
        "[tool.ruff.lint]\n"
        'select = ["ALL"]\n'
        'ignore = ["ANN"]\n'
    )
    assert not _l021_findings(tmp_path, toml)


def test_l021_silent_category_prefix(tmp_path: Path) -> None:
    """Selecting a category prefix (G, T2, LOG) covers all rules in that group."""
    toml = (
        '[project]\nname = "my-app"\n'
        "[tool.ruff.lint]\n"
        'select = ["E", "F", "G", "T2", "LOG"]\n'
    )
    assert not _l021_findings(tmp_path, toml)


def test_l021_silent_exact_rule_ids(tmp_path: Path) -> None:
    toml = (
        '[project]\nname = "my-app"\n'
        "[tool.ruff.lint]\n"
        'select = ["E", "F", "G001", "G003", "G004", "T201", "LOG009"]\n'
    )
    assert not _l021_findings(tmp_path, toml)


def test_l021_fires_when_rule_explicitly_ignored(tmp_path: Path) -> None:
    """A rule that is selected but then ignored is not covered."""
    toml = (
        '[project]\nname = "my-app"\n'
        "[tool.ruff.lint]\n"
        'select = ["ALL"]\n'
        'ignore = ["G004"]\n'
    )
    findings = _l021_findings(tmp_path, toml)
    assert findings
    assert "G004" in findings[0].message


def test_l021_silent_extend_select(tmp_path: Path) -> None:
    """Rules in extend-select also count as covered."""
    toml = (
        '[project]\nname = "my-app"\n'
        "[tool.ruff.lint]\n"
        'select = ["E", "F"]\n'
        'extend-select = ["G001", "G003", "G004", "T201", "LOG009"]\n'
    )
    assert not _l021_findings(tmp_path, toml)


def test_l021_silent_sdk_self_check_exempt(tmp_path: Path) -> None:
    """The SDK's own pyproject.toml is exempt from L021."""
    toml = '[project]\nname = "atlan-application-sdk"\n'
    assert not _l021_findings(tmp_path, toml)


def test_l021_silent_no_pyproject(tmp_path: Path) -> None:
    """No pyproject.toml → no L021 finding (not every repo has one)."""
    findings = [f for f in scan_all([], tmp_path) if f.rule_id == "L021"]
    assert not findings


def test_l021_partial_coverage_lists_only_missing(tmp_path: Path) -> None:
    """When some rules are present, only the missing ones are reported."""
    toml = (
        '[project]\nname = "my-app"\n'
        "[tool.ruff.lint]\n"
        'select = ["E", "G", "LOG"]\n'  # covers G001/G003/G004 and LOG009, missing T201
    )
    findings = _l021_findings(tmp_path, toml)
    assert findings
    msg = findings[0].message
    assert "T201" in msg
    assert "G001" not in msg
    assert "G004" not in msg
    assert "LOG009" not in msg


# ---------------------------------------------------------------------------
# Regression tests for bugs fixed after initial review
# ---------------------------------------------------------------------------


def test_l020_fires_on_self_logger_warn() -> None:
    """self.logger.warn(...) must trigger L020 — one-level Attribute receiver."""
    src = "class Foo:\n    def bar(self):\n        self.logger.warn('x')\n"
    assert _ids(src) == ["L020"]


def test_l020_fires_on_cls_logger_warn() -> None:
    """cls._logger.warn(...) must trigger L020 — one-level Attribute receiver."""
    src = "class Foo:\n    def bar(cls):\n        cls._logger.warn('x')\n"
    assert _ids(src) == ["L020"]


def test_l013_fires_on_aliased_logging_module() -> None:
    """import logging as L; L.info('hi', custom=1) must fire L013.

    Regression: is_logger_call hard-coded obj.id == 'logging', so aliased
    imports silently bypassed every rule that routes through it.
    """
    src = "import logging as L\nL.info('hi', custom=1)\n"
    assert "L013" in _ids(src)


def test_l009_fires_on_aliased_logging_module() -> None:
    """import logging as L; L.error('failed'); raise must fire L009.

    Regression: WarnThenRaiseVisitor / _is_warn_or_error_log_stmt fell back to
    the default frozenset({'logging'}), so aliased module names were silently
    skipped by get_logger_method.
    """
    src = (
        "import logging as L\n"
        "def f():\n"
        "    L.error('failed')\n"
        "    raise ValueError('x')\n"
    )
    assert "L009" in _ids(src)


def test_l004_fires_in_inner_try_body_inside_except() -> None:
    """logger.error() in an inner try-body inside an outer except must fire L004.

    Regression: _walk_no_scope pruned ast.Try, so an inner try-body inside an
    outer except handler was unreachable and silently missed.
    """
    src = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "try:\n"
        "    pass\n"
        "except Exception:\n"
        "    try:\n"
        "        logger.error('inner')\n"
        "    finally:\n"
        "        pass\n"
    )
    assert "L004" in _ids(src)
