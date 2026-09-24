"""F021 through the real runner: a warning, never a failing exit on its own."""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.runner import main

_HANDLER = """\
from application_sdk.errors import AuthError
from application_sdk.handler.base import Handler
from application_sdk.handler.contracts import (
    PreflightCheck, PreflightInput, PreflightOutput,
)


class H(Handler):
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        try:
            await probe()
        except Exception as exc:
            return PreflightOutput(checks=[PreflightCheck(
                name="auth", passed=False,
                error=AuthError(
                    message="Could not log in.",
                    suggested_action="Check the credentials, then re-run.",
                    cause=exc,
                ).to_failure_details(),
            )])
        return PreflightOutput(checks=[{checks}])
{extra}"""

_UNTYPED_CHECK = """
    async def _version(self) -> PreflightCheck:
        return PreflightCheck(name="version", passed=False)
"""


def _run(
    tmp_path: Path, rules: str, checks: str = "", extra: str = ""
) -> tuple[int, set]:
    app = tmp_path / "app"
    app.mkdir()
    (app / "handler.py").write_text(_HANDLER.format(checks=checks, extra=extra))
    output = tmp_path / "report.sarif"
    code = main(["--repo", str(tmp_path), "--rule", rules, "--output", str(output)])
    results = json.loads(output.read_text())["runs"][0]["results"]
    return code, {(r["ruleId"], r["level"]) for r in results}


def test_f021_alone_is_a_warning_and_exits_zero(tmp_path: Path) -> None:
    code, results = _run(tmp_path, "F021")
    assert results == {("F021", "warning")}
    assert code == 0


def test_a_blocking_rule_still_fails_next_to_f021(tmp_path: Path) -> None:
    code, results = _run(
        tmp_path, "F003,F021", checks="await self._version()", extra=_UNTYPED_CHECK
    )
    assert results == {("F003", "error"), ("F021", "warning")}
    assert code != 0
