"""Tests for .github/scripts/resync_approval_conditions.py — the code-owner
approval path for the conformance-resync lane's PRs (FND-2848, FND-2868).

The bar, as for the Renovate gate: no non-affirmative signal reaches the
approval. Identity (author + branch) only routes a PR here; the tests below pin
that every other condition — and above all the byte-identical re-render — must
hold before ``gh pr review --approve`` is ever invoked.
"""

from __future__ import annotations

import base64
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
RESOLVED_AT = "2026-09-28T12:00:00Z"
AUTO_RENOVATE_JSON = json.dumps(
    {"extends": ["github>atlanhq/application-sdk//renovate-config/default.json"]}
)
SOFT_RENOVATE_JSON = json.dumps(
    {
        "extends": ["github>atlanhq/application-sdk//renovate-config/default.json"],
        "lockFileMaintenance": {"automerge": False, "platformAutomerge": False},
        "packageRules": [
            {"matchPackageNames": ["*"], "automerge": False, "platformAutomerge": False}
        ],
    }
)
MARKER = (
    f"<!-- conformance-resync-lane suite=0.39.0 resolved-at={RESOLVED_AT} -->\nbody"
)


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


COMMITTED_AT = "2026-09-28T12:05:00Z"


def commit(
    sha=HEAD, author=resync.RESYNC_AUTHOR, parents=(PARENT,), committed=COMMITTED_AT
):
    return {
        "sha": sha,
        "author": {"login": author},
        "parents": [{"sha": p} for p in parents],
        "commit": {"committer": {"date": committed}},
    }


class FakeRunner:
    """Answers gh calls from a table; records every call. Any approval is
    captured in ``approved`` so tests can assert it never happened."""

    def __init__(
        self,
        *,
        commits=None,
        compare="ahead",
        checks_rc=0,
        reviews=None,
        live_head=HEAD,
        renovate_json=None,
        renovate_json_missing=False,
        parent_pin="0.39.0",
        live=None,
        parent_read_error=None,
    ):
        self.parent_pin = parent_pin
        self.live = live
        self.parent_read_error = parent_read_error
        self.commits = [commit()] if commits is None else commits
        self.live_head = live_head
        self.renovate_json = (
            None
            if renovate_json_missing
            else (AUTO_RENOVATE_JSON if renovate_json is None else renovate_json)
        )
        self.compare = compare
        self.checks_rc = checks_rc
        self.reviews = reviews or []
        self.calls: list[list[str]] = []
        self.approved = False

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        if "/reviews" in joined and "POST" in cmd:
            self.approved = True
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["gh", "api", f"repos/{REPO}/pulls/7"]:
            live = self.live or meta(
                head={
                    "sha": self.live_head,
                    "ref": resync.RESYNC_BRANCH,
                    "repo": {"full_name": REPO},
                }
            )
            return subprocess.CompletedProcess(cmd, 0, json.dumps(live), "")
        if "/contents/" in joined and self.parent_read_error:
            return subprocess.CompletedProcess(cmd, 1, "", self.parent_read_error)
        if cmd[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(cmd, self.checks_rc, "", "")
        if "/commits" in joined and "pulls" in joined:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([self.commits]), "")
        if "/contents/uv.lock" in joined:
            if f"ref={PARENT}" not in joined:
                raise AssertionError(f"uv.lock must be read at the parent: {cmd}")
            if self.parent_pin is None:
                return subprocess.CompletedProcess(cmd, 1, "", "Not Found")
            lock = (
                '[[package]]\nname = "atlan-application-sdk-conformance"\n'
                f'version = "{self.parent_pin}"\n'
            )
            return subprocess.CompletedProcess(cmd, 0, lock, "")
        if "/contents/renovate.json" in joined:
            if f"ref={PARENT}" not in joined:
                raise AssertionError(f"renovate.json must be read at the parent: {cmd}")
            if self.renovate_json is None:
                return subprocess.CompletedProcess(cmd, 1, "", "Not Found")
            encoded = base64.b64encode(self.renovate_json.encode()).decode()
            return subprocess.CompletedProcess(cmd, 0, encoded + "\n", "")
        if "/compare/" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"status": self.compare}), ""
            )
        if "/reviews" in joined:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([self.reviews]), "")
        raise AssertionError(f"unexpected call: {cmd}")


