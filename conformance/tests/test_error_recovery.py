"""Tests for P-series error-recovery checks (suite/checks/error_recovery.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from suite.checks.error_recovery import (
    BUILTIN_RAISES,
    LEAF_CLASSES,
    LEGACY_ATLAN_ERRORS,
    is_broad_suppress,
    is_builtin_raise,
    main,
    parse_ignore_directive,
    scan_text,
)
from suite.schema import SarifReport, derive_disposition, validate_sarif
from suite.schema.disposition import Disposition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _findings(src: str) -> list[str]:
    """Return sorted rule_id list from scan_text."""
    return sorted(f.rule_id for f in scan_text(src, "fake.py"))


def _single(src: str, rule_id: str) -> None:
    """Assert exactly one finding with the given rule_id."""
    found = _findings(src)
    assert found == [rule_id], f"Expected [{rule_id}], got {found!r}\nSource:\n{src}"


def _none(src: str) -> None:
    """Assert no active (non-suppressed) findings."""
    active = [f for f in scan_text(src, "fake.py") if not f.suppressed]
    assert not active, (
        f"Expected no findings, got {[f.rule_id for f in active]!r}\nSource:\n{src}"
    )


def _suppressed(src: str, rule_id: str) -> None:
    """Assert the finding exists but is suppressed."""
    fs = scan_text(src, "fake.py")
    matching = [f for f in fs if f.rule_id == rule_id]
    assert matching, f"No {rule_id} finding at all"
    assert all(f.suppressed for f in matching), f"{rule_id} finding is not suppressed"


# ── parse_ignore_directive ────────────────────────────────────────────────────


def test_parse_directive_with_rule_ids() -> None:
    d = parse_ignore_directive("# conformance: ignore[P001,P002] some reason")
    assert d is not None
    assert d.rule_ids == frozenset({"P001", "P002"})
    assert d.justification == "some reason"


def test_parse_directive_without_rule_ids() -> None:
    d = parse_ignore_directive("# conformance: ignore intentional")
    assert d is not None
    assert d.rule_ids is None
    assert d.justification == "intentional"


def test_parse_directive_no_match() -> None:
    assert parse_ignore_directive("# just a normal comment") is None
    assert parse_ignore_directive("# type: ignore") is None


def test_parse_directive_case_insensitive() -> None:
    d = parse_ignore_directive("# CONFORMANCE: IGNORE[P003] ok")
    assert d is not None
    assert "P003" in (d.rule_ids or set())


# ── is_broad_suppress / is_builtin_raise truth tables ────────────────────────


@pytest.mark.parametrize(
    "src,expected",
    [
        ("suppress(Exception)", True),
        ("suppress(BaseException)", True),
        ("suppress(FileNotFoundError)", False),
        ("suppress(OSError, FileNotFoundError)", False),
        # contextlib.suppress(...) — _get_name returns the attr "suppress", so it IS matched
        ("contextlib.suppress(Exception)", True),
        ("contextlib.suppress(FileNotFoundError)", False),
    ],
)
def test_is_broad_suppress(src: str, expected: bool) -> None:
    import ast

    tree = ast.parse(src, mode="eval")
    assert isinstance(tree.body, ast.Call)
    assert is_broad_suppress(tree.body) == expected


@pytest.mark.parametrize(
    "src,expected",
    [
        ("raise ValueError('bad')", True),
        ("raise RuntimeError()", True),
        ("raise Exception('oops')", True),
        ("raise NotImplementedError()", True),
        ("raise OSError()", True),
        ("raise KeyError('k')", True),
        ("raise LookupError()", True),
        ("raise TypeError('t')", True),
        ("raise InternalError()", False),
        ("raise", False),  # bare re-raise has exc=None
    ],
)
def test_is_builtin_raise(src: str, expected: bool) -> None:
    import ast

    tree = ast.parse(src)
    raise_node = tree.body[0]
    assert isinstance(raise_node, ast.Raise)
    assert is_builtin_raise(raise_node) == expected


# ── P001 — BareExceptPass ─────────────────────────────────────────────────────


def test_p001_bare_except_pass() -> None:
    _single(
        """\
