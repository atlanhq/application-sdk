"""`with-tests` is accepted for one release and reaches nothing.

Conformance checks that the preflight scenarios are defined; whether they
pass is the test gate's measure. So no conformance leg syncs an environment
or runs pytest, and the `with-tests` switch that used to ask for that is a
deprecated no-op until v0.40.0.

"Accepted" and "reaches nothing" are both load-bearing. Dropping the input
from the reusable workflow would fail every caller that still passes it at
startup, with no check run to say why. Leaving it forwarded would keep the
env sync on the F leg and keep `--with-tests` flowing into `detect`. Each
link is asserted off the parsed YAML and the real function, because a
string search passes just as happily on a forwarded-but-ignored input.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from conformance.bootstrap.render import render
from conformance.suite.runner import main

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SUITE = _REPO_ROOT / ".github/workflows/conformance-reusable.yaml"
_INPUT = "with-tests"


def _suite_doc() -> dict[str, Any]:
    return yaml.safe_load(_SUITE.read_text(encoding="utf-8"))


def _load_build_args_module() -> Any:
    path = _REPO_ROOT / ".github/scripts/build_conformance_args.py"
    spec = importlib.util.spec_from_file_location("_bca", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_reusable_still_accepts_the_input() -> None:
    # `on:` parses as the boolean True in YAML 1.1.
    inputs = _suite_doc()[True]["workflow_call"]["inputs"]
    assert _INPUT in inputs, "removing the input fails every caller that passes it"
    assert inputs[_INPUT]["required"] is False
    assert "Deprecated no-op" in inputs[_INPUT]["description"]


def test_the_reusable_reads_the_input_nowhere() -> None:
    body = _SUITE.read_text(encoding="utf-8")
    assert f"inputs.{_INPUT}" not in body


def test_no_conformance_leg_but_dependencies_syncs_an_environment() -> None:
    for job in _suite_doc()["jobs"].values():
        include = job.get("strategy", {}).get("matrix", {}).get("include")
        if include:
            synced = {leg["series"] for leg in include if leg.get("needs_env")}
            assert synced == {"D"}, synced
            return
    raise AssertionError("no matrix job with an `include` list in the suite")


def test_the_detect_action_accepts_the_input_and_exports_nothing_for_it() -> None:
    action = yaml.safe_load(render("run-conformance-detect-action.yaml"))
    assert _INPUT in action["inputs"]
    for step in action["runs"]["steps"]:
        env = step.get("env") or {}
        assert "WITH_TESTS" not in env, step.get("name")
        assert all(_INPUT not in str(value) for value in env.values()), step.get("name")


def test_the_arg_builder_never_emits_with_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_build_args_module()
    monkeypatch.setenv("WITH_TESTS", "true")
    assert "--with-tests" not in module.build_args("F", "preflight")


def test_the_cli_flags_are_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same findings with and without the flags, a warning, and no child process."""

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("conformance must not start a subprocess for tests")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="1"\n')
    (tmp_path / "handler.py").write_text(
        "from application_sdk.handler import Handler\n"
        "class H(Handler):\n"
        "    async def preflight_check(self, input): ...\n"
    )
    results = []
    for extra in (
        [],
        ["--with-tests", "--test-timeout", "5", "--test-python", "python"],
        ["--preflight-report", str(tmp_path / "missing.json")],
    ):
        output = tmp_path / "report.sarif"
        main(
            ["--repo", str(tmp_path), "--series", "F", "--output", str(output), *extra]
        )
        results.append(
            sorted(
                r["message"]["text"]
                for r in json.loads(output.read_text())["runs"][0]["results"]
            )
        )
        err = capsys.readouterr().err
        assert ("deprecated no-op" in err) == bool(extra)
    assert results[0] == results[1] == results[2]
