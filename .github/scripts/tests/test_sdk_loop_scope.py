"""Scope routing — and why a one-agent scope must not dispatch."""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sdk_loop_common import DismissalLedger  # noqa: E402


class _Done:
    returncode = 0
    stdout = ""


import sdk_loop_phase  # noqa: E402
from sdk_loop_common import (  # noqa: E402
    REVIEW_MODEL,
    SCOPE_AGENTS,
    classify_scope,
    dispatch_set,
    opencode_config,
    solo_scope,
)
from sdk_loop_phase import review_prompt  # noqa: E402


def test_the_scope_table_matches_the_playbook() -> None:
    """SCOPE_AGENTS mirrors §2a's routing table, so the table has to stay the
    authority. Parsed rather than eyeballed: a scope silently routing to the
    wrong specialist reviews the PR through the wrong lens and still returns
    a confident verdict.
    """
    text = pathlib.Path(".mothership/pr-review/ORCHESTRATION.md").read_text(
        encoding="utf-8"
    )
    rows = re.findall(r"^\| `([a-z-]+)` \| (.+?) \|$", text, re.M)
    assert rows, "could not find §2a's routing table — did its shape change?"

    from_playbook: dict[str, set[str]] = {}
    for scope, cell in rows:
        if scope not in SCOPE_AGENTS:
            continue
        named = set(re.findall(r"([a-z-]+)\.md", cell))
        if "SKIP" in cell:
            named = set()
        from_playbook[scope] = named

    for scope, expected in from_playbook.items():
        assert set(SCOPE_AGENTS[scope]) == expected, (
            f"{scope}: playbook routes to {sorted(expected)}, "
            f"SCOPE_AGENTS has {sorted(SCOPE_AGENTS[scope])}"
        )


@pytest.mark.parametrize(
    "files,lines,expected",
    [
        ([".github/actionlint.yaml", ".pre-commit-config.yaml"], 85, "config-only"),
        (["packages/conformance/suite/checks/x.py"], 40, "conformance-only"),
        (["application_sdk/a.py", "application_sdk/b.py", "x/c.py"], 900, "full"),
        (["application_sdk/a.py"], 10, "minor"),
        (["tests/test_a.py"], 30, "tests-only"),
        (["contract-toolkit/src/x.pkl"], 20, "contract-toolkit"),
        ([".mothership/pr-review/ORCHESTRATION.md"], 50, "docs-only"),
        (["contract-toolkit/a.pkl", "application_sdk/b.py"], 60, "mixed-sdk-toolkit"),
        # Security paths never take the `minor` fast path — a three-line auth
        # change is exactly where a subtle blocker hides (§11).
        (["application_sdk/credentials.py"], 10, "full"),
    ],
)
def test_scope_classification(files: list[str], lines: int, expected: str) -> None:
    assert classify_scope(files, lines) == expected