try:
    do_something()
except:
    pass
""",
        "P001",
    )


def test_p001_no_finding_typed_except_pass() -> None:
    # P002, not P001
    findings = _findings(
        """\
try:
    do_something()
except ValueError:
    pass
"""
    )
    assert "P001" not in findings


def test_p001_no_finding_bare_except_with_body() -> None:
    # P006, not P001
    findings = _findings(
        """\
try:
    do_something()
except:
    logger.warning("oops", exc_info=True)
"""
    )
    assert "P001" not in findings


# ── P002 — TypedExceptPass ────────────────────────────────────────────────────


def test_p002_typed_except_pass() -> None:
    _single(
        """\
try:
    connect()
except ConnectionError:
    pass
""",
        "P002",
    )


def test_p002_no_finding_with_logging() -> None:
    _none(
        """\
try:
    connect()
except ConnectionError:
    logger.debug("connection unavailable, skipping")
"""
    )


def test_p002_no_finding_with_raise() -> None:
    _none(
        """\
try:
    connect()
except ConnectionError:
    raise
"""
    )


# ── P003 — BroadContextlibSuppress ────────────────────────────────────────────


def test_p003_suppress_exception() -> None:
    _single(
        """\
from contextlib import suppress
with suppress(Exception):
    do_something()
""",
        "P003",
    )


def test_p003_suppress_base_exception() -> None:
    _single(
        """\
from contextlib import suppress
with suppress(BaseException):
    do_something()
""",
        "P003",
    )


def test_p003_no_finding_narrow_suppress() -> None:
    _none(
        """\
from contextlib import suppress
with suppress(FileNotFoundError):
    os.remove(path)
"""
    )


def test_p003_no_finding_multiple_narrow_types() -> None:
    _none(
        """\
from contextlib import suppress
with suppress(FileNotFoundError, PermissionError):
    os.remove(path)
"""
    )


# ── P004 — BroadExceptClause ──────────────────────────────────────────────────


def test_p004_broad_except_no_log() -> None:
    # P009 also fires (assignment-only body) — check P004 is present, not _single
    findings = _findings(
        """\
try:
    run()
except Exception:
    x = 1
"""
    )
    assert "P004" in findings


def test_p004_no_finding_with_exc_info() -> None:
    _none(
        """\
try:
    run()
except Exception:
    logger.error("failed", exc_info=True)
"""
    )


def test_p004_no_finding_with_exception_method() -> None:
    _none(
        """\
try:
    run()
except Exception:
    logger.exception("failed")
"""
    )


def test_p004_base_exception_flagged() -> None:
    findings = _findings(
        """\
try:
    run()
except BaseException:
    pass
"""
    )
    assert "P004" in findings or "P002" in findings  # P002 wins for pass-only body


# ── P005 — ExceptBlockMissingExcInfo ─────────────────────────────────────────


def test_p005_warning_without_exc_info() -> None:
    _single(
        """\
try:
    run()
except ValueError as e:
    logger.warning("failed: something went wrong")
""",
        "P005",
    )


def test_p005_error_without_exc_info() -> None:
    _single(
        """\
try:
    run()
except ValueError as e:
    logger.error("failed")
""",
        "P005",
    )


def test_p005_no_finding_exc_info_true() -> None:
    _none(
        """\
try:
    run()
except ValueError:
    logger.warning("failed", exc_info=True)
"""
    )


def test_p005_no_finding_logger_exception() -> None:
    _none(
        """\
try:
    run()
except ValueError:
    logger.exception("failed")
"""
    )


# ── P006 — BareExceptWithBody ─────────────────────────────────────────────────


def test_p006_bare_except_with_body() -> None:
    # P005 also fires (warning without exc_info) — check P006 is present
    findings = _findings(
        """\
try:
    do_something()
except:
    logger.warning("failed")
"""
    )
    assert "P006" in findings


def test_p006_no_finding_typed_except() -> None:
    _none(
        """\
try:
    do_something()
except Exception:
    logger.warning("failed", exc_info=True)
"""
    )


# ── P007 — ErrorToReturnValue ─────────────────────────────────────────────────


def test_p007_return_none_without_log() -> None:
    _single(
        """\
