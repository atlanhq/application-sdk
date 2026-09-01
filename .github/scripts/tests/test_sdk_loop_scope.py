"""Scope routing — and why a one-agent scope must not dispatch."""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sdk_loop_common import DismissalLedger  # noqa: E402
from sdk_loop_common import (  # noqa: E402
    REVIEW_MODEL,
    SCOPE_AGENTS,
    classify_scope,
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


def test_a_one_agent_scope_registers_no_subagents() -> None:
    """The fix, made structural rather than instructed.

    A dispatch exists to run agents concurrently. With one agent there is
    nothing to run alongside, so it buys no parallelism and costs a cold
    start — the parent has read the playbook, the diff and every changed
    file, and the sub-agent starts with none of it. Three such reviews spent
    904s, ~9min and 26min inside that single call, per-step latency climbing
    4s to 526s as the re-read context accumulated.

    Registering nothing means `Task` has nothing to delegate to, so the rule
    holds by construction. Prose was not enough: the comparable "fetch once"
    instruction in the research preamble is demonstrably ignored.
    """
    monkey = pytest.MonkeyPatch()
    monkey.setenv("LITELLM_BASE_URL", "https://gateway.example")
    try:
        for scope in ("config-only", "conformance-only", "tests-only", "minor"):
            assert solo_scope(scope), f"{scope} should route to exactly one agent"
            cfg = opencode_config(REVIEW_MODEL, with_subagents=False)
            assert "agent" not in cfg

        # Two or more is a different trade and dispatch stays: one agent
        # covering several domains produces a worse verdict, silently.
        for scope in ("full", "mixed-sdk-toolkit", "tests-focused"):
            assert not solo_scope(scope)
            cfg = opencode_config(
                REVIEW_MODEL, with_subagents=True, subagents=SCOPE_AGENTS[scope]
            )
            assert set(cfg["agent"]) == set(SCOPE_AGENTS[scope]), (
                "register exactly the routed agents — a scope must not be able "
                "to dispatch a specialist §2a did not choose"
            )
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
        fan = review_prompt(1, 1, "a" * 40, DismissalLedger(), scope="full")
        assert "dispatch them in parallel" in fan
        assert "correctness, quality, structure" in fan
    finally:
        monkey.undo()
