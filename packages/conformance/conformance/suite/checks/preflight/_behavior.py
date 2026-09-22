"""Grade the registered preflight scenarios against their execution record.

Two ways to obtain that record, one way to grade it. ``_execute`` runs the
marked tests here in a bounded subprocess; ``_load`` reads a report an
earlier pytest run already wrote. Both hand ``run_behavior`` the same
``(execution, data)`` pair, and everything after that point — coverage
against ``SCENARIOS``, per-scenario findings, the summary the F019
clearing pass reads — is shared.

Keeping the split at that seam is deliberate: the grading is the rule, and
it must not be possible to reach a softer verdict by supplying evidence
from somewhere else.
"""

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


def _record_location(record: dict[str, Any], root: Path) -> tuple[str, int]:
    filename, line = record.get("file"), record.get("line")
    if isinstance(filename, str) and filename and type(line) is int and line > 0:
        try:
            path = (root / filename).resolve().relative_to(root.resolve())
            return path.as_posix(), line
        except (ValueError, OSError):
            pass
    return "pyproject.toml", 1


def _stop(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def _execute(
    root: Path,
    selected: set[str],
    timeout: float,
    python: str | None,
) -> tuple[str, dict[str, Any]]:
    """Run the marked tests ourselves and return (execution, report data)."""
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
        return execution, data


def _load(report: Path) -> tuple[str, dict[str, Any]]:
    """Read a report an earlier pytest run wrote and return (execution, data).

    The producer is the app's own test job:
    ``pytest -p conformance.preflight_testing --preflight-report=<path>``.
    Reading it means the scenarios execute once, in the environment that
    already installs the app, instead of a second time in a subprocess
    here — see the module note in ``tests/test_preflight_report_input.py``.

    A report we cannot read is ``"error"``, never ``"completed"``: the
    absence of evidence must not read as evidence of conformance. It is
    deliberately not distinguished from a failed subprocess, because the
    consequence for the caller is identical — re-run the tests — and the
    message the interpreter emits already says so.

    ``exitstatus`` is the load-bearing check, and it is checked against the
    same ``{0, 1, 5}`` that ``_execute`` applies to its subprocess's return
    code: passed, tests-failed, nothing-collected. Everything else —
    interrupted (2), internal error (3), usage error (4) — means the run
    that produced this file did not finish, and a scenario that passed
    before the run died proves nothing about a matrix that never completed.
    Without this, a report whose scenarios all passed graded as
    ``"completed"`` on a pytest run that exited 2, cleared F019, and
    reported preflight conformance for a red test job.

    That matters more here than in the subprocess path. ``_execute`` runs
    only the marked scenarios and watches its own child's return code; the
    producer of an external report is the caller's *whole* test suite, so
    anything in it can take the run down after the scenarios have passed.
    """
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "error", {}
    if not isinstance(data, dict) or not data:
        return "error", {}
    # `type(...) is int` rather than isinstance: bool subclasses int, and a
    # JSON `true` here would otherwise pass as exit status 1.
    status = data.get("exitstatus")
    if type(status) is not int or status not in {0, 1, 5}:
        return "error", {}
    if data.get("collection_errors"):
        return "error", {}
    return "completed", data


def run_behavior(
    root: Path,
    rule_ids: set[str] | None = None,
    scope: str = "app",
    timeout: float = 120.0,
    entrypoints: tuple[str, ...] = ("default",),
    python: str | None = None,
    report: Path | None = None,
) -> BehaviorResult:
    """Grade marked tests; missing, skipped, failed or unsupported cannot pass.

    With ``report``, the results of an earlier pytest run are graded and
    nothing is executed here. Without it, the tests are run in a bounded
    subprocess. The grading is identical either way — same records, same
    findings, same summary — which is the point: the evidence's provenance
    changes, the standard it is held to does not.
    """
    if timeout <= 0 or not timeout < float("inf"):
        raise ValueError("Preflight test timeout must be positive and finite")
    applicable = {"F017", "F018"} if scope == "sdk" else {"F016"}
    selected = applicable & (rule_ids if rule_ids is not None else applicable)
    if not selected:
        return BehaviorResult([], {})
    summary: dict[str, Any] = {}
    findings: list[Finding] = []
    root = root.resolve()
    entrypoints = tuple(dict.fromkeys(entrypoints)) or ("default",)
    if report is not None:
        execution, data = _load(report)
    else:
        execution, data = _execute(root, selected, timeout, python)
    records = data.get("tests", {})
    if not isinstance(records, dict):
        records = {}
        execution = "error"
    for rule in sorted(selected):
        expected = {(entry, name) for entry in entrypoints for name in SCENARIOS[rule]}
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
                filename, line = _record_location(record, root)
                findings.append(
                    Finding(
                        rule_id=rule,
                        file=filename,
                        line=line,
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
            "complete": not missing and not any(f.rule_id == rule for f in findings),
        }
    return BehaviorResult(findings, summary)