def get_value():
    try:
        return fetch()
    except KeyError:
        return None
""",
        "P007",
    )


def test_p007_no_finding_log_before_return() -> None:
    _none(
        """\
def get_value():
    try:
        return fetch()
    except KeyError:
        logger.warning("key missing", exc_info=True)
        return None
"""
    )


def test_p007_no_finding_bare_return() -> None:
    # bare return (no value) — not an error conversion
    _none(
        """\
def do_it():
    try:
        run()
    except StopIteration:
        return
"""
    )


# ── P008 — ImportErrorWithoutLogging ─────────────────────────────────────────


def test_p008_import_error_no_log() -> None:
    _single(
        """\
try:
    import ujson as json
except ImportError:
    import json
""",
        "P008",
    )


def test_p008_module_not_found_no_log() -> None:
    # P009 also fires (assignment-only body) — check P008 is present
    findings = _findings(
        """\
try:
    import ujson
except ModuleNotFoundError:
    ujson = None
"""
    )
    assert "P008" in findings


def test_p008_no_finding_with_debug_log() -> None:
    _none(
        """\
try:
    import ujson as json
except ImportError:
    logger.debug("ujson not available, falling back to stdlib json")
    import json
"""
    )


# ── P009 — ExceptBlockOnlyAssigns ────────────────────────────────────────────


def test_p009_only_assignment() -> None:
    # P004 also fires (broad except without exc_info) — check P009 is present
    findings = _findings(
        """\
try:
    result = fetch()
except Exception:
    result = default_value
"""
    )
    assert "P009" in findings


def test_p009_no_finding_log_present() -> None:
    _none(
        """\
try:
    result = fetch()
except Exception:
    logger.warning("fetch failed, using default", exc_info=True)
    result = default_value
"""
    )


def test_p009_no_finding_raise_present() -> None:
    # The raise statement in the body means P009 should not fire; P004 and P018
    # may fire for other reasons — only assert P009 is absent.
    findings = _findings(
        """\
try:
    result = fetch()
except Exception as e:
    result = default_value
    raise InternalError() from e
"""
    )
    assert "P009" not in findings


# ── P010 — AsyncioGatherExceptionsUnexamined ─────────────────────────────────


def test_p010_bare_gather_expression() -> None:
    _single(
        """\
import asyncio
async def run():
    asyncio.gather(t1(), t2(), return_exceptions=True)
""",
        "P010",
    )


def test_p010_assigned_not_inspected() -> None:
    _single(
        """\
import asyncio
async def run():
    results = await asyncio.gather(t1(), t2(), return_exceptions=True)
    process(results)
""",
        "P010",
    )


def test_p010_no_finding_isinstance_check() -> None:
    _none(
        """\