def renderer(matches=True, lost=None):
    calls = []

    def _render(repo, parent_sha, head_sha, suite, resolved_at, runner):
        calls.append((repo, parent_sha, head_sha, suite, resolved_at))
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
    assert render.calls == [(REPO, PARENT, HEAD, "0.39.0", RESOLVED_AT)]
    post = runner.calls[-1]
    assert f"commit_id={HEAD}" in post and "event=APPROVE" in post
    body = next(a for a in post if a.startswith("body="))[len("body=") :]
    assert body.startswith(resync.RESYNC_SIGNATURE)


def test_head_moved_during_verification_never_approves():
    approved, runner, _ = run(runner=FakeRunner(live_head="n" * 40))
    assert not approved and not runner.approved


def test_base_other_than_main_never_approves():
    approved, runner, render = run(
        meta(base={"ref": "attacker-branch", "repo": {"full_name": REPO}})
    )
    assert not approved and not runner.approved and render.calls == []


def test_marker_without_resolved_at_never_approves():
    approved, runner, render = run(
        meta(body="<!-- conformance-resync-lane suite=0.39.0 -->\nbody")
    )
    assert not approved and not runner.approved and render.calls == []


def test_resync_command_fences_third_party_and_exempts_first_party():
    cmd = resync.resync_command("0.39.0", RESOLVED_AT)
    assert cmd[cmd.index("--exclude-newer") + 1] == "2026-09-21T12:00:00Z"
    for pkg in resync.FIRST_PARTY:
        assert f"{pkg}={RESOLVED_AT}" in cmd
    assert cmd[-4:] == [resync.CONFORMANCE_PACKAGE, "bootstrap", "--resync", "--json"]


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


def test_lost_setting_lines_counts_duplicates():
    backup = "a:\n  secrets: inherit\nb:\n  secrets: inherit\n"
    new = "a:\n  secrets: inherit\nb:\n"
    assert resync.lost_setting_lines(backup, new) == ["secrets: inherit"]


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


TESTS_YAML = ".github/workflows/tests.yaml"


def test_yaml_quoting_alone_is_not_a_lost_setting():
    backup = (
        "    with:\n"
        "      services-script: .github/test/setup-services.sh\n"
        "      e2e-test-path: 'tests/e2e/sdr'\n"
    )
    new = (
        "    with:\n"
        '      e2e-test-path: "tests/e2e/sdr"\n'
        '      services-script: ".github/test/setup-services.sh"\n'
    )
    assert resync.lost_setting_lines(backup, new, TESTS_YAML) == []


@pytest.mark.parametrize(
    "old, new",
    [
        ('enable-e2e: "true"', "enable-e2e: true"),
        ("timeout-minutes: 30", 'timeout-minutes: "30"'),
        ('runtime-sdk-ref: ""', "runtime-sdk-ref:"),
        ('runtime-sdk-ref: "~"', "runtime-sdk-ref: ~"),
        ('args: "a\\tb"', "args: a\\tb"),
        ('args: "-k slow"', "args: -k slow"),
        ('args: "a: b"', "args: a: b"),
    ],
)
def test_yaml_quotes_that_change_the_value_still_count_as_lost(old, new):
    assert resync.lost_setting_lines(old + "\n", new + "\n", TESTS_YAML) == [old]


def test_json_quoting_is_never_ignored():
    backup = '{\n  "automerge": "true",\n  "groupName": "x"\n}\n'
    new = '{\n  "automerge": true,\n  "groupName": "x"\n}\n'
    assert resync.lost_setting_lines(backup, new, "renovate.json") == [
        '"automerge": "true",'
    ]


