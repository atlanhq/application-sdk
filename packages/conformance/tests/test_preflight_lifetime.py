from pathlib import Path

import pytest
from conformance.suite.checks.preflight._common import build_registry
from conformance.suite.checks.preflight._lifetime import scan


def findings(tmp_path: Path, body: str, imports: str = ""):
    source = tmp_path / "handler.py"
    source.write_text(
        "from application_sdk.handler import Handler\n"
        + imports
        + "\nclass H(Handler):\n    async def preflight_check(self, input):\n"
        + "\n".join("        " + line for line in body.splitlines())
    )
    return scan(build_registry([source], tmp_path))


@pytest.mark.parametrize(
    "call",
    [
        "time.sleep(3)",
        "requests.get('https://example.invalid')",
        "pyodbc.connect('synthetic')",
    ],
)
def test_blocking_source_calls(tmp_path, call):
    assert "P057" in {
        f.rule_id for f in findings(tmp_path, call, "import time, requests, pyodbc\n")
    }


def test_unrelated_method_named_get(tmp_path):
    assert not findings(tmp_path, "return input.metadata.get('scope')")


def test_thread_requires_outer_deadline(tmp_path):
    assert "P057" in {
        f.rule_id
        for f in findings(
            tmp_path, "await asyncio.to_thread(probe)", "import asyncio\n"
        )
    }


def test_bounded_thread_is_valid(tmp_path):
    assert not findings(
        tmp_path,
        "await asyncio.wait_for(asyncio.to_thread(probe), timeout=input.timeout_seconds)",
        "import asyncio\n",
    )


def test_async_timeout_context_is_valid(tmp_path):
    assert not findings(
        tmp_path,
        "async with asyncio.timeout(input.timeout_seconds):\n    await asyncio.to_thread(probe)",
        "import asyncio\n",
    )


@pytest.mark.parametrize(
    "expr", ["max(input.timeout_seconds, 120)", "input.timeout_seconds + 5"]
)
def test_budget_enlargement(tmp_path, expr):
    assert "P058" in {
        f.rule_id for f in findings(tmp_path, f"await probe(timeout={expr})")
    }


def test_budget_headroom(tmp_path):
    assert not findings(tmp_path, "await probe(timeout=min(input.timeout_seconds, 5))")


def test_blocking_cleanup(tmp_path):
    result = findings(
        tmp_path, "try:\n    await probe()\nfinally:\n    engine.dispose()"
    )
    assert "P059" in {f.rule_id for f in result}


def test_bounded_cleanup_off_loop(tmp_path):
    assert not findings(
        tmp_path,
        "try:\n    await probe()\nfinally:\n    await asyncio.wait_for(asyncio.to_thread(engine.dispose), timeout=1)",
        "import asyncio\n",
    )


def test_exception_exposed_in_preflight_message(tmp_path):
    result = findings(
        tmp_path,
        'try:\n    await probe()\nexcept Exception as exc:\n    return PreflightCheck(passed=False, message=f"Failed: {exc}")',
        "from application_sdk.handler.contracts import PreflightCheck\n",
    )
    assert "P060" in {f.rule_id for f in result}


def test_redacted_exception_message(tmp_path):
    assert not findings(
        tmp_path,
        "try:\n    await probe()\nexcept Exception as exc:\n    return PreflightCheck(passed=False, message=redact_secrets(str(exc)))",
        "from application_sdk.handler.contracts import PreflightCheck\nfrom application_sdk.errors.base import redact_secrets\n",
    )


def test_removed_contract_is_upgrade_warning(tmp_path):
    result = findings(
        tmp_path, 'return os.getenv("ATLAN_PREFLIGHT_GATE_MODE")', "import os\n"
    )
    assert [f.rule_id for f in result] == ["P061"]
    assert "#3685" in result[0].message


def test_documenting_removed_name_does_not_fire(tmp_path):
    assert not findings(
        tmp_path, 'message = "ATLAN_PREFLIGHT_GATE_MODE is obsolete"\nreturn message'
    )


def test_eager_thread_argument_runs_on_loop(tmp_path):
    assert "P059" in {
        f.rule_id
        for f in findings(
            tmp_path,
            "await asyncio.to_thread(probe, resource.close())",
            "import asyncio\n",
        )
    }


def test_sync_helper_blocks_loop(tmp_path):
    assert "P057" in {
        f.rule_id
        for f in findings(
            tmp_path, "probe()", "import time\ndef probe():\n    time.sleep(5)\n"
        )
    }


@pytest.mark.parametrize(
    "prefix", ['token = "synthetic"', 'def unused():\n    token = "synthetic"']
)
def test_unrelated_credential_names_do_not_flag_traceback(tmp_path, prefix):
    rows = findings(
        tmp_path,
        prefix
        + '\ntry:\n    await probe()\nexcept Exception as exc:\n    logger.exception("failed")',
    )
    assert "P060" not in {row.rule_id for row in rows}


@pytest.mark.parametrize(
    "body",
    [
        'token = "synthetic"\ntry:\n    await probe(token)\nexcept Exception as exc:\n    logger.exception("failed")',
        'try:\n    await probe()\nexcept Exception as exc:\n    logger.exception("failed: %s", token)',
    ],
)
def test_traceback_credential_reads_remain_visible(tmp_path, body):
    assert "P060" in {row.rule_id for row in findings(tmp_path, body)}


def test_other_except_branch_does_not_taint_traceback(tmp_path):
    rows = findings(
        tmp_path,
        'try:\n    await probe()\nexcept ValueError:\n    consume(token)\nexcept Exception as exc:\n    logger.exception("failed")',
    )
    assert "P060" not in {row.rule_id for row in rows}
