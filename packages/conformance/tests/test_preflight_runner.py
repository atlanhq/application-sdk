import json
from pathlib import Path

from conformance.suite.runner import main

HANDLER = (
    "from application_sdk.handler import Handler\n"
    "class H(Handler):\n"
    "    async def preflight_check(self, input): ...\n"
)


def _f016_messages(report_path: Path) -> list[str]:
    results = json.loads(report_path.read_text())["runs"][0]["results"]
    return [r["message"]["text"] for r in results if r["ruleId"] == "F016"]


def test_undefined_scenarios_are_reported_statically_at_warn(tmp_path: Path):
    """No tests, no flags: the whole matrix is reported missing, and WARN exits 0."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="example-connector"\nversion="1.0.0"\n'
    )
    (tmp_path / "handler.py").write_text(HANDLER)
    output = tmp_path / "report.json"
    assert (
        main(["--repo", str(tmp_path), "--rule", "F016", "--output", str(output)]) == 0
    )
    messages = _f016_messages(output)
    assert len(messages) == 13
    assert all("is not registered" in m for m in messages)
    run = json.loads(output.read_text())["runs"][0]
    assert {r["level"] for r in run["results"]} == {"warning"}
    assert "atlan/preflightTests" not in run.get("properties", {})


def test_new_static_rule_reaches_sarif(tmp_path: Path):
    (tmp_path / "handler.py").write_text(
        "from application_sdk.handler import Handler\nclass H(Handler):\n    async def preflight_check(self, input):\n        return True\n"
    )
    output = tmp_path / "report.json"
    main(["--repo", str(tmp_path), "--rule", "F006", "--output", str(output)])
    assert {
        row["ruleId"] for row in json.loads(output.read_text())["runs"][0]["results"]
    } == {"F006"}


def test_unresolved_analysis_is_machine_identifiable(tmp_path):
    source = tmp_path / "handler.py"
    source.write_text(
        'from application_sdk.handler import PreflightCheck\ndef check(ok):\n return PreflightCheck(name="probe", passed=ok)\n'
    )
    output = tmp_path / "report.sarif"
    main(["--repo", str(tmp_path), "--rule", "F003,F019", "--output", str(output)])
    results = json.loads(output.read_text())["runs"][0]["results"]
    assert results[0]["ruleId"] == "F019"
    assert results[0]["properties"]["atlan/analysisStatus"] == "unresolved"


def test_preflight_selection_does_not_run_unrelated_p_checks(tmp_path, monkeypatch):
    from conformance.suite import runner

    def unexpected(root):
        raise AssertionError("Unrelated P-series discovery was invoked")

    trap = runner.CheckRegistration(
        series="P", discover=unexpected, scan_path=lambda path, root: []
    )
    monkeypatch.setattr(runner, "_CHECKS", [*runner._CHECKS, trap])
    runner.main(
        [
            "--repo",
            str(tmp_path),
            "--rule",
            "F003,F019",
            "--output",
            str(tmp_path / "report.sarif"),
        ]
    )


def test_typed_failure_errors_preserve_soft_exit_mode(tmp_path):
    (tmp_path / "handler.py").write_text(
        'from application_sdk.handler import PreflightCheck\ndef check():\n return PreflightCheck(name="probe", passed=False)\n'
    )
    output = tmp_path / "report.sarif"
    args = ["--repo", str(tmp_path), "--rule", "F003", "--output", str(output)]
    assert main(args) == 1
    assert main([*args, "--exit-zero"]) == 0
    run = json.loads(output.read_text())["runs"][0]
    assert run["results"][0]["level"] == "error"
    assert run["invocations"][0]["exitCode"] == 1


def _missing_f016_entrypoints(tmp_path: Path, *extra: str) -> set[str]:
    """Return the entrypoints F016 reported undefined scenarios for."""
    output = tmp_path / "report.json"
    main(["--repo", str(tmp_path), "--rule", "F016", "--output", str(output), *extra])
    return {
        m.split(" for entrypoint ", 1)[1].split(" ", 1)[0]
        for m in _f016_messages(output)
    }


def test_scenario_entrypoints_honour_exclude(tmp_path: Path):
    """An excluded subtree's @entrypoints must not join the expected matrix.

    /remediate used to clone reference apps under ``remediation/refs/``; a
    multi-entrypoint clone there used to make a single-entrypoint app owe
    F016 scenarios for the clone's entrypoints even with
    ``--exclude remediation/``.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="example-connector"\nversion="1.0.0"\n'
    )
    (tmp_path / "handler.py").write_text(HANDLER)
    (tmp_path / "tests").mkdir()
    ref = tmp_path / "remediation" / "refs" / "other-app" / "app"
    ref.mkdir(parents=True)
    (ref / "connector.py").write_text(
        "from application_sdk.app import App, entrypoint\n"
        "class Other(App):\n"
        "    @entrypoint\n"
        "    async def extract_metadata(self, input): pass\n"
        "    @entrypoint\n"
        "    async def extract_lineage(self, input): pass\n"
    )

    # Unexcluded, the clone's entrypoints are picked up — the precondition
    # that makes the excluded assertion below meaningful.
    assert _missing_f016_entrypoints(tmp_path) == {
        "extract_metadata",
        "extract_lineage",
    }
    assert _missing_f016_entrypoints(tmp_path, "--exclude", "remediation/") == {
        "default"
    }