import asyncio
async def run():
    results = await asyncio.gather(t1(), t2(), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.error("task failed", exc_info=True)
"""
    )


def test_p010_no_finding_without_return_exceptions() -> None:
    _none(
        """\
import asyncio
async def run():
    results = await asyncio.gather(t1(), t2())
    process(results)
"""
    )


# ── P011 — LoggingFilterUnsafeBody ───────────────────────────────────────────


def test_p011_unwrapped_filter_body() -> None:
    _single(
        """\
import logging

class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = get_request_id()
        return True
""",
        "P011",
    )


def test_p011_no_finding_wrapped_in_try() -> None:
    # P011 must not fire when the body IS wrapped; P004/P007 may still fire for
    # the inner except block (broad except, return without log) — only assert P011 absent.
    findings = _findings(
        """\
import logging

class RequestIdFilter(logging.Filter):
    def filter(self, record):
        try:
            record.request_id = get_request_id()
            return True
        except Exception:
            record.request_id = "unknown"
            return True
"""
    )
    assert "P011" not in findings


def test_p011_no_finding_non_filter_class() -> None:
    _none(
        """\
class MyHandler:
    def filter(self, record):
        record.x = 1
        return True
"""
    )


# ── P012 — UntypedBuiltinRaise ───────────────────────────────────────────────


@pytest.mark.parametrize("exc_name", sorted(BUILTIN_RAISES))
def test_p012_builtin_raise(exc_name: str) -> None:
    src = f"""\
def do_it():
    raise {exc_name}("something went wrong")
"""
    _single(src, "P012")


def test_p012_no_finding_in_post_init() -> None:
    _none(
        """\
from dataclasses import dataclass

@dataclass
class Config:
    value: int

    def __post_init__(self):
        if self.value < 0:
            raise ValueError("value must be non-negative")
"""
    )


def test_p012_no_finding_in_init() -> None:
    _none(
        """\
class Config:
    def __init__(self, value):
        if not isinstance(value, int):
            raise TypeError("value must be int")
        self.value = value
"""
    )


def test_p012_no_finding_in_field_validator() -> None:
    _none(
        """\
from pydantic import field_validator

class MyModel:
    @field_validator("name")
    def validate_name(cls, v):
        if not v:
            raise ValueError("name must not be empty")
        return v
"""
    )


def test_p012_no_finding_for_typed_error() -> None:
    # InternalError is a typed AppError leaf → P012 must not fire.
    # P018 may fire (bare parent leaf) — only assert P012 is absent.
    findings = _findings(
        """\
from application_sdk.errors import InternalError

def do_it():
    raise InternalError(message="something went wrong")
"""
    )
    assert "P012" not in findings


# ── P013 — LegacyAtlanErrorRaise ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc_name",
    [n for n in sorted(LEGACY_ATLAN_ERRORS) if n != "IOError"],
)
def test_p013_legacy_error_raise(exc_name: str) -> None:
    src = f"""\
from application_sdk.common.error_codes import {exc_name}

def do_it():
    raise {exc_name}("SOME_CODE", "something went wrong")
"""
    _single(src, "P013")


def test_p013_ioerror_flagged_when_imported_from_error_codes() -> None:
    _single(
        """\
from application_sdk.common.error_codes import IOError

def do_it():
    raise IOError("IO_ERR", "failed")
""",
        "P013",
    )


def test_p013_ioerror_not_flagged_when_builtin() -> None:
    # IOError without import from error_codes — it's just the Python builtin alias
    _none(
        """\
def do_it():
    raise IOError("file error")
"""
    )


# ── P014 — ExceptLoopControlSwallow ──────────────────────────────────────────


def test_p014_except_continue_in_loop() -> None:
    _single(
        """\
for item in items:
    try:
        process(item)
    except ValueError:
        continue
""",
        "P014",
    )


def test_p014_except_break_in_loop() -> None:
    _single(
        """\
while True:
    try:
        run()
    except RuntimeError:
        break
""",
        "P014",
    )


def test_p014_no_finding_log_before_continue() -> None:
    _none(
        """\
for item in items:
    try:
        process(item)
    except ValueError:
        logger.debug("skipping item", exc_info=True)
        continue
"""
    )


def test_p014_no_finding_outside_loop() -> None:
    findings = _findings(
        """\
try:
    run()
except ValueError:
    pass
"""
    )
    assert "P014" not in findings


# ── P015 — ExceptionTextInErrorMessage ───────────────────────────────────────


def test_p015_fstring_exc_in_message() -> None:
    # P016 and P018 also fire on InternalError — only assert P015 is present.
    assert "P015" in _findings(
        """\
try:
    fetch()
except ValueError as e:
    raise InternalError(message=f"fetch failed: {e}")
"""
    )


def test_p015_str_exc_in_message() -> None:
    # P016 and P018 also fire — only assert P015 is present.
    assert "P015" in _findings(
        """\
try:
    fetch()
except ValueError as e:
    raise InternalError(message=str(e))
"""
    )


def test_p015_no_finding_static_message() -> None:
    # Static message is clean for P015; P018 still fires (bare InternalError leaf).
    assert "P015" not in _findings(
        """\
try:
    fetch()
except ValueError as e:
    raise InternalError(message="fetch failed", cause=e) from e
"""
    )


def test_p015_no_finding_no_message_kwarg() -> None:
    # No message= kwarg → P015 must not fire.
    assert "P015" not in _findings(
        """\
try:
    fetch()
except ValueError as e:
    raise InternalError(cause=e) from e
"""
    )


# ── P016 — MissingExceptionChaining ──────────────────────────────────────────


def test_p016_raise_without_from() -> None:
    # P018 also fires (bare InternalError leaf) — only assert P016 is present.
    assert "P016" in _findings(
        """\
try:
    connect()
except ValueError as e:
    raise InternalError(message="connect failed")
"""
    )


def test_p016_no_finding_with_from() -> None:
    # 'from e' satisfies P016; P018 may still fire — only assert P016 is absent.
    assert "P016" not in _findings(
        """\
try:
    connect()
except ValueError as e:
    raise InternalError(message="connect failed") from e
"""
    )


def test_p016_no_finding_from_none() -> None:
    # 'from None' explicitly suppresses chaining — P016 must not fire.
    assert "P016" not in _findings(
        """\
try:
    connect()
except ValueError as e:
    raise InternalError(message="connect failed") from None
"""
    )


def test_p016_no_finding_bare_reraise() -> None:
    _none(
        """\
try:
    connect()
except ValueError:
    raise
"""
    )


def test_p016_no_finding_no_binding() -> None:
    # No 'as e' binding — chaining is not applicable; P016 must not fire.
    # P018 may fire (bare InternalError leaf) — only assert P016 is absent.
    assert "P016" not in _findings(
        """\
try:
    connect()
except ValueError:
    raise InternalError(message="connect failed")
"""
    )


# ── P017 — SecretNamedEvidenceKey ────────────────────────────────────────────


@pytest.mark.parametrize("suffix", ["_secret", "_password", "_token"])
def test_p017_secret_suffix(suffix: str) -> None:
    # P018 also fires (bare InternalError leaf) — only assert P017 is present.
    src = f"""\
raise InternalError(message="auth failed", api{suffix}="hunter2")
"""
    assert "P017" in _findings(src)


def test_p017_no_finding_safe_key() -> None:
    # Safe key → P017 must not fire; P018 may fire.
    assert "P017" not in _findings(
        """\
raise InternalError(message="auth failed", credential_name="my-cred")
"""
    )


def test_p017_no_finding_token_type() -> None:
    # "token_type" does not end with the forbidden suffixes → P017 must not fire.
    assert "P017" not in _findings(
        """\
raise InternalError(message="auth failed", token_type="Bearer")
"""
    )


# ── P018 — BareParentLeafRaise ───────────────────────────────────────────────


@pytest.mark.parametrize("leaf", sorted(LEAF_CLASSES - {"InternalError"}))
def test_p018_bare_leaf(leaf: str) -> None:
    src = f"""\
def fail():
    raise {leaf}(message="something went wrong")
"""
    _single(src, "P018")


def test_p018_no_finding_classification_pending() -> None:
    _none(
        """\
def fail():
    raise InternalError(classification_pending=True)
"""
    )


def test_p018_internal_error_without_classification_pending_flagged() -> None:
    _single(
        """\
def fail():
    raise InternalError(message="oops")
""",
        "P018",
    )


def test_p018_no_finding_subclass_not_in_leaf_list() -> None:
    # A domain subclass with a different name — not in LEAF_CLASSES
    _none(
        """\
def fail():
    raise EngineNotInitializedError(message="engine missing")
"""
    )


# ── Suppression behaviour ─────────────────────────────────────────────────────


def test_suppression_trailing_comment() -> None:
    src = """\
try:
    do_something()
except:  # conformance: ignore[P001] test_only path
    pass
"""
    _suppressed(src, "P001")


def test_suppression_own_line_above() -> None:
    # Directive must be on the line directly above the except keyword (line-1).
    src = """\
try:
    do_something()
# conformance: ignore[P002] StopIteration expected in manual iterator
except StopIteration:
    pass
"""
    fs = scan_text(src, "fake.py")
    p002 = [f for f in fs if f.rule_id == "P002"]
    assert p002, "No P002 finding emitted"
    assert all(f.suppressed for f in p002), "P002 not suppressed"


def test_suppression_wrong_rule_id_does_not_suppress() -> None:
    src = """\
try:
    do_something()
except:  # conformance: ignore[P999] not the right rule
    pass
"""
    fs = scan_text(src, "fake.py")
    p001 = [f for f in fs if f.rule_id == "P001"]
    assert p001, "No P001 finding"
    assert not any(f.suppressed for f in p001), "P001 should not be suppressed"


def test_suppression_without_rule_ids_suppresses_all() -> None:
    src = """\
try:
    do_something()
except:  # conformance: ignore intentional
    pass
"""
    _suppressed(src, "P001")


def test_suppression_justification_captured() -> None:
    src = """\
try:
    do_something()
except:  # conformance: ignore[P001] best-effort cleanup path
    pass
"""
    fs = scan_text(src, "fake.py")
    p001 = [f for f in fs if f.rule_id == "P001" and f.suppressed]
    assert p001
    assert p001[0].suppression_justification == "best-effort cleanup path"


def test_suppressed_finding_has_sarif_suppression_record(tmp_path: Path) -> None:
    """Suppressed finding is emitted as SARIF kind=fail with suppressions entry."""
    src_file = tmp_path / "target.py"
    src_file.write_text(
        """\
try:
    do_something()
except:  # conformance: ignore[P001] test
    pass
"""
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            "--sarif-output",
            str(sarif_file),
            str(src_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    p001_results = [r for r in report.runs[0].results if r.rule_id == "P001"]
    assert p001_results, "No P001 result in SARIF"
    suppressed = [r for r in p001_results if r.suppressions]
    assert suppressed, "P001 result has no suppressions entry"
    assert derive_disposition(suppressed[0]) == Disposition.SUPPRESSED


# ── Scan syntax error ─────────────────────────────────────────────────────────


def test_scan_text_syntax_error_returns_empty() -> None:
    assert scan_text("def (", "bad.py") == []


# ── End-to-end via main() ─────────────────────────────────────────────────────


def test_main_exit_1_on_block_violation(tmp_path: Path) -> None:
    """main() exits 1 when a BLOCK-tier rule fires (P001)."""
    src_file = tmp_path / "target.py"
    src_file.write_text(
        """\
try:
    do_something()
except:
    pass
"""
    )
    code = main(["--root", str(tmp_path), str(src_file)])
    assert code == 1


def test_main_exit_0_when_clean(tmp_path: Path) -> None:
    """main() exits 0 when no findings."""
    src_file = tmp_path / "target.py"
    src_file.write_text("x = 1\n")
    code = main(["--root", str(tmp_path), str(src_file)])
    assert code == 0


def test_main_exit_0_warn_only(tmp_path: Path) -> None:
    """main() exits 0 when only WARN-tier rules fire (WARN ≠ BLOCK → exit 0)."""
    # P005 is WARN tier
    src_file = tmp_path / "target.py"
    src_file.write_text(
        """\
try:
    run()
except ValueError:
    logger.warning("failed")
"""
    )
    code = main(["--root", str(tmp_path), str(src_file)])
    assert code == 0


def test_main_exit_0_suppressed_block(tmp_path: Path) -> None:
    """Suppressed BLOCK finding does not affect exit code."""
    src_file = tmp_path / "target.py"
    src_file.write_text(
        """\
try:
    do_something()
except:  # conformance: ignore[P001] test
    pass
"""
    )
    code = main(["--root", str(tmp_path), str(src_file)])
    assert code == 0


def test_main_sarif_validates(tmp_path: Path) -> None:
    """Emitted SARIF validates against the official schema."""
    src_file = tmp_path / "target.py"
    src_file.write_text(
        """\
try:
    do_something()
except:
    pass
"""
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            "--sarif-output",
            str(sarif_file),
            "--validate",
            str(src_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    validate_sarif(report)


def test_main_p001_result_is_failing(tmp_path: Path) -> None:
    """P001 violation produces a FAILING disposition."""
    src_file = tmp_path / "target.py"
    src_file.write_text(
        """\
try:
    do_something()
except:
    pass
"""
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            "--sarif-output",
            str(sarif_file),
            str(src_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    failing = [
        r
        for r in report.runs[0].results
        if derive_disposition(r) == Disposition.FAILING
    ]
    assert any(r.rule_id == "P001" for r in failing)
