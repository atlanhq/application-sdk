"""Tests for conformance_remediation_e2e.py — the one-unit e2e dispatcher."""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_MOD_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "conformance_remediation_e2e.py"
)
_spec = importlib.util.spec_from_file_location("conformance_remediation_e2e", _MOD_PATH)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

# parents: [0]=tests, [1]=scripts, [2]=.github, [3]=repo root
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.fixture()
def payload(monkeypatch: pytest.MonkeyPatch) -> dict:
    # The script reads the playbook from the working directory; the suite may run
    # from anywhere, so pin cwd to the repo root the files actually live in.
    monkeypatch.chdir(REPO_ROOT)
    return mod.build_payload(
        repo="atlanhq/atlan-netsuite-app",
        rule_id="L011",
        suite_version="0.20.1",
        gha_run_url="https://gha/run/1",
    )


# ── payload shape ─────────────────────────────────────────────────────────


def test_both_models_are_pinned(payload: dict) -> None:
    """small_fast_model must be explicit — `fast = small_fast_model or model`."""
    assert payload["model"] == "kimi-k3"
    assert payload["small_fast_model"] == "gpt-5.6-luna"
    assert payload["env_vars"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "gpt-5.6-luna"


def test_the_gateway_key_permits_both_models(payload: dict) -> None:
    assert payload["ai_gateway_key_name"] == "sdk_review"


def test_session_files_come_from_this_checkout(payload: dict) -> None:
    """The whole point: the branch under test supplies the playbook, so the e2e
    needs nothing merged to main."""
    files = payload["session_files"]
    assert (
        "# Conformance Remediation — Orchestration Playbook"
        in files["/workspace/.mothership/session/REMEDIATION.md"]
    )
    assert files["/workspace/.mothership/session/PRIOR_DECISIONS.json"] == "[]"
    assert "/home/sandbox/.claude/skills/remediate/SKILL.md" in files


def test_every_session_file_uses_a_blessed_prefix(payload: dict) -> None:
    for path in payload["session_files"]:
        assert path.startswith(
            ("/workspace/.mothership/session/", "/home/sandbox/.claude/skills/")
        ), path


def test_the_run_targets_main_and_clones_only_the_target(payload: dict) -> None:
    assert payload["base_branch"] == "main"
    assert payload["repositories"] == ["atlanhq/atlan-netsuite-app"]


def test_the_stream_flag_is_on_because_gha_sits_behind_nginx(payload: dict) -> None:
    assert payload["stream"] is True


def test_the_prompt_pins_the_unit(payload: dict) -> None:
    text = payload["prompt"]
    assert "RULE_ID:             L011" in text
    assert "DELIVERY:            one_pr_per_rule" in text
    assert "SUITE_VERSION:       0.20.1" in text
    assert "Never request interactive input" in text


def test_missing_playbook_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """Dispatching a rover with no playbook must be impossible, not silent."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="REMEDIATION|ORCHESTRATION|playbook"):
        mod.build_payload(
            repo="atlanhq/x", rule_id="L011", suite_version="0.20.1", gha_run_url=""
        )


# ── SSE parsing + summary mining ──────────────────────────────────────────


def _sse(events: list[tuple[str, dict]]) -> list[str]:
    lines: list[str] = []
    for name, data in events:
        lines += [f"event: {name}", f"data: {json.dumps(data)}", ""]
    return lines


def test_response_text_is_buffered_and_mined() -> None:
    report = (
        "=== REMEDIATION SUMMARY ===\n"
        "repo: atlanhq/atlan-netsuite-app\nrule: L011\n"
        "findings_before: 2\nfindings_after: 0\ncleared: 2\n"
        "pr_url: https://github.com/atlanhq/atlan-netsuite-app/pull/99\n"
        "main_model: kimi-k3\nsubagent_model: gpt-5.6-luna\n"
        "=== END REMEDIATION SUMMARY ===\n"
        "RESULT: pushed:abc123\n"
    )
    st = mod.process_stream(
        _sse(
            [
                ("started", {"session_id": "s1", "sandbox_id": "b1"}),
                ("response", {"text": report}),
                ("complete", {"status": "completed", "cost_usd": "0.42"}),
            ]
        )
    )
    fields, kind, detail = mod.mine_summary(st.buffer)
    assert st.completed and st.status == "completed"
    assert fields["cleared"] == "2"
    assert fields["main_model"] == "kimi-k3"
    assert (kind, detail) == ("pushed", "abc123")


def test_the_template_inside_the_playbook_is_not_mined() -> None:
    """The playbook (quoted into context) contains a template copy of the block;
    the LAST occurrence — the real report — must win."""
    template = (
        "=== REMEDIATION SUMMARY ===\nrepo: TEMPLATE\n=== END REMEDIATION SUMMARY ===\n"
        "RESULT: pushed:<sha> | exists:<url>\n"
    )
    real = (
        "=== REMEDIATION SUMMARY ===\nrepo: atlanhq/real\n=== END REMEDIATION SUMMARY ===\n"
        "RESULT: no-op: rule detects clean\n"
    )
    fields, kind, _ = mod.mine_summary(template + real)
    assert fields["repo"] == "atlanhq/real"
    assert kind == "no-op"


def test_the_buffer_keeps_the_tail_when_capped() -> None:
    st = mod.StreamState()
    st.append_text("x" * (mod.BUFFER_CAP_BYTES + 100))
    st.append_text("RESULT: pushed:tail")
    assert len(st.buffer) <= mod.BUFFER_CAP_BYTES
    assert st.buffer.endswith("RESULT: pushed:tail")


def test_elicitation_is_a_failure_not_a_pause() -> None:
    st = mod.process_stream(_sse([("elicitation", {"q": "?"})]))
    assert st.errored
    assert st.err_code == "elicitation"


# ── exit decisions ────────────────────────────────────────────────────────


def _completed_state() -> "mod.StreamState":  # type: ignore[name-defined]
    st = mod.StreamState()
    st.got_event = True
    st.completed = True
    st.status = "completed"
    return st


def test_green_requires_both_a_completed_sandbox_and_a_result_line() -> None:
    """Independent failures: a sandbox can complete with the rover never having
    reported, and a rover can report just before the sandbox dies."""
    code, _ = mod.decide_exit(_completed_state(), "pushed")
    assert code == 0
    code, msg = mod.decide_exit(_completed_state(), "")
    assert code == 1 and "no RESULT" in msg


def test_a_rover_error_result_fails_the_job() -> None:
    assert mod.decide_exit(_completed_state(), "error")[0] == 1


def test_rule_review_and_no_op_are_valid_outcomes() -> None:
    """A run that proved the rule is wrong — or clean — still proved the lane."""
    assert mod.decide_exit(_completed_state(), "rule-review")[0] == 0
    assert mod.decide_exit(_completed_state(), "no-op")[0] == 0


def test_an_incomplete_stream_fails() -> None:
    st = mod.StreamState()
    st.got_event = True
    assert mod.decide_exit(st, "pushed")[0] == 1


def test_an_empty_stream_points_at_the_network() -> None:
    code, msg = mod.decide_exit(mod.StreamState(), "")
    assert code == 1 and "VPN" in msg


# ── input validation ──────────────────────────────────────────────────────


def test_main_rejects_a_series_or_garbage_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TARGET_REPO", "atlanhq/x")
    for bad in ("L", "l0", "L004,E002", "everything"):
        monkeypatch.setenv("RULE_ID", bad)
        assert mod.main() == 1


def test_main_lowercase_rule_is_normalised(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("TARGET_REPO", "atlanhq/atlan-netsuite-app")
    monkeypatch.setenv("RULE_ID", "l011")
    monkeypatch.setenv("DRY_RUN", "1")
    assert mod.main() == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["metadata"]["rule"] == "L011"


def test_dry_run_never_embeds_the_playbook_in_the_log(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("TARGET_REPO", "atlanhq/atlan-netsuite-app")
    monkeypatch.setenv("RULE_ID", "L011")
    monkeypatch.setenv("DRY_RUN", "1")
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "Orchestration Playbook" not in out  # sizes only, not contents


# ── step summary ──────────────────────────────────────────────────────────


def test_step_summary_carries_the_proof_fields() -> None:
    st = _completed_state()
    st.cost = "0.42"
    text = mod.render_step_summary(
        st,
        {
            "rule": "L011",
            "findings_before": "2",
            "findings_after": "0",
            "cleared": "2",
            "pr_url": "https://github.com/atlanhq/atlan-netsuite-app/pull/99",
            "main_model": "kimi-k3",
            "subagent_model": "gpt-5.6-luna",
        },
        "pushed",
        "abc123",
    )
    for needle in ("pushed", "kimi-k3", "gpt-5.6-luna", "pull/99", "0.42"):
        assert needle in text
