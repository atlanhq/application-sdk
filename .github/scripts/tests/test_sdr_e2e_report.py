"""Tests for .github/actions/sdr-e2e/sdr_report.py.

Lives beside the sdr-e2e action for the reason given in test_sdr_e2e_steps.py.
Every test runs in a throwaway working directory, since the script reads the
caller's ``results/`` and ``integration-test-summary*.json`` relative to cwd.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "actions" / "sdr-e2e"))

import sdr_report  # noqa: E402

TENANT = "acme.atlan.com"


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "results").mkdir()
    out = tmp_path / "gh_output"
    out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    return tmp_path


def _output(ws: Path, name: str) -> str:
    text = (ws / "gh_output").read_text()
    m = re.fullmatch(rf"{name}<<(\S+)\n(.*)\n\1\n", text, re.S)
    assert m, text
    return m.group(2)


def _junit(ws: Path, body: str) -> None:
    (ws / "results/sdr-test-results.xml").write_text(f"<testsuites>{body}</testsuites>")


def _scenario(
    name: str, status: str = "passed", wf: str = "wf", run: str = "run"
) -> dict:
    return {
        "name": name,
        "api": "workflow",
        "status": status,
        "assertions": {
            "data.workflow_id": {"actual": wf},
            "data.run_id": {"actual": run},
        },
    }


# --- workflow-links ----------------------------------------------------------


def _url(wf: str = "wf", run: str = "run") -> str:
    return (
        f"https://{TENANT}/api/temporal/namespaces/default/workflows/{wf}/{run}/history"
    )


def test_links_flat_for_a_single_suite(ws, monkeypatch) -> None:
    monkeypatch.setenv("SDR_TEST_TENANT", TENANT)
    (ws / "integration-test-summary.json").write_text(
        json.dumps(
            {
                "scenarios": [
                    _scenario("a"),
                    _scenario("b", "failed"),
                    _scenario("c", "weird"),
                    {"name": "auth", "api": "auth"},
                    _scenario("nostart", run=""),
                ]
            }
        )
    )
    assert sdr_report.main(["workflow-links"]) == 0
    assert _output(ws, "links") == (
        f"✅ [a]({_url()}) · ❌ [b]({_url()}) · ❓ [c]({_url()})"
    )


def test_links_grouped_by_class_for_multiple_suites(ws, monkeypatch) -> None:
    monkeypatch.setenv("SDR_TEST_TENANT", TENANT)
    # Per-class files are named after the class (application_sdk.testing.sdr.base
    # writes integration-test-summary-<cls.__name__>.json); that name wins.
    (ws / "integration-test-summary-TestEcc.json").write_text(
        json.dumps({"scenarios": [_scenario("one"), _scenario("orphan")]})
    )
    (ws / "integration-test-summary-TestS4.json").write_text(
        json.dumps({"scenarios": [_scenario("two", "skipped")]})
    )
    # The shared file is ignored once per-class files exist.
    (ws / "integration-test-summary.json").write_text(
        json.dumps({"scenarios": [_scenario("shared")]})
    )
    sdr_report.main(["workflow-links"])
    assert _output(ws, "links").split("\n") == [
        "",
        f"  - **TestEcc:** ✅ [one]({_url()}) · ✅ [orphan]({_url()})",
        f"  - **TestS4:** ⏭ [two]({_url()})",
    ]


def test_a_scenario_name_shared_by_two_classes_stays_with_each_class(
    ws, monkeypatch
) -> None:
    """A name -> class map from JUnit keeps one class per name; the per-class
    file is the authority, so both `create` links land under their own class."""
    monkeypatch.setenv("SDR_TEST_TENANT", TENANT)
    _junit(
        ws,
        '<testsuite><testcase classname="tests.e2e.TestEcc" name="test_create"/>'
        '<testcase classname="tests.e2e.TestS4" name="test_create"/></testsuite>',
    )
    (ws / "integration-test-summary-TestEcc.json").write_text(
        json.dumps({"scenarios": [_scenario("create", wf="ecc")]})
    )
    (ws / "integration-test-summary-TestS4.json").write_text(
        json.dumps({"scenarios": [_scenario("create", wf="s4")]})
    )
    sdr_report.main(["workflow-links"])
    assert _output(ws, "links").split("\n") == [
        "",
        f"  - **TestEcc:** ✅ [create]({_url(wf='ecc')})",
        f"  - **TestS4:** ✅ [create]({_url(wf='s4')})",
    ]


def test_shared_summary_groups_by_junit_class(ws, monkeypatch) -> None:
    monkeypatch.setenv("SDR_TEST_TENANT", TENANT)
    _junit(
        ws,
        '<testsuite><testcase classname="tests.e2e.TestEcc" name="test_one"/>'
        '<testcase classname="tests.e2e.TestS4" name="test_two"/></testsuite>',
    )
    (ws / "integration-test-summary.json").write_text(
        json.dumps({"scenarios": [_scenario("one"), _scenario("two"), _scenario("x")]})
    )
    sdr_report.main(["workflow-links"])
    assert _output(ws, "links").split("\n") == [
        "",
        f"  - **TestEcc:** ✅ [one]({_url()})",
        f"  - **TestS4:** ✅ [two]({_url()})",
        f"  - **(unknown suite):** ✅ [x]({_url()})",
    ]


def test_links_empty_without_a_tenant(ws, monkeypatch, capsys) -> None:
    monkeypatch.delenv("SDR_TEST_TENANT", raising=False)
    (ws / "integration-test-summary.json").write_text(
        json.dumps({"scenarios": [_scenario("a")]})
    )
    sdr_report.main(["workflow-links"])
    assert _output(ws, "links") == ""
    assert "(none)" in capsys.readouterr().out


def test_links_empty_without_summary_files(ws, monkeypatch) -> None:
    monkeypatch.setenv("SDR_TEST_TENANT", TENANT)
    sdr_report.main(["workflow-links"])
    assert _output(ws, "links") == ""


# --- step-summary ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "title"),
    [
        ("tests/e2e/sdr", "SDR Integration Tests (testcontainer)"),
        ("tests/e2e/full_dag/", "E2E Full Tests (system apps)"),
        ("tests/e2e/my-suite_x", "My Suite X Tests"),
        ("", "Tests"),
    ],
)
def test_title_from_test_path(path, title) -> None:
    assert sdr_report.title_from_test_path(path) == title


def test_step_summary_full_report(ws, monkeypatch) -> None:
    summary = ws / "summary.md"
    summary.write_text("stale")  # the step overwrites, it does not append
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("OUTCOME", "success")
    monkeypatch.setenv("APP_NAME", "mysql")
    monkeypatch.setenv("TEST_PATH", "tests/e2e/sdr")
    monkeypatch.delenv("REPORT_TITLE_OVERRIDE", raising=False)
    _junit(
        ws,
        '<testsuite tests="4" failures="1" errors="1" skipped="1" time="12.34">'
        '<testcase name="test_auth_ok" time="1.0"/>'
        '<testcase name="test_auth_bad" time="0.05"><failure message="boom"/></testcase>'
        '<testcase name="test_workflow_run" time="2"><error message="err"/></testcase>'
        '<testcase name="test_workflow_skip"><skipped message="nope"/></testcase>'
        "</testsuite>",
    )
    (ws / "results/sdr-test-output.txt").write_text(
        "noise\n===== 1 passed, 1 failed in 3s =====\n"
    )
    assert sdr_report.main(["step-summary"]) == 0
    text = summary.read_text()
    assert text.startswith("## ✅ SDR Integration Tests (testcontainer) — mysql\n")
    assert "| ✅ Passed  | 1 |" in text
    assert "| ❌ Failed  | 2 |" in text
    assert "| ⏱ Total time | 12.3s |" in text
    assert "```\n===== 1 passed, 1 failed in 3s =====\n```" in text
    assert "<b>Auth</b> (1 passed, 1 failed, 0 skipped)" in text
    assert "| ❌ | `auth_bad` — _boom_ | <0.1s |" in text
    assert "<b>Workflow</b> (0 passed, 1 failed, 1 skipped)" in text
    assert "| 💥 | `workflow_run` — _err_ | 2.0s |" in text


def test_step_summary_without_results_uses_the_override(ws, monkeypatch) -> None:
    summary = ws / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("OUTCOME", "failure")
    monkeypatch.setenv("APP_NAME", "mysql")
    monkeypatch.setenv("REPORT_TITLE_OVERRIDE", "Custom")
    sdr_report.main(["step-summary"])
    assert summary.read_text() == "## ❌ Custom — mysql\n\n"


# --- asset-summary -----------------------------------------------------------

CID1 = "aaaa-1111"
CID2 = "bbbb-2222"
LOG = "\n".join(
    [
        f"correlation_id={CID1} x - Starting workflow workflow_id=wf-1 run_id=run-1",
        f"correlation_id={CID1} x - Extracted table: 10 raw records",
        f"correlation_id={CID1} x - Transformed table: 9 records",
        f"correlation_id={CID1} x - Transformed column: 5 records",
        f"correlation_id={CID2} x - workflow_id=wf-2",
        f"correlation_id={CID2} x - Extracted schema: 2 raw records",
        "correlation_id=cccc-3333 x - auth only",
    ]
)


def test_asset_summary_without_a_container_log(ws, capsys) -> None:
    assert sdr_report.main(["asset-summary"]) == 0
    assert (ws / "gh_output").read_text() == "markdown=\n"
    assert "No container log" in capsys.readouterr().out


def test_asset_summary_without_asset_lines(ws) -> None:
    (ws / "results/sdr-container.log").write_text("correlation_id=ab x - hello\n")
    sdr_report.main(["asset-summary"])
    assert (ws / "gh_output").read_text() == "markdown=\n"


def test_asset_summary_tables(ws, monkeypatch) -> None:
    monkeypatch.setenv("SDR_TEST_TENANT", TENANT)
    (ws / "results/sdr-container.log").write_text(LOG)
    sdr_report.main(["asset-summary"])
    md = _output(ws, "markdown")
    url = "https://acme.atlan.com/api/temporal/namespaces/default/workflows/wf-1/run-1/history"
    assert md.split("\n")[:12] == [
        "### Extracted + transformed assets",
        "",
        f"**Workflow run 1** — [wf-1]({url})",
        "",
        "| Entity | Extracted | Transformed |",
        "|--------|-----------|-------------|",
        "| column | — | 5 |",
        "| table | 10 | 9 |",
        "",
        "**Workflow run 2** — `wf-2`",
        "",
        "| Entity | Extracted | Transformed |",
    ]
    assert "Assets in Atlas" not in md


def test_asset_summary_no_link_without_a_tenant(ws, monkeypatch) -> None:
    monkeypatch.delenv("SDR_TEST_TENANT", raising=False)
    (ws / "results/sdr-container.log").write_text(LOG)
    sdr_report.main(["asset-summary"])
    assert "**Workflow run 1** — `wf-1`\n" in _output(ws, "markdown")


def _with_inventory(ws: Path, atlas: str, lineage: str) -> None:
    (ws / "results/sdr-container.log").write_text(LOG)
    (ws / "results/sdr-test-output.txt").write_text(
        "Atlas inventory under old/qn: {'Table': 1}\n"
        f"Atlas inventory under default/x/1: {atlas}\n"
        f"Lineage inventory under default/x/1: {lineage}\n"
    )


def test_asset_summary_full_dag_sections(ws, monkeypatch) -> None:
    monkeypatch.setenv("TWO_STORE", "false")
    _with_inventory(
        ws, "{'Table': 3, 'Column': 7}", "{'Process': 2} lineage_present=True"
    )
    sdr_report.main(["asset-summary"])
    md = _output(ws, "markdown")
    assert (
        "### Assets in Atlas\n\n**Connection:** `default/x/1`\n\n**Total:** 10\n" in md
    )
    assert "| Column | 7 |\n| Table | 3 |" in md
    assert "### Lineage coverage" in md
    assert "Possible missing hand-off" not in md
    assert "No lineage assets observed" not in md


def test_asset_summary_two_store_handoff_warning(ws, monkeypatch) -> None:
    monkeypatch.setenv("TWO_STORE", "true")
    _with_inventory(ws, "{}", "{} lineage_present=False")
    sdr_report.main(["asset-summary"])
    md = _output(ws, "markdown")
    assert "transformed 14 record(s) locally but 0 assets reached Atlas" in md
    assert "No lineage assets observed" in md
    assert "**Total:** 0\n\n> ⚠️" in md


def test_asset_summary_bad_literal_counts_as_empty(ws, monkeypatch) -> None:
    monkeypatch.setenv("TWO_STORE", "false")
    _with_inventory(ws, "{not python}", "{'Process': 1} lineage_present=True")
    sdr_report.main(["asset-summary"])
    md = _output(ws, "markdown")
    assert (
        "### Assets in Atlas\n\n**Connection:** `default/x/1`\n\n**Total:** 0\n" in md
    )


def test_every_subcommand_is_invoked_by_the_action() -> None:
    action = (
        Path(__file__).parents[2] / "actions" / "sdr-e2e" / "action.yaml"
    ).read_text(encoding="utf-8")
    for command in ("workflow-links", "step-summary", "asset-summary"):
        assert f'sdr_report.py" {command}' in action
