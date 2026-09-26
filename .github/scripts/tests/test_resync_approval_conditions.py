"""Tests for .github/scripts/resync_approval_conditions.py — the code-owner
approval path for connector-pulse's conformance-resync lane PRs (FND-2848).

The bar, as for the Renovate gate: no non-affirmative signal reaches the
approval. Identity (author + branch) only routes a PR here; the tests below pin
that every other condition — and above all the byte-identical re-render — must
hold before ``gh pr review --approve`` is ever invoked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import renovate_approval_conditions as gate  # noqa: E402
import resync_approval_conditions as resync  # noqa: E402

REPO = "atlanhq/atlan-example-app"
HEAD = "h" * 40
PARENT = "p" * 40
MARKER = "<!-- conformance-resync-lane suite=0.39.0 -->\nbody"


def meta(**over):
    base = {
        "user": {"login": resync.RESYNC_AUTHOR},
        "state": "open",
        "draft": False,
        "body": MARKER,
        "head": {"sha": HEAD, "ref": resync.RESYNC_BRANCH, "repo": {"full_name": REPO}},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
    }
    for key, value in over.items():
        base[key] = value
    return base


def commit(sha=HEAD, author=resync.RESYNC_AUTHOR, parents=(PARENT,)):
    return {
        "sha": sha,
        "author": {"login": author},
        "parents": [{"sha": p} for p in parents],
    }


class FakeRunner:
    """Answers gh calls from a table; records every call. Any approval is
    captured in ``approved`` so tests can assert it never happened."""

    def __init__(self, *, commits=None, compare="ahead", checks_rc=0, reviews=None):
        self.commits = [commit()] if commits is None else commits
        self.compare = compare
        self.checks_rc = checks_rc
        self.reviews = reviews or []
        self.calls: list[list[str]] = []
        self.approved = False

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if cmd[:3] == ["gh", "pr", "review"]:
            self.approved = True
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(cmd, self.checks_rc, "", "")
        if "/commits" in joined and "pulls" in joined:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([self.commits]), "")
        if "/compare/" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"status": self.compare}), ""
            )
        if "/reviews" in joined:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([self.reviews]), "")
        raise AssertionError(f"unexpected call: {cmd}")


def renderer(matches=True, lost=None):
    calls = []

    def _render(repo, parent_sha, head_sha, suite, runner):
        calls.append((repo, parent_sha, head_sha, suite))
        return resync.RenderResult(
            matches, suite, lost or {}, "" if matches else "differs"
        )

    _render.calls = calls
    return _render


def run(m=None, runner=None, render=None):
    runner = runner or FakeRunner()
    render = render or renderer()
    approved = resync.process_resync_pr(REPO, "7", HEAD, m or meta(), runner, render)
    return approved, runner, render


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_approves_only_when_every_condition_holds():
    approved, runner, render = run()
    assert approved and runner.approved
    assert render.calls == [(REPO, PARENT, HEAD, "0.39.0")]
    body = runner.calls[-1][runner.calls[-1].index("--body") + 1]
    assert body.startswith(resync.RESYNC_SIGNATURE)


# ---------------------------------------------------------------------------
# Fail closed: every negative signal withholds the approval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "m",
    [
        meta(user={"login": "atlan-app-fleet[bot]"}),
        meta(user={"login": "some-engineer"}),
        meta(
            head={
                "sha": HEAD,
                "ref": "bot/conformance-resync-x",
                "repo": {"full_name": REPO},
            }
        ),
        meta(
            head={
                "sha": HEAD,
                "ref": resync.RESYNC_BRANCH,
                "repo": {"full_name": "fork/x"},
            }
        ),
        meta(state="closed"),
        meta(draft=True),
        meta(
            head={
                "sha": "other",
                "ref": resync.RESYNC_BRANCH,
                "repo": {"full_name": REPO},
            }
        ),
        meta(body="no marker here"),
    ],
)
def test_metadata_failures_never_approve_or_render(m):
    approved, runner, render = run(m=m)
    assert not approved and not runner.approved
    assert render.calls == []


@pytest.mark.parametrize(
    "commits",
    [
        [],
        [commit(), commit(sha="x" * 40)],  # someone pushed on top
        [commit(author="some-engineer")],
        [commit(sha="x" * 40)],
        [commit(parents=())],
        [commit(parents=(PARENT, "q" * 40))],  # merge commit
    ],
)
def test_commit_shape_failures_never_approve(commits):
    approved, runner, render = run(runner=FakeRunner(commits=commits))
    assert not approved and not runner.approved
    assert render.calls == []


@pytest.mark.parametrize("status", ["behind", "diverged", None])
def test_parent_off_main_history_never_approves(status):
    approved, runner, render = run(runner=FakeRunner(compare=status))
    assert not approved and not runner.approved
    assert render.calls == []


def test_render_mismatch_never_approves():
    approved, runner, _ = run(render=renderer(matches=False))
    assert not approved and not runner.approved


def test_lost_settings_never_approve_even_on_tree_match():
    approved, runner, _ = run(
        render=renderer(matches=True, lost={"tests.yaml": ["unit: 90"]})
    )
    assert not approved and not runner.approved


def test_red_required_checks_never_approve():
    approved, runner, _ = run(runner=FakeRunner(checks_rc=1))
    assert not approved and not runner.approved


def test_idempotent_for_same_head():
    prior = {
        "user": {"login": "atlan-ci"},
        "state": "APPROVED",
        "commit_id": HEAD,
        "body": resync.RESYNC_APPROVAL_BODY,
    }
    approved, runner, _ = run(runner=FakeRunner(reviews=[prior]))
    assert not approved and not runner.approved


def test_prior_approval_of_an_older_head_does_not_count():
    prior = {
        "user": {"login": "atlan-ci"},
        "state": "APPROVED",
        "commit_id": "old",
        "body": resync.RESYNC_APPROVAL_BODY,
    }
    approved, _, _ = run(runner=FakeRunner(reviews=[prior]))
    assert approved


# ---------------------------------------------------------------------------
# Routing from the Renovate gate
# ---------------------------------------------------------------------------


def test_gate_routes_resync_prs_and_leaves_others(monkeypatch):
    seen = []
    monkeypatch.setattr(
        gate.resync, "process_resync_pr", lambda *a, **k: seen.append(a[1]) or False
    )
    monkeypatch.setattr(gate, "fetch_pr_meta", lambda repo, pr, runner: meta())
    assert gate.process_pr(REPO, "7", HEAD, "", FakeRunner()) is False
    assert seen == ["7"]

    # A Renovate PR is never routed to the resync path.
    renovate_meta = meta(user={"login": "atlan-app-fleet[bot]"})
    renovate_meta["head"]["ref"] = "renovate/lock-file-maintenance"
    monkeypatch.setattr(gate, "fetch_pr_meta", lambda repo, pr, runner: renovate_meta)
    monkeypatch.setattr(gate, "fetch_changed_files", lambda repo, pr, runner: [])
    gate.process_pr(REPO, "8", HEAD, "", FakeRunner())
    assert seen == ["7"]


def test_branch_name_alone_is_not_a_candidate():
    m = meta(user={"login": "some-engineer"})
    assert not resync.is_candidate(m)
    assert resync.is_candidate(meta())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_pinned_conformance_reads_uv_lock():
    lock = (
        '[[package]]\nname = "atlan-application-sdk-conformance"\nversion = "0.39.0"\n'
    )
    assert resync.pinned_conformance(lock) == "0.39.0"
    assert (
        resync.pinned_conformance('[[package]]\nname = "x"\nversion = "1.0.0"\n')
        is None
    )
    assert resync.pinned_conformance("not toml [[[") is None


def test_parse_manifest_takes_last_manifest_line():
    out = 'installed: x\n{"touched": ["a"]}\n{"skipped": false, "touched": ["b"]}\n'
    assert resync.parse_manifest(out)["touched"] == ["b"]
    assert resync.parse_manifest("nothing") is None


def test_lost_setting_lines_is_reorder_immune():
    assert (
        resync.lost_setting_lines(
            '{\n "a": 1,\n "b": 2\n}\n', '{\n "b": 2,\n "a": 1\n}\n'
        )
        == []
    )
    assert resync.lost_setting_lines("unit: 90\ne2e: true\n", "e2e: true\n") == [
        "unit: 90"
    ]


def test_safe_touched_drops_escapes_backups_and_symlinked_dirs(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real")
    manifest = {
        "touched": [
            "ok.yaml",
            "x.bak",
            "../escape",
            "/abs",
            "linked/f.json",
            "real/f.json",
            3,
        ]
    }
    assert resync.safe_touched(manifest, tmp_path) == ["ok.yaml", "real/f.json"]


def test_stage_like_the_lane_removes_backups_and_stages_manifest_only(tmp_path):
    work = str(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    (tmp_path / "tests.yaml").write_text("e2e: true\n")
    (tmp_path / "tests.yaml.bak").write_text("e2e: true\nunit: 90\n")
    (tmp_path / "stray.txt").write_text("not in manifest\n")
    lost = resync.stage_like_the_lane(work, {"touched": ["tests.yaml"]}, subprocess.run)
    assert lost == {"tests.yaml": ["unit: 90"]}
    assert not (tmp_path / "tests.yaml.bak").exists()
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=work,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert staged == ["tests.yaml"]


def test_other_connectivity_ai_prs_are_never_approved(monkeypatch):
    """The connectivity-ai App also opens the AI remediation lane's PRs
    (``conformance/<rule>`` branches). They must never reach the resync path,
    and the Renovate path refuses the author outright — so no approval."""
    routed = []
    monkeypatch.setattr(
        gate.resync, "process_resync_pr", lambda *a, **k: routed.append(a[1]) or True
    )
    for ref in (
        "conformance/D011",
        "conformance/L004",
        "bot/conformance-resync-x",
        "main",
    ):
        m = meta()
        m["head"] = {"sha": HEAD, "ref": ref, "repo": {"full_name": REPO}}
        monkeypatch.setattr(gate, "fetch_pr_meta", lambda repo, pr, runner, m=m: m)
        runner = FakeRunner()
        assert gate.process_pr(REPO, "9", HEAD, "", runner) is False
        assert not runner.approved
    assert routed == []


def test_accepted_drops_do_not_block_but_other_losses_do(tmp_path):
    work = str(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yaml").write_text("e2e: true\n")
    (wf / "tests.yaml.bak").write_text(
        'e2e: true\ne2e-clouds: "aws,azure,gcp"\ncontainer-health-timeout-seconds: 240\n'
    )
    (tmp_path / "renovate.json").write_text("{}\n")
    (tmp_path / "renovate.json.bak").write_text('{\n  "automerge": false\n}\n')
    manifest = {"touched": [".github/workflows/tests.yaml", "renovate.json"]}
    lost = resync.stage_like_the_lane(work, manifest, subprocess.run)
    assert ".github/workflows/tests.yaml" not in lost
    assert lost == {"renovate.json": ['"automerge": false']}


def test_accepted_drops_mirror_the_lane():
    # connector-pulse conformance_resync_service.ACCEPTED_DROPS must match.
    assert resync.ACCEPTED_DROPS == {
        ".github/workflows/tests.yaml": frozenset(
            {"container-health-timeout-seconds", "e2e-clouds"}
        )
    }