def test_template_description_text_is_not_a_lost_setting():
    backup = (
        "on:\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      application_sdk_ref:\n"
        "        description: |\n"
        "          Branch/SHA of atlanhq/application-sdk.\n"
        "\n"
        "          Pins the SDK in the tests + e2e jobs.\n"
        "        required: false\n"
        "      run_e2e:\n"
        "        description: \"Set to 'true' to trigger the e2e job.\"\n"
        "        type: string\n"
    )
    new = (
        "on:\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      application_sdk_ref:\n"
        "        description: >-\n"
        "          Pins SDK in tests + e2e jobs.\n"
        "        required: false\n"
        "      run_e2e:\n"
        "        description: \"Set to 'true' to run e2e. Defaults to off.\"\n"
        "        type: string\n"
    )
    assert resync.lost_setting_lines(backup, new, TESTS_YAML) == []
    json_backup = '{\n  "packageRules": [\n    {\n      "description": "old text",\n      "automerge": true\n    }\n  ]\n}\n'
    json_new = '{\n  "packageRules": [\n    {\n      "description": "new text",\n      "automerge": true\n    }\n  ]\n}\n'
    assert resync.lost_setting_lines(json_backup, json_new, "renovate.json") == []


def test_settings_next_to_a_description_are_still_checked():
    backup = (
        "      run_e2e:\n"
        "        description: |\n"
        "          Some text.\n"
        "        required: false\n"
        "        default: 'off'\n"
    )
    new = "      run_e2e:\n        description: |\n          Other text.\n"
    assert resync.lost_setting_lines(backup, new, TESTS_YAML) == [
        "required: false",
        "default: 'off'",
    ]
    after_block = (
        "    description: |\n"
        "      Some text.\n"
        "    with:\n"
        "      services-script: .github/test/setup-services.sh\n"
    )
    assert resync.lost_setting_lines(
        after_block, "    description: |\n      Other.\n    with:\n", TESTS_YAML
    ) == ["services-script: .github/test/setup-services.sh"]
    json_backup = '{\n  "description": "a",\n  "automerge": true\n}\n'
    json_new = '{\n  "description": "b"\n}\n'
    assert resync.lost_setting_lines(json_backup, json_new, "renovate.json") == [
        '"automerge": true'
    ]
    list_item = "rules:\n  - description: |\n      Some text.\n    automerge: true\n"
    assert resync.lost_setting_lines(
        list_item, "rules:\n  - description: |\n      Other.\n", TESTS_YAML
    ) == ["automerge: true"]


def test_description_and_quotes_count_in_files_that_are_not_yaml_or_json():
    backup = 'description: old\nname: "x"\n'
    new = "description: new\nname: x\n"
    assert resync.lost_setting_lines(backup, new, ".claude/skills/r/SKILL.md") == [
        "description: old",
        'name: "x"',
    ]
    assert resync.lost_setting_lines(backup, new) == [
        "description: old",
        'name: "x"',
    ]


def test_stage_like_the_lane_does_not_hold_on_quoting_or_description(tmp_path):
    work = str(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yaml.bak").write_text(
        "    with:\n      services-script: .github/test/setup-services.sh\n"
    )
    (wf / "tests.yaml").write_text(
        '    with:\n      services-script: ".github/test/setup-services.sh"\n'
    )
    (tmp_path / "renovate.json.bak").write_text(
        '{\n  "description": "old",\n  "automerge": true\n}\n'
    )
    (tmp_path / "renovate.json").write_text(
        '{\n  "description": "new",\n  "automerge": true\n}\n'
    )
    manifest = {"touched": [TESTS_YAML, "renovate.json"]}
    assert resync.stage_like_the_lane(work, manifest, subprocess.run) == {}


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


def test_other_lane_author_prs_are_never_approved(monkeypatch):
    """A PR by the lane's App on any other branch must never reach the resync
    path, and the Renovate path refuses the author outright — so no approval."""
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
    assert resync.ACCEPTED_DROPS == {
        ".github/workflows/tests.yaml": frozenset(
            {"container-health-timeout-seconds", "e2e-clouds"}
        )
    }


@pytest.mark.parametrize(
    "runner_kwargs",
    [
        {"renovate_json": SOFT_RENOVATE_JSON},
        {"renovate_json": "{not json"},
        {"renovate_json_missing": True},
    ],
    ids=["soft-mode", "invalid-json", "missing"],
)
def test_no_approval_unless_the_repo_auto_merges(runner_kwargs):
    approved, runner, render = run(runner=FakeRunner(**runner_kwargs))
    assert not approved and not runner.approved and render.calls == []


