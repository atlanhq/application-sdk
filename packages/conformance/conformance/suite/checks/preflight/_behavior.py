"""Execute registered preflight scenarios in a bounded subprocess."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conformance.preflight_testing import SCENARIOS
from conformance.suite.schema.findings import Finding


@dataclass
class BehaviorResult:
    findings: list[Finding]
    summary: dict[str, Any]


def _stop(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def run_behavior(
    root: Path,
    rule_ids: set[str] | None = None,
    scope: str = "app",
    timeout: float = 120.0,
    entrypoints: tuple[str, ...] = ("default",),
    python: str | None = None,
) -> BehaviorResult:
    """Run marked tests; missing, skipped, failed or unsupported cases cannot pass."""
    if timeout <= 0 or not timeout < float("inf"):
        raise ValueError("Preflight test timeout must be positive and finite")
    applicable = {"P063", "P064"} if scope == "sdk" else {"P062"}
    selected = applicable & (rule_ids if rule_ids is not None else applicable)
    if not selected:
        return BehaviorResult([], {})
    summary: dict[str, Any] = {}
    findings: list[Finding] = []
    root = root.resolve()
    entrypoints = tuple(dict.fromkeys(entrypoints)) or ("default",)
    with tempfile.TemporaryDirectory(prefix="preflight-conformance-") as directory:
        report_path = Path(directory) / "results.json"
        env = dict(os.environ)
        package_root = str(Path(__file__).resolve().parents[4])
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, (package_root, env.get("PYTHONPATH")))
        )
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [
            python or sys.executable,
            "-m",
            "pytest",
            "-p",
            "conformance.preflight_testing",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            f"--preflight-report={report_path}",
            f"--preflight-rules={','.join(sorted(selected))}",
            "--",
            str(root / "tests" if (root / "tests").is_dir() else root),
        ]
        execution = "completed"
        data: dict[str, Any] = {}
        try:
            with (Path(directory) / "pytest.log").open("wb") as output:
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name == "posix",
                )
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    execution = "timeout"
                    _stop(process)
                except BaseException:
                    _stop(process)
                    raise
            if report_path.exists():
                data = json.loads(report_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
                    execution = "error"
            if execution != "timeout" and (
                not data
                or process.returncode not in {0, 1, 5}
                or data.get("collection_errors")
            ):
                execution = "error"
        except (OSError, ValueError):
            execution = "error"
        records = data.get("tests", {})
        if not isinstance(records, dict):
            records = {}
            execution = "error"
        for rule in sorted(selected):
            expected = {
                (entry, name) for entry in entrypoints for name in SCENARIOS[rule]
            }
            seen: set[tuple[str, str]] = set()
            passed = evaluated = 0
            for record in records.values():
                if not isinstance(record, dict) or record.get("rule") != rule:
                    continue
                key = (record.get("entrypoint"), record.get("scenario"))
                phases = record.get("phases", {})
                if not isinstance(phases, dict) or not all(
                    isinstance(value, str) for value in key
                ):
                    execution = "error"
                    continue
                if phases.get("call") is not None:
                    evaluated += 1
                valid = key in expected
                if valid:
                    seen.add(key)
                success = (
                    valid
                    and all(
                        phases.get(phase) == "passed"
                        for phase in ("setup", "call", "teardown")
                    )
                    and not record.get("unsupported")
                    and not record.get("xfail")
                    and execution == "completed"
                )
                if success:
                    passed += 1
                else:
                    reason = (
                        "unsupported"
                        if record.get("unsupported")
                        else "invalid registration"
                        if not valid
                        else "failed, skipped, or incomplete"
                    )
                    findings.append(
                        Finding(
                            rule_id=rule,
                            file="pyproject.toml",
                            line=1,
                            column=1,
                            discriminator=str(key),
                            message=f"Preflight scenario {key[1]} for entrypoint {key[0]} is {reason}; it does not establish behavioral conformance.",
                        )
                    )
            missing = sorted(expected - seen)
            for entry, scenario in missing:
                findings.append(
                    Finding(
                        rule_id=rule,
                        file="pyproject.toml",
                        line=1,
                        column=1,
                        discriminator=f"{entry}:{scenario}",
                        message=f"Missing executed preflight scenario {scenario} for entrypoint {entry}. Register a real-handler contract test with preflight_conformance; file presence is not coverage.",
                    )
                )
            if execution != "completed":
                findings.append(
                    Finding(
                        rule_id=rule,
                        file="pyproject.toml",
                        line=1,
                        column=1,
                        discriminator="execution",
                        message=f"Preflight scenario execution ended with {execution}; no complete conformance result is available. Re-run the selected tests in the app's test environment.",
                    )
                )
            summary[rule] = {
                "execution": execution,
                "evaluated": evaluated,
                "passed": passed,
                "missing": [f"{entry}:{scenario}" for entry, scenario in missing],
                "complete": not missing
                and not any(f.rule_id == rule for f in findings),
            }
    return BehaviorResult(findings, summary)
