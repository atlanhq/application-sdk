"""Tests for .github/scripts/test_command_comment.py and its wiring (FND-3839).

Each PR carries one sticky comment per command, found by a hidden marker and
edited in place, always describing the latest request only. What has to hold:

* every rendered stage carries the marker (or the next writer cannot find the
  comment and posts a second one), and only a final stage says how to re-run;
* the e2e suite × cloud grid is read from the run's own job names, where a
  suite may itself contain hyphens;
* a failed comment write never fails the job it runs in — those jobs own
  required checks and commit statuses, and the comment is only a view;
* the workflows invoke the CLI under the conditions that make it meaningful,
  with the permissions its API calls need.

``gh`` is stubbed through ``GitHub``'s ``run`` seam; nothing touches the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import pr_test_command as ptc  # noqa: E402
import test_command_comment as tcc  # noqa: E402
from conformance.bootstrap import render as bootstrap_render  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github/workflows"
TEMPLATES = REPO_ROOT / "packages/conformance/conformance/bootstrap/templates"

REPO = "atlanhq/atlan-example-app"
SHA = "a96a27b0000000000000000000000000000000aa"
RUN_URL = "https://github.com/atlanhq/atlan-example-app/actions/runs/1"
CLOUDS = ["aws", "azure", "gcp"]

ALL_STAGES = (
    tcc.REQUESTED,
    tcc.RUNNING,
    tcc.PASSED,
    tcc.FAILED,
    tcc.NO_VERDICT,
    tcc.NOT_STARTED,
)
FINAL_STAGES = (tcc.PASSED, tcc.FAILED, tcc.NO_VERDICT, tcc.NOT_STARTED)


# ── render ─────────────────────────────────────────────────────────────────


class TestRender:
    @pytest.mark.parametrize("command", tcc.COMMANDS)
    @pytest.mark.parametrize("stage", ALL_STAGES)
    def test_every_stage_carries_the_marker_first(
        self, command: str, stage: str
    ) -> None:
        body = tcc.render(tcc.CommentState(command, stage, SHA))
        assert body.startswith(tcc.marker(command) + "\n")
        assert f"`/{command}`" in body.splitlines()[1]
        assert f"on `{SHA[:7]}`" in body.splitlines()[1]

    @pytest.mark.parametrize("stage", ALL_STAGES)
    def test_rerun_hint_only_on_final_stages(self, stage: str) -> None:
        """A request still in flight must not invite a second one."""
        body = tcc.render(tcc.CommentState(tcc.E2E, stage, SHA))
        assert ("Re-run:" in body) is (stage in FINAL_STAGES)

    def test_the_markers_differ_per_command(self) -> None:
        """Otherwise `/gate-check` would overwrite the `/e2e` comment."""
        assert tcc.marker(tcc.E2E) != tcc.marker(tcc.GATE_CHECK)
        assert tcc.marker(tcc.E2E) not in tcc.marker(tcc.GATE_CHECK)
        assert tcc.marker(tcc.GATE_CHECK) not in tcc.marker(tcc.E2E)

    def test_meta_line_note_and_details(self) -> None:
        body = tcc.render(
            tcc.CommentState(
                tcc.E2E,
                tcc.FAILED,
                SHA,
                actor="a-human",
                summary="1 of 2 leg(s) failed",
                links=(tcc.Link("run", RUN_URL),),
                details_title="Results",
                details="| a | b |",
                note="Something to know.",
            )
        )
        lines = body.splitlines()
        assert lines[2] == (
            f"1 of 2 leg(s) failed · requested by @a-human · [run]({RUN_URL})"
        )
        assert "Something to know." in lines
        assert "<details><summary>Results</summary>" in lines
        assert "| a | b |" in lines and "</details>" in lines
        assert lines[-1].startswith("Re-run:")

    def test_bare_state_has_no_meta_line(self) -> None:
        body = tcc.render(tcc.CommentState(tcc.E2E, tcc.REQUESTED, SHA))
        assert body.splitlines() == [
            tcc.marker(tcc.E2E),
            f"### `/e2e` · ⏳ requested on `{SHA[:7]}`",
        ]

    @pytest.mark.parametrize(
        ("result", "glyph"),
        [("success", "✅"), ("failure", "❌"), (None, "⏳"), ("", "⏳"), ("odd", "⚠️")],
    )
    def test_glyph(self, result: str | None, glyph: str) -> None:
        assert tcc.glyph(result) == glyph


# ── e2e legs and table ─────────────────────────────────────────────────────


def _job(leg: str, conclusion: str | None = "success", status: str = "completed"):
    return {
        "name": f"e2e / End-to-end ({leg})",
        "conclusion": conclusion,
        "status": status,
    }


class TestLegsFromJobs:
    def test_hyphenated_suites_split_on_the_known_cloud(self) -> None:
        jobs = [
            _job("full-dag-aws"),
            _job("full-dag-azure", "failure"),
            _job("lineage-gcp"),
            {"name": "e2e / Unit", "conclusion": "success"},
            {"name": "e2e / Discover e2e suites", "conclusion": "success"},
        ]
        assert tcc.legs_from_jobs(jobs, CLOUDS) == [
            ("full-dag", "aws", "success"),
            ("full-dag", "azure", "failure"),
            ("lineage", "gcp", "success"),
        ]

    def test_a_suite_named_like_a_cloud_suffix_needs_the_hyphen(self) -> None:
        """`awsome` does not end in `-aws`, so it is not split."""
        assert tcc.legs_from_jobs([_job("awsome")], CLOUDS) == [
            ("awsome", "", "success")
        ]

    def test_no_cloud_runs_keep_the_whole_name(self) -> None:
        jobs = [_job("full-dag-aws"), _job("smoke")]
        assert tcc.legs_from_jobs(jobs, []) == [
            ("full-dag-aws", "", "success"),
            ("smoke", "", "success"),
        ]

    def test_an_unfinished_leg_reports_its_status(self) -> None:
        jobs = [_job("smoke", None, "in_progress"), {"name": None}]
        assert tcc.legs_from_jobs(jobs, []) == [("smoke", "", "in_progress")]


class TestE2eTable:
    def test_grid_one_row_per_suite_one_column_per_cloud(self) -> None:
        legs = [
            ("full-dag", "aws", "success"),
            ("full-dag", "azure", "failure"),
            ("lineage", "gcp", "cancelled"),
        ]
        assert tcc.e2e_table(legs, CLOUDS).splitlines() == [
            "| suite | aws | azure | gcp |",
            "|---|---|---|---|",
            "| full-dag | ✅ | ❌ | – |",
            "| lineage | – | – | 🚫 |",
        ]

    @pytest.mark.parametrize("clouds", [[], CLOUDS])
    def test_flat_when_there_is_no_cloud_dimension(self, clouds: list[str]) -> None:
        legs = [("smoke", "", "success"), ("full-dag", "", "failure")]
        assert tcc.e2e_table(legs, clouds).splitlines() == [
            "| suite | result |",
            "|---|---|",
            "| smoke | ✅ |",
            "| full-dag | ❌ |",
        ]

    def test_no_legs_no_table(self) -> None:
        assert tcc.e2e_table([], CLOUDS) == ""


class TestE2eVerdictState:
    LEGS = [("a", "aws", "success"), ("a", "gcp", "failure"), ("b", "aws", "skipped")]

    def test_success(self) -> None:
        state = tcc.e2e_verdict_state(
            sha=SHA,
            verdict="success",
            legs=self.LEGS[:1],
            clouds=CLOUDS,
            run_url=RUN_URL,
        )
        assert (state.stage, state.summary) == (tcc.PASSED, "1 leg(s) passed")
        assert state.links == (tcc.Link("run", RUN_URL),)
        assert state.details_title == "Results (suite × cloud)"
        assert "| suite | aws | azure | gcp |" in state.details

    def test_failure_counts_failed_legs_only(self) -> None:
        """A skipped leg is not a failure."""
        state = tcc.e2e_verdict_state(
            sha=SHA, verdict="failure", legs=self.LEGS, clouds=CLOUDS, run_url=RUN_URL
        )
        assert (state.stage, state.summary) == (tcc.FAILED, "1 of 3 leg(s) failed")

    @pytest.mark.parametrize("verdict", ["error", "cancelled", ""])
    def test_anything_else_is_no_verdict(self, verdict: str) -> None:
        state = tcc.e2e_verdict_state(
            sha=SHA, verdict=verdict, legs=[], clouds=[], run_url=RUN_URL
        )
        assert state.stage == tcc.NO_VERDICT
        assert "no verdict" in state.summary
        assert state.details_title == "Results"

    def test_extra_markdown_follows_the_results(self) -> None:
        """The e2e preview links ride on the verdict, after the results table
        and before the re-run hint."""
        state = tcc.e2e_verdict_state(
            sha=SHA,
            verdict="success",
            legs=self.LEGS[:1],
            clouds=CLOUDS,
            run_url=RUN_URL,
            extra="### Preview: assets this run published\n",
        )
        body = tcc.render(state)
        assert (
            body.index("</details>") < body.index("### Preview") < body.index("Re-run")
        )


class TestReadExtra:
    def test_a_missing_file_is_no_extra(self, tmp_path: Path) -> None:
        assert tcc._read_extra(str(tmp_path / "absent.md")) == ""

    def test_an_oversized_file_is_dropped_so_the_verdict_still_fits(
        self, tmp_path: Path
    ) -> None:
        big = tmp_path / "big.md"
        big.write_text("x" * (tcc.MAX_EXTRA_CHARS + 1))
        assert tcc._read_extra(str(big)) == ""

    def test_a_file_is_read_whole(self, tmp_path: Path) -> None:
        small = tmp_path / "small.md"
        small.write_text("### Preview\n")
        assert tcc._read_extra(str(small)) == "### Preview\n"


class TestSdkE2eState:
    def _check(self, name: str, conclusion: str | None, url: str = "") -> dict:
        return {
            "name": name,
            "conclusion": conclusion,
            "status": "completed",
            "html_url": url,
        }

    def test_one_row_per_connector_check_only(self) -> None:
        checks = [
            self._check("Connector E2E run / mysql", "success", "https://x.invalid/1"),
            self._check("Connector E2E run / metabase", "failure"),
            self._check("Unit tests", "failure"),
            self._check("Connector gate", "failure"),
        ]
        state = tcc.sdk_e2e_state(sha=SHA, checks=checks, run_url=RUN_URL)
        assert (state.stage, state.summary) == (
            tcc.FAILED,
            "1 of 2 connector(s) failed",
        )
        assert state.details.splitlines() == [
            "| connector | result |",
            "|---|---|",
            "| metabase | ❌ |",
            "| mysql | [✅](https://x.invalid/1) |",
        ]

    def test_all_passed(self) -> None:
        checks = [self._check("Connector E2E run / mysql", "success")]
        state = tcc.sdk_e2e_state(sha=SHA, checks=checks, run_url=RUN_URL)
        assert (state.stage, state.summary) == (tcc.PASSED, "1 connector(s) passed")

    @pytest.mark.parametrize("checks", [[], [{"name": "Unit tests"}, {"name": None}]])
    def test_none_reported_is_no_verdict(self, checks: list) -> None:
        state = tcc.sdk_e2e_state(sha=SHA, checks=checks, run_url=RUN_URL)
        assert state.stage == tcc.NO_VERDICT
        assert state.details == ""

    def test_an_unfinished_check_is_not_a_pass(self) -> None:
        checks = [
            {
                "name": "Connector E2E run / mysql",
                "conclusion": None,
                "status": "in_progress",
            }
        ]
        assert tcc.sdk_e2e_state(sha=SHA, checks=checks, run_url=RUN_URL).stage == (
            tcc.FAILED
        )


def test_gate_check_table() -> None:
    rows = [
        ("tests.yaml", "success", "https://x.invalid/1"),
        ("checks.yml", "failure", ""),
    ]
    assert tcc.gate_check_table(rows).splitlines() == [
        "| workflow | result |",
        "|---|---|",
        "| `tests.yaml` | [✅](https://x.invalid/1) |",
        "| `checks.yml` | ❌ |",
    ]


# ── I/O ────────────────────────────────────────────────────────────────────


class FakeApi:
    """`gh api` for the comment endpoints, jobs and check runs."""

    def __init__(
        self,
        *,
        comments: list[dict[str, Any]] | None = None,
        jobs: list[dict[str, Any]] | None = None,
        checks: list[dict[str, Any]] | None = None,
        fail: tuple[str, ...] = (),
    ) -> None:
        self.comments = list(comments or [])
        self.jobs = jobs or []
        self.checks = checks or []
        self.fail = fail
        self.posted: list[str] = []
        self.patched: list[tuple[int, str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        joined = " ".join(args)
        if any(needle in joined for needle in self.fail):
            return subprocess.CompletedProcess(args, 1, "", "boom")
        out: Any = {}
        if "-X" in args:
            path = args[args.index("-X") + 2]
            body = next(a for a in args if a.startswith("body=")).split("=", 1)[1]
            if args[args.index("-X") + 1] == "PATCH":
                self.patched.append((int(path.rsplit("/", 1)[1]), body))
            else:
                self.posted.append(body)
        else:
            path = next(a for a in args[1:] if a.startswith("repos/"))
            if "/comments?" in path:
                out = [self.comments]
            elif "/jobs?" in path:
                out = [{"jobs": self.jobs}]
            elif "/check-runs?" in path:
                out = [{"check_runs": self.checks}]
            else:
                raise AssertionError(f"unexpected gh call: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(out), "")


STATE = tcc.CommentState(tcc.E2E, tcc.RUNNING, SHA)


class TestUpsert:
    def test_creates_when_absent(self) -> None:
        api = FakeApi(comments=[{"id": 1, "body": "unrelated"}])
        tcc.upsert(ptc.GitHub(REPO, api), 12, STATE)
        assert api.posted == [tcc.render(STATE)]
        assert api.patched == []

    def test_edits_the_newest_marked_comment(self) -> None:
        mark = tcc.marker(tcc.E2E)
        api = FakeApi(
            comments=[
                {"id": 5, "body": f"{mark}\nold"},
                {"id": 8, "body": f"{mark}\nnewer"},
                {"id": 9, "body": tcc.marker(tcc.GATE_CHECK)},
            ]
        )
        tcc.upsert(ptc.GitHub(REPO, api), 12, STATE)
        assert api.patched == [(8, tcc.render(STATE))]
        assert api.posted == []

    @pytest.mark.parametrize("failing", ["/comments?", "PATCH", "POST"])
    def test_try_upsert_never_raises(
        self, capsys: pytest.CaptureFixture[str], failing: str
    ) -> None:
        """The comment is a view of the verdict; it must not fail the job."""
        api = FakeApi(
            comments=[{"id": 8, "body": tcc.marker(tcc.E2E)}], fail=(failing,)
        )
        if failing == "POST":
            api.comments = []
        tcc.try_upsert(ptc.GitHub(REPO, api), 12, STATE)
        assert "::warning::could not update the /e2e comment" in capsys.readouterr().err


# ── CLI ────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch):
    def run(api: FakeApi, *args: str, pr: str = "12") -> int:
        real = ptc.GitHub
        monkeypatch.setattr(tcc, "GitHub", lambda repo: real(repo, api))
        return tcc.main(
            ["--repo", REPO, "--pr", pr, "--sha", SHA, "--run-url", RUN_URL, *args]
        )

    return run


class TestCli:
    @pytest.mark.parametrize("pr", ["", "  ", "abc"])
    @pytest.mark.parametrize(
        "args",
        [
            ("e2e-running",),
            ("e2e-verdict", "--run-id", "1", "--verdict", "success"),
            ("sdk-e2e-verdict",),
        ],
    )
    def test_no_pr_number_is_a_noop(
        self, cli, monkeypatch: pytest.MonkeyPatch, pr: str, args: tuple[str, ...]
    ) -> None:
        """A dispatch from an older caller passes no PR: touch nothing."""
        monkeypatch.setattr(tcc, "GitHub", lambda repo: pytest.fail("no API calls"))
        assert (
            tcc.main(
                ["--repo", REPO, "--pr", pr, "--sha", SHA, "--run-url", RUN_URL, *args]
            )
            == 0
        )

    @pytest.mark.parametrize(("legs", "summary"), [("6", "6 leg(s)"), ("", "")])
    def test_e2e_running(self, cli, legs: str, summary: str) -> None:
        api = FakeApi()
        assert cli(api, "e2e-running", "--legs", legs) == 0
        (body,) = api.posted
        assert "🏃 running" in body
        assert ("6 leg(s)" in body) is bool(summary)
        assert f"[run]({RUN_URL})" in body
        assert "Re-run:" not in body

    def test_e2e_verdict_reads_the_runs_jobs(self, cli) -> None:
        api = FakeApi(jobs=[_job("full-dag-aws"), _job("full-dag-gcp", "failure")])
        assert (
            cli(
                api,
                "e2e-verdict",
                "--run-id",
                "77",
                "--verdict",
                "failure",
                "--clouds",
                "aws,gcp",
            )
            == 0
        )
        (body,) = api.posted
        assert "❌ failed" in body
        assert "1 of 2 leg(s) failed" in body
        assert "| full-dag | ✅ | ❌ |" in body

    def test_e2e_verdict_with_unreadable_jobs_still_posts(self, cli, capsys) -> None:
        api = FakeApi(fail=("/jobs?",))
        assert cli(api, "e2e-verdict", "--run-id", "77", "--verdict", "success") == 0
        (body,) = api.posted
        assert "✅ passed" in body
        assert "could not read the run's jobs" in capsys.readouterr().err

    def test_sdk_e2e_verdict(self, cli) -> None:
        api = FakeApi(
            comments=[{"id": 3, "body": tcc.marker(tcc.E2E)}],
            checks=[{"name": "Connector E2E run / mysql", "conclusion": "success"}],
        )
        assert cli(api, "sdk-e2e-verdict") == 0
        ((comment_id, body),) = api.patched
        assert comment_id == 3
        assert "✅ passed" in body and "| mysql | ✅ |" in body

    def test_sdk_e2e_verdict_with_unreadable_checks_writes_nothing(
        self, cli, capsys
    ) -> None:
        """No verdict is better than a wrong "no connector e2e check reported"."""
        api = FakeApi(fail=("/check-runs",))
        assert cli(api, "sdk-e2e-verdict") == 0
        assert api.posted == [] and api.patched == []
        assert "could not read the check runs" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "args",
        [
            ("e2e-running",),
            ("e2e-verdict", "--run-id", "1", "--verdict", "success"),
            ("sdk-e2e-verdict",),
        ],
    )
    def test_a_failed_write_still_exits_zero(self, cli, args: tuple[str, ...]) -> None:
        """Every subcommand runs inside a job that owns a required check."""
        api = FakeApi(fail=("/comments",))
        assert cli(api, *args) == 0


# ── wiring ─────────────────────────────────────────────────────────────────


def _render(name: str) -> str:
    with mock.patch.object(
        bootstrap_render,
        "_load_template",
        lambda n: (TEMPLATES / n).read_text(encoding="utf-8"),
    ):
        return bootstrap_render.render(name)


def _on(doc: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) reads a bare `on:` key as boolean True.
    return doc.get("on") or doc.get(True) or {}


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [s for s in job.get("steps") or [] if s.get("name") == name]
    assert matches, f"lost the {name!r} step"
    return matches[0]


def _flat(text: Any) -> str:
    return " ".join(str(text).split())


class TestWiring:
    def test_template_declares_and_passes_the_pr_number(self) -> None:
        doc = yaml.safe_load(_render("tests.yaml"))
        assert "pr_number" in _on(doc)["workflow_dispatch"]["inputs"]
        assert ptc.dispatch_inputs(_render("tests.yaml")) >= {"pr_number"}
        assert doc["jobs"]["tests"]["with"]["pr-number"] == "${{ inputs.pr_number }}"

    def test_reusable_declares_the_pr_number_input(self) -> None:
        inputs = _on(_workflow("tests-reusable.yaml"))["workflow_call"]["inputs"]
        assert inputs["pr-number"]["default"] == ""
        assert inputs["pr-number"]["required"] is False

    def test_running_step(self) -> None:
        job = _workflow("tests-reusable.yaml")["jobs"]["discover-e2e"]
        step = _step(job, "Show the /e2e request as running on the PR")
        condition = _flat(step["if"])
        assert "github.event_name == 'workflow_dispatch'" in condition
        assert "inputs.run-e2e == 'only'" in condition
        assert "inputs.pr-number != ''" in condition
        assert step["continue-on-error"] is True
        assert "e2e-running" in step["run"]
        # The checkout it runs from shares the condition, or it runs nothing.
        fetch = _step(job, "Fetch the comment driver")
        assert _flat(fetch["if"]) == condition
        assert step["run"].split()[1].startswith(fetch["with"]["path"] + "/")

    def test_verdict_step_in_the_gate(self) -> None:
        job = _workflow("tests-reusable.yaml")["jobs"]["tests-passed"]
        step = _step(job, "Post the /e2e verdict on the PR")
        condition = _flat(step["if"])
        assert condition.startswith("always()")
        assert "inputs.run-e2e == 'only'" in condition
        assert "inputs.pr-number != ''" in condition
        assert step["continue-on-error"] is True
        assert "e2e-verdict" in step["run"]
        assert "needs.discover-e2e.outputs.clouds" in step["env"]["CLOUDS"]

    def test_gate_can_read_its_own_jobs(self) -> None:
        """`run_jobs` needs `actions: read`; without it the grid is empty."""
        permissions = _workflow("tests-reusable.yaml")["jobs"]["tests-passed"][
            "permissions"
        ]
        assert permissions["actions"] == "read"
        assert permissions["pull-requests"] == "write"

    def test_e2e_job_names_are_what_legs_from_jobs_parses(self) -> None:
        job = _workflow("tests-reusable.yaml")["jobs"]["e2e"]
        assert job["name"] == "End-to-end (${{ matrix.name }})"
        assert tcc.legs_from_jobs(
            [{"name": "e2e / " + job["name"].replace("${{ matrix.name }}", "x-aws")}],
            ["aws"],
        ) == [("x", "aws", "")]

    def test_sdk_connector_gate_step(self) -> None:
        job = _workflow("pull_request.yaml")["jobs"]["connector-gate"]
        assert "e2e-request" in job["needs"]
        assert job["permissions"]["pull-requests"] == "write"
        # check_runs reads the connector e2e checks on the head commit.
        assert job["permissions"]["checks"] == "read"
        step = _step(job, "Post the /e2e verdict on the PR")
        condition = _flat(step["if"])
        assert "needs.e2e-request.outputs.requested == 'true'" in condition
        assert "steps.upstream.outputs.dispatched == 'true'" in condition
        assert step["continue-on-error"] is True
        assert "sdk-e2e-verdict" in step["run"]
        assert any(s.get("id") == "upstream" for s in job["steps"])