def test_scoped_per_package_opt_out_still_approves():
    scoped = json.dumps(
        {
            "extends": ["github>atlanhq/application-sdk//renovate-config/default.json"],
            "packageRules": [
                {"matchPackageNames": ["atlan-application-sdk"], "automerge": False}
            ],
        }
    )
    approved, runner, _ = run(runner=FakeRunner(renovate_json=scoped))
    assert approved and runner.approved


# ---------------------------------------------------------------------------
# Parent preconditions (shared with the lane's leave-alone check)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pin", ["0.40.0", None])
def test_parent_pinning_another_suite_never_approves_and_skips_the_render(pin):
    approved, runner, render = run(runner=FakeRunner(parent_pin=pin))
    assert not approved and not runner.approved and render.calls == []


def test_parent_preconditions_pass_for_matching_pin_and_auto_mode():
    assert resync.parent_preconditions(REPO, PARENT, "0.39.0", FakeRunner()) == (
        True,
        "",
    )


# ---------------------------------------------------------------------------
# Render sandbox: third-party code never runs on the token-holding host
# ---------------------------------------------------------------------------


def test_render_argv_runs_the_pinned_image_with_no_host_env():
    argv = resync.render_argv("/s/w", "0.39.0", RESOLVED_AT, "n", 1001, 121)
    assert argv[:2] == ["docker", "run"]
    assert "@sha256:" in resync.RENDER_IMAGE and resync.RENDER_IMAGE in argv
    assert argv[argv.index("-v") + 1] == "/s/w:/w"
    assert argv[argv.index("--user") + 1] == "1001:121"
    env = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert env and not any("TOKEN" in e for e in env)
    assert "--env-file" not in argv and "--privileged" not in argv
    assert argv[argv.index(resync.RENDER_IMAGE) + 1 :] == resync.resync_command(
        "0.39.0", RESOLVED_AT
    )