def test_main_passes_no_subagents_for_a_solo_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Assert what PRODUCTION passes, not what a helper can be asked to build.

    Calling `opencode_config(..., with_subagents=False)` in a test proves only
    that the builder honours its own argument. It would stay green if `main()`
    started passing `subagents=True`, which is the regression that matters —
    so this drives `main()` and captures the kwargs `run_agent` receives.
    """
    captured: dict[str, object] = {}

    def fake_run_agent(model, prompt, cwd, timeout_s, **kwargs):
        captured.update(kwargs)
        captured["prompt"] = prompt
        return sdk_loop_phase.AgentResult(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(sdk_loop_phase, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        sdk_loop_phase, "pr_files", lambda r, p: [".github/workflows/x.yaml"]
    )
    monkeypatch.setattr(sdk_loop_phase, "diff_lines", lambda r, p: 85)
    monkeypatch.setattr(sdk_loop_phase, "live_head", lambda r, h: "a" * 40)
    monkeypatch.setattr(sdk_loop_phase, "_sh", lambda *a, **k: _Done())
    monkeypatch.setattr(sdk_loop_phase, "opencode_usage", lambda w: {})
    monkeypatch.setenv("PHASE", "review")
    monkeypatch.setenv("ROUND", "1")
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("HEAD_REF", "b")
    monkeypatch.setenv("BASE_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.example")
    sdk_loop_phase.main([])

    assert captured.get("subagents") is False, (
        "a config-only PR routes to one agent; registering any means `Task` "
        "can dispatch, and the cold-start cost this change removes comes back"
    )
    assert "Do not dispatch it" in str(captured.get("prompt", ""))


def test_the_registration_set_covers_everything_the_playbook_dispatches() -> None:
    """§2a's table is only the Wave 1 row. Registering exactly it looked
    right and silently broke two dispatches the playbook still asks for:
    §1b's `reachability` on full/mixed, and the mixed-partition specialist
    §2a and §11 add when a PR also carries config or conformance files.
    """
    # §1b: reachability on full and mixed-sdk-toolkit, and nowhere else.
    assert "reachability" in dispatch_set("full", ["application_sdk/a.py"])
    assert "reachability" in dispatch_set("mixed-sdk-toolkit", ["contract-toolkit/a"])
    for scope in ("config-only", "conformance-only", "tests-only", "minor"):
        assert "reachability" not in dispatch_set(scope, [])

    # §2a mixed partitions.
    full_cfg = dispatch_set("full", ["application_sdk/a.py", ".github/w.yaml"])
    assert "ci-config" in full_cfg
    full_conf = dispatch_set("full", ["application_sdk/a.py", "packages/conformance/c"])
    assert "conformance" in full_conf

    # ...but NOT for an incidental lockfile bump, per §2a's own carve-out.
    assert "ci-config" not in dispatch_set("full", ["application_sdk/a.py", "uv.lock"])

    # §11: a conformance PR that also carries config is TWO agents, so it is
    # not solo — treating it as solo would drop the CI specialist from a PR
    # that changes CI.
    both = ["packages/conformance/c.py", ".github/workflows/x.yaml"]
    assert set(dispatch_set("conformance-only", both)) == {"conformance", "ci-config"}
    assert solo_scope("conformance-only", both) == ""
    assert (
        solo_scope("conformance-only", ["packages/conformance/c.py"]) == "conformance"
    )


def test_registration_matches_the_dispatch_set() -> None:
    """Whatever the playbook may dispatch must be registered, exactly."""
    monkey = pytest.MonkeyPatch()
    monkey.setenv("LITELLM_BASE_URL", "https://gateway.example")
    try:
        for scope, files in (
            ("full", ["application_sdk/a.py", ".github/w.yaml"]),
            ("mixed-sdk-toolkit", ["contract-toolkit/a.pkl", "application_sdk/b.py"]),
            ("tests-focused", ["tests/t.py", "application_sdk/a.py"]),
        ):
            want = dispatch_set(scope, files)
            cfg = opencode_config(REVIEW_MODEL, with_subagents=True, subagents=want)
            assert set(cfg["agent"]) == set(want)
    finally:
        monkey.undo()


def test_the_prompt_tells_a_solo_review_not_to_dispatch() -> None:
    monkey = pytest.MonkeyPatch()
    monkey.setenv("LITELLM_BASE_URL", "https://gateway.example")
    try:
        solo = review_prompt(
            1, 1, "a" * 40, DismissalLedger(), scope="config-only", solo="ci-config"
        )
        assert "Do not dispatch it" in solo
        assert "agents/ci-config.md" in solo
        files = ["application_sdk/a.py"]
        fan = review_prompt(
            1,
            1,
            "a" * 40,
            DismissalLedger(),
            scope="full",
            agents=dispatch_set("full", files),
        )
        assert "do them in parallel" in fan
        # The prompt names the REGISTERED set, so §1b's reachability must
        # appear — a `full` review is told to dispatch it.
        for name in ("correctness", "quality", "structure", "reachability"):
            assert name in fan
    finally:
        monkey.undo()