def _tree(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def test_copy_back_brings_touched_files_and_backups_with_exec_bit(tmp_path):
    scratch, work = tmp_path / "s", tmp_path / "w"
    _tree(scratch, {"a.yaml": "new", "a.yaml.bak": "old", "run.sh": "#!"})
    (scratch / "run.sh").chmod(0o755)
    _tree(work, {"a.yaml": "old", "untouched": "keep"})
    _tree(scratch, {"untouched": "changed by the render"})
    refused = resync.copy_back(scratch, work, {"touched": ["a.yaml", "run.sh"]})
    assert refused == []
    assert (work / "a.yaml").read_text() == "new"
    assert (work / "a.yaml.bak").read_text() == "old"
    assert (work / "run.sh").stat().st_mode & 0o111
    assert (work / "untouched").read_text() == "keep"


def test_copy_back_deletes_what_the_render_deleted(tmp_path):
    scratch, work = tmp_path / "s", tmp_path / "w"
    scratch.mkdir()
    _tree(work, {"retired.sh": "x"})
    assert resync.copy_back(scratch, work, {"touched": ["retired.sh"]}) == []
    assert not (work / "retired.sh").exists()


@pytest.mark.parametrize(
    "touched", [".git/hooks/post-checkout", ".git/config", "../escape", "/abs"]
)
def test_copy_back_never_writes_git_internals_or_escapes(tmp_path, touched):
    scratch, work = tmp_path / "s", tmp_path / "w"
    scratch.mkdir()
    work.mkdir()
    (work / ".git").mkdir()
    assert resync.copy_back(scratch, work, {"touched": [touched]}) == [touched]
    assert list((work / ".git").iterdir()) == []


def test_copy_back_refuses_a_symlink_the_render_wrote(tmp_path):
    scratch, work = tmp_path / "s", tmp_path / "w"
    scratch.mkdir()
    work.mkdir()
    (scratch / "a.yaml").symlink_to("/etc/passwd")
    assert resync.copy_back(scratch, work, {"touched": ["a.yaml"]}) == ["a.yaml"]
    assert not (work / "a.yaml").exists()


@pytest.mark.parametrize(
    "link, target, touched",
    [
        (".claude", ".agents", ".claude/settings.json"),
        (".claude/skills", "../.agents/skills", ".claude/skills/remediate/SKILL.md"),
    ],
)
def test_copy_back_skips_paths_under_a_symlinked_dir_of_the_clone(
    tmp_path, link, target, touched
):
    scratch, work = tmp_path / "s", tmp_path / "w"
    for root in (scratch, work):
        _tree(root, {".agents/skills/remediate/SKILL.md": "old", "a.yaml": "old"})
        (root / ".agents" / "settings.json").write_text("old")
        (root / link).parent.mkdir(parents=True, exist_ok=True)
        (root / link).symlink_to(target)
    (scratch / touched).write_text("new")
    (scratch / "a.yaml").write_text("new")
    refused = resync.copy_back(scratch, work, {"touched": [touched, "a.yaml"]})
    assert refused == []
    assert (work / touched).read_text() == "old"
    assert (work / "a.yaml").read_text() == "new"


def test_symlink_skipped_lists_only_paths_under_a_symlinked_dir(tmp_path):
    _tree(tmp_path, {".agents/skills/r/SKILL.md": "x", "a.yaml": "x"})
    (tmp_path / ".claude").symlink_to(".agents")
    touched = [
        ".claude/skills/r/SKILL.md",
        ".claude/skills/r/SKILL.md",
        "a.yaml",
        ".claude",
        ".agents/../.claude/skills/r/SKILL.md",
        "/abs/.claude/x",
        7,
    ]
    assert resync.symlink_skipped({"touched": touched}, tmp_path) == [
        ".claude/skills/r/SKILL.md"
    ]
    assert resync.symlink_skipped({}, tmp_path) == []


def test_copy_back_refuses_a_symlinked_dir_only_the_render_has(tmp_path):
    scratch, work = tmp_path / "s", tmp_path / "w"
    _tree(scratch, {"real/f.yaml": "new"})
    (scratch / "linked").symlink_to(scratch / "real")
    work.mkdir()
    assert resync.copy_back(scratch, work, {"touched": ["linked/f.yaml"]}) == [
        "linked/f.yaml"
    ]
    assert not (work / "linked").exists()


def test_sandboxed_render_succeeds_when_the_render_writes_through_a_clone_symlink(
    tmp_path,
):
    work = tmp_path / "w"
    _tree(work, {".agents/settings.json": "old", "a.yaml": "old"})
    (work / ".claude").symlink_to(".agents")

    def runner(cmd, **kwargs):
        if cmd[:2] == ["docker", "run"]:
            scratch = Path(cmd[cmd.index("-v") + 1].split(":")[0])
            (scratch / ".claude" / "settings.json").write_text("new")
            (scratch / "a.yaml").write_text("new")
            touched = '{"touched": [".claude/settings.json", "a.yaml"]}\n'
            return subprocess.CompletedProcess(cmd, 0, touched, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    rc, _, err = resync.sandboxed_render(str(work), "0.39.0", RESOLVED_AT, runner)
    assert (rc, err) == (0, "")
    assert (work / ".agents" / "settings.json").read_text() == "old"
    assert (work / "a.yaml").read_text() == "new"


def test_sandboxed_render_never_copies_git_into_the_sandbox(tmp_path):
    work = tmp_path / "w"
    _tree(work, {"a.yaml": "old", ".git/config": "[core]"})
    seen = {}

    def runner(cmd, **kwargs):
        if cmd[:2] == ["docker", "run"]:
            scratch = Path(cmd[cmd.index("-v") + 1].split(":")[0])
            seen["git"] = (scratch / ".git").exists()
            seen["env"] = kwargs.get("env")
            (scratch / "a.yaml").write_text("new")
            return subprocess.CompletedProcess(cmd, 0, '{"touched": ["a.yaml"]}\n', "")
        raise AssertionError(cmd)

    rc, _, _ = resync.sandboxed_render(str(work), "0.39.0", RESOLVED_AT, runner)
    assert rc == 0 and seen["git"] is False
    assert (work / "a.yaml").read_text() == "new"


def test_sandboxed_render_timeout_kills_the_container(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    rc, _, err = resync.sandboxed_render(str(work), "0.39.0", RESOLVED_AT, runner)
    name = calls[0][calls[0].index("--name") + 1]
    assert rc == 124 and "timed out" in err
    assert calls[-1] == ["docker", "rm", "-f", name]


# ---------------------------------------------------------------------------
# resolved-at comes from the editable PR body: bound it, never crash on it
# ---------------------------------------------------------------------------


def _marker(resolved_at):
    return f"<!-- conformance-resync-lane suite=0.39.0 resolved-at={resolved_at} -->"


@pytest.mark.parametrize(
    "resolved_at",
    [
        "2099-01-01T00:00:00Z",  # in the future: would lift the fence
        "2026-09-28T12:06:00Z",  # after the lane's own commit
        "2026-13-45T00:00:00Z",  # matches the marker regex, not a real date
    ],
)
def test_unsafe_resolved_at_never_approves_and_never_renders(resolved_at):
    approved, runner, render = run(meta(body=_marker(resolved_at)))
    assert not approved and not runner.approved and render.calls == []


def test_commit_without_committer_date_never_approves():
    no_date = commit()
    del no_date["commit"]
    approved, runner, render = run(runner=FakeRunner(commits=[no_date]))
    assert not approved and not runner.approved and render.calls == []


def test_check_resolved_at_bounds():
    cap = resync.parse_resolved_at("2026-09-28T12:05:00Z")
    assert resync.check_resolved_at(RESOLVED_AT, cap) == (True, "")
    assert not resync.check_resolved_at("2026-09-28T12:05:01Z", cap)[0]
    assert not resync.check_resolved_at(None, cap)[0]
    assert resync.parse_resolved_at("2026-02-30T00:00:00Z") is None


# ---------------------------------------------------------------------------
# Cheap checks run before the render
# ---------------------------------------------------------------------------


def test_red_checks_skip_the_render():
    approved, runner, render = run(runner=FakeRunner(checks_rc=1))
    assert not approved and not runner.approved and render.calls == []


def test_already_approved_head_skips_the_render():
    review = {
        "user": {"login": resync.APPROVER_LOGIN},
        "state": "APPROVED",
        "commit_id": HEAD,
        "body": resync.RESYNC_APPROVAL_BODY,
    }
    approved, runner, render = run(runner=FakeRunner(reviews=[review]))
    assert not approved and not runner.approved and render.calls == []


# ---------------------------------------------------------------------------
# Round-2 review: live revalidation, API errors, accepted drops, timeouts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "live",
    [
        meta(base={"ref": "attacker-branch", "repo": {"full_name": REPO}}),
        meta(draft=True),
        meta(state="closed"),
    ],
)
def test_pr_changed_during_verification_never_approves(live):
    approved, runner, render = run(runner=FakeRunner(live=live))
    assert render.calls  # it got as far as the render
    assert not approved and not runner.approved


def test_rate_limited_parent_read_raises_instead_of_reading_as_missing():
    runner = FakeRunner(parent_read_error="gh: API rate limit exceeded (HTTP 403)")
    with pytest.raises(resync.GhError):
        run(runner=runner)
    assert not runner.approved


def test_missing_parent_files_still_read_as_missing():
    runner = FakeRunner(parent_read_error="gh: Not Found (HTTP 404)")
    assert resync.parent_automerge_mode(REPO, PARENT, runner) == "missing"
    assert resync.parent_pinned_conformance(REPO, PARENT, runner) is None


def test_an_accepted_key_excuses_one_lost_line_not_every_line_using_it():
    path = ".github/workflows/tests.yaml"
    lost = ["e2e-clouds: aws", "e2e-clouds: gcp", "kept-key: x"]
    assert resync.still_lost(path, lost) == ["e2e-clouds: gcp", "kept-key: x"]
    assert resync.still_lost(path, ["e2e-clouds: aws"]) == []


def test_call_turns_a_timeout_into_a_gh_error():
    def runner(cmd, **kwargs):
        assert kwargs["timeout"] == resync.GH_TIMEOUT
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    with pytest.raises(resync.GhError, match="timed out"):
        resync.call(runner, ["gh", "api", "x"])
