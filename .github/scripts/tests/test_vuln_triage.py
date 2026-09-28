"""Tests for .github/scripts/vuln_triage (the deterministic vuln triage)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import vuln_triage.__main__ as cli  # noqa: E402
from vuln_triage import allowlist, bump, classify, report, scan  # noqa: E402
from vuln_triage.effects import newest_successful_run  # noqa: E402
from vuln_triage.run import Context, Deps, _listing, _uv_cause, run  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

LOCK = """\
version = 1
revision = 3

[[package]]
name = "requests"
version = "2.32.3"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/r.tar.gz", upload-time = "2024-05-29T00:00:00Z" }

[[package]]
name = "Some_Lib"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.pythonhosted.org/s.whl", upload-time = "2020-01-01T00:00:00Z" },
]

[[package]]
name = "temporalio"
version = "1.9.0"
source = { registry = "https://pypi.org/simple" }
"""

PYPROJECT = """\
[project]
name = "x"

[tool.uv]
default-groups = ["dev"]
constraint-dependencies = [
    "protobuf>=6.33.5",          # CVE-2026-0994
    "requests>=2.0",  # CVE-OLD
]

[tool.uv.sources]
"""


def vuln(cve, pkg, installed, fixed="", sev="HIGH", path=""):
    v = {
        "VulnerabilityID": cve,
        "PkgName": pkg,
        "InstalledVersion": installed,
        "FixedVersion": fixed,
        "Severity": sev,
    }
    if path:
        v["PkgPath"] = path
    return v


def write_scan(d: Path, fs=(), image=()):
    d.mkdir(parents=True, exist_ok=True)
    (d / scan.FS_FILE).write_text(
        json.dumps({"Results": [{"Target": "uv.lock", "Vulnerabilities": list(fs)}]})
    )
    (d / scan.IMAGE_FILE).write_text(
        json.dumps({"Results": [{"Target": "image", "Vulnerabilities": list(image)}]})
    )


@pytest.fixture
def lock(tmp_path):
    p = tmp_path / "uv.lock"
    p.write_text(LOCK)
    return scan.load_lock(p)


alive = lambda _pkg: True  # noqa: E731
dead = lambda _pkg: False  # noqa: E731
unknown = lambda _pkg: None  # noqa: E731


# --------------------------------------------------------------------------- scan


def test_normalize_matches_pep503():
    assert scan.normalize("Some_Lib") == scan.normalize("some.lib") == "some-lib"


def test_vendored_in_reads_the_wheel_dir_not_dist_info():
    h = scan.Hit(
        "pyo3",
        "0.20",
        "0.24",
        "fs",
        ".venv/lib/python3.12/site-packages/temporalio/bridge/Cargo.lock",
    )
    assert h.vendored_in == "temporalio"
    h2 = scan.Hit(
        "x", "1", "", "fs", ".venv/lib/python3.12/site-packages/x-1.dist-info/METADATA"
    )
    assert h2.vendored_in == ""


def test_load_findings_merges_both_scans(tmp_path):
    write_scan(
        tmp_path,
        fs=[vuln("CVE-1", "requests", "2.32.3", "2.32.4")],
        image=[
            vuln("CVE-1", "requests", "2.31.0", "2.32.4"),
            vuln("CVE-2", "openssl", "3.0"),
        ],
    )
    f = scan.load_findings(tmp_path)
    assert set(f) == {"CVE-1", "CVE-2"}
    assert {h.source for h in f["CVE-1"].hits} == {"fs", "image"}


def test_load_lock_upload_time_falls_back_to_wheel(lock):
    assert lock["requests"]["upload_time"].startswith("2024-05-29")
    assert lock["some-lib"]["upload_time"].startswith("2020-01-01")


def test_lock_registry_hosts():
    assert scan.lock_registry_hosts(LOCK) == {"pypi.org", "files.pythonhosted.org"}


# --------------------------------------------------------------------------- classify


@pytest.mark.parametrize(
    "installed,fixed,want",
    [
        ("2.32.3", "2.32.4, 3.0.1", "2.32.4"),
        ("2.32.3", "3.0.1, 2.32.4", "2.32.4"),
        ("2.32.3", "3.0.1", "3.0.1"),
        ("1.0", "", ""),
    ],
)
def test_pick_fixed(installed, fixed, want):
    assert classify.pick_fixed(installed, fixed) == want


def _one(finding_hits, lock, upstream=alive, sev="HIGH"):
    f = scan.Finding(cve="CVE-X", severity=sev, hits=finding_hits)
    return classify.classify(f, lock, upstream)


def test_case1_our_dep_with_fix(lock):
    t = _one([scan.Hit("requests", "2.32.3", "2.32.4", "fs", "uv.lock")], lock)
    assert (t.case, t.bump_to, t.source) == (1, "2.32.4", "our dependency")


def test_case2_our_dep_no_fix_upstream_alive(lock):
    t = _one([scan.Hit("requests", "2.32.3", "", "fs", "uv.lock")], lock)
    assert t.case == 2


def test_case2_when_liveness_unknown_says_so(lock):
    t = _one([scan.Hit("requests", "2.32.3", "", "fs", "uv.lock")], lock, unknown)
    assert t.case == 2 and "could not be checked" in t.note


def test_case3_our_dep_no_fix_upstream_dead(lock):
    t = _one([scan.Hit("Some_Lib", "1.0.0", "", "fs", "uv.lock")], lock, dead)
    assert t.case == 3


def test_vendored_native_dep_is_case2_not_a_bump(lock):
    path = ".venv/lib/python3.12/site-packages/temporalio/bridge/Cargo.lock"
    t = _one([scan.Hit("pyo3", "0.20.0", "0.24.1", "fs", path)], lock)
    assert t.case == 2 and "temporalio" in t.reason and not t.bump_to


def test_case4_base_image_even_with_a_fix(lock):
    t = _one([scan.Hit("dapr", "1.14.0", "1.14.5", "image", "usr/bin/daprd")], lock)
    assert t.case == 4 and t.source == "base image"


def test_base_image_copy_of_a_locked_pkg_at_another_version_is_case4(lock):
    t = _one([scan.Hit("requests", "2.31.0", "2.32.4", "image", "site")], lock)
    assert t.case == 4 and "already has 2.32.3" in t.note


def test_other_manifest_in_our_tree_is_case2_not_base_image(lock):
    # The fs scan also reads packages/conformance/uv.lock. Even a package the root lock
    # carries at the same version is not the root bump's to fix, and it is never a
    # base-image rebuild.
    t = _one(
        [
            scan.Hit(
                "requests", "2.32.3", "2.32.4", "fs", "packages/conformance/uv.lock"
            )
        ],
        lock,
    )
    assert t.case == 2 and not t.bump_to
    assert t.source == "manifest packages/conformance/uv.lock"
    o = report.Outcome()
    assert any("packages/conformance/uv.lock" in h for h in report.needs_human([t], o))


def test_most_actionable_hit_wins_and_others_are_noted(lock):
    t = _one(
        [
            scan.Hit("requests", "2.31.0", "2.32.4", "image", "site"),
            scan.Hit("requests", "2.32.3", "2.32.4", "fs", "uv.lock"),
        ],
        lock,
    )
    assert t.case == 1 and "base image" in t.note


def test_triage_ticket_kills_cves_the_scan_no_longer_reports(lock):
    out = classify.triage_ticket(["CVE-GONE"], {}, lock, alive, "HIGH")
    assert out[0].case == classify.KILLED and not out[0].allowlistable


# --------------------------------------------------------------------------- allowlist


def _t(cve, sev, case=4, pkg="p"):
    return classify.Triage(
        cve=cve, severity=sev, case=case, package=pkg, reason="Case 4: x."
    )


def test_plan_entries_sla_from_detection_and_skips():
    existing = {"_expiry_policy": {"CRITICAL": 7, "HIGH": 30}, "CVE-OLD": {}}
    plan = allowlist.plan_entries(
        [
            _t("CVE-C", "CRITICAL"),
            _t("CVE-H", "HIGH"),
            _t("CVE-M", "MEDIUM"),
            _t("CVE-OLD", "HIGH"),
        ],
        existing,
        detected=date(2026, 9, 25),
        today=date(2026, 9, 28),
        ticket="FND-1",
        added_by="bot",
    )
    assert plan.entries["CVE-C"]["expires"] == "2026-10-02"
    assert plan.entries["CVE-H"]["expires"] == "2026-10-25"
    assert "CVE-M" not in plan.entries
    assert plan.skipped == {"CVE-OLD": "already allowlisted"}
    assert set(plan.entries["CVE-C"]) >= {
        "package",
        "severity",
        "reason",
        "expires",
        "added_by",
        "case",
        "ticket",
    }


def test_plan_entries_never_redates_a_breached_sla():
    plan = allowlist.plan_entries(
        [_t("CVE-C", "CRITICAL")],
        {},
        detected=date(2026, 9, 1),
        today=date(2026, 9, 28),
        ticket="FND-1",
        added_by="bot",
    )
    assert not plan.entries and plan.skipped["CVE-C"].startswith("SLA already breached")


def test_apply_bumps_updated_only_when_adding():
    data = {"_updated": "2026-01-01"}
    assert allowlist.apply(data, {}, date(2026, 9, 28))["_updated"] == "2026-01-01"
    out = allowlist.apply(data, {"CVE-1": {}}, date(2026, 9, 28))
    assert out["_updated"] == "2026-09-28" and "CVE-1" in out


# --------------------------------------------------------------------------- bump


def test_set_constraints_adds_new_and_raises_existing():
    out = bump.set_constraints(
        PYPROJECT, {"requests": ("2.32.4", ["CVE-1"]), "urllib3": ("2.5.0", ["CVE-2"])}
    )
    assert '"requests>=2.32.4",  # CVE-OLD, CVE-1' in out
    assert '    "urllib3>=2.5.0",  # CVE-2\n]' in out
    assert '"protobuf>=6.33.5",          # CVE-2026-0994' in out  # untouched


def test_set_constraints_never_lowers_a_floor():
    out = bump.set_constraints(PYPROJECT, {"protobuf": ("6.0.0", ["CVE-9"])})
    assert out == PYPROJECT


def test_set_constraints_without_block_raises():
    with pytest.raises(bump.BumpError):
        bump.set_constraints("[project]\n", {"a": ("1", [])})


def _lock(version, upload="2024-01-01T00:00:00Z"):
    return {"requests": {"version": version, "upload_time": upload}}


def test_verify_ok_and_fresh_release_flagged():
    errors, fresh = bump.verify(
        old_lock=_lock("2.32.3"),
        new_lock=_lock("2.32.4", "2026-09-26T00:00:00Z"),
        old_text=LOCK,
        new_text=LOCK,
        targets={"requests": "2.32.4"},
        now=NOW,
        cooldown_days=7,
    )
    assert errors == [] and fresh and "requests 2.32.4" in fresh[0]


def test_verify_rejects_unmoved_target_new_host_and_lost_revision():
    new_text = LOCK.replace("revision = 3\n", "").replace(
        "pypi.org/simple", "factory.endorlabs.com/x"
    )
    errors, _ = bump.verify(
        old_lock=_lock("2.32.3"),
        new_lock=_lock("2.32.3"),
        old_text=LOCK,
        new_text=new_text,
        targets={"requests": "2.32.4"},
        now=NOW,
        cooldown_days=7,
    )
    joined = " ".join(errors)
    assert (
        "revision" in joined
        and "factory.endorlabs.com" in joined
        and "below the fix" in joined
    )


# --------------------------------------------------------------------------- report


def test_report_lists_every_cve_and_human_actions():
    triages = [
        classify.Triage(
            "CVE-1",
            "HIGH",
            1,
            "requests",
            "2.32.3",
            "2.32.4",
            "our dependency",
            "Case 1: x.",
            bump_to="2.32.4",
        ),
        classify.Triage(
            "CVE-2", "HIGH", 4, "openssl", "3.0", "3.1", "base image", "Case 4: y."
        ),
        classify.Triage("CVE-3", "HIGH", classify.KILLED, reason="gone"),
    ]
    o = report.Outcome(
        allowlist_pr="https://gh/pr/1",
        allowlist_entries={
            "CVE-1": {"expires": "2026-10-25"},
            "CVE-2": {"expires": "2026-10-25"},
        },
        bump_pr="https://gh/pr/2",
        bump_labelled=True,
    )
    body = report.render("FND-1", triages, o, "https://run")
    for s in (
        "CVE-1",
        "CVE-2",
        "CVE-3",
        "https://gh/pr/1",
        "https://gh/pr/2",
        "app-runtime-base:3",
        report.MARKER,
    ):
        assert s in body


# --------------------------------------------------------------------------- run (end to end, fake effects)


class FakeRunner:
    """Records commands; answers the few whose output run() reads."""

    def __init__(
        self, root: Path, *, open_prs=(), remote=(), uv_version="2.32.4", uv_rc=0
    ):
        self.root, self.open_prs, self.remote = root, set(open_prs), set(remote)
        self.uv_version, self.uv_rc = uv_version, uv_rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out = ""
        rc = 0
        if cmd[:2] == ["git", "rev-parse"]:
            out = "basesha\n"
        elif cmd[:3] == ["gh", "pr", "list"]:
            branch = cmd[cmd.index("--head") + 1]
            out = f"https://gh/existing/{branch}" if branch in self.open_prs else ""
        elif cmd[:2] == ["git", "ls-remote"]:
            out = "sha\trefs/heads/x" if cmd[-1] in self.remote else ""
        elif cmd[:3] == ["gh", "pr", "create"]:
            out = f"https://gh/pr/{cmd[cmd.index('--head') + 1]}\n"
        elif cmd[:2] == ["uv", "lock"]:
            rc = self.uv_rc
            if rc == 0:
                p = self.root / "uv.lock"
                p.write_text(
                    p.read_text().replace(
                        'version = "2.32.3"', f'version = "{self.uv_version}"'
                    )
                )
        elif cmd[:2] == ["git", "checkout"] and "-B" in cmd:
            # a fresh branch from base: restore the tracked files the test mutates
            for name, text in self.pristine.items():
                (self.root / name).write_text(text)
        return subprocess.CompletedProcess(
            cmd, rc, stdout=out, stderr="boom" if rc else ""
        )

    def cmds(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".security").mkdir(parents=True)
    (root / ".security/base-allowlist.json").write_text(
        json.dumps(
            {"_expiry_policy": {"CRITICAL": 7, "HIGH": 30}, "_updated": "2026-01-01"},
            indent=2,
        )
    )
    (root / "uv.lock").write_text(LOCK)
    (root / "pyproject.toml").write_text(PYPROJECT)
    write_scan(
        root / "scan",
        fs=[vuln("CVE-1", "requests", "2.32.3", "2.32.4")],
        image=[vuln("CVE-2", "openssl", "3.0", "3.1"), vuln("CVE-9", "other", "1")],
    )
    return root


def _issue(cves, created="2026-09-27T10:00:00.000Z"):
    return {
        "id": "uuid-1",
        "identifier": "FND-1",
        "createdAt": created,
        "description": f"x\n<!-- vuln-ids: {','.join(cves)} -->\n",
    }


def _run(repo, runner, cves=("CVE-1", "CVE-2", "CVE-3"), dry=False):
    comments = []
    runner.pristine = {
        n: (repo / n).read_text()
        for n in ("uv.lock", "pyproject.toml", ".security/base-allowlist.json")
    }
    ctx = Context(
        repo="o/r",
        root=repo,
        ticket="FND-1",
        scan_dir=repo / "scan",
        run_url="https://run",
        run_id="42",
        dry_run=dry,
        now=NOW,
    )
    deps = Deps(
        runner=runner,
        fetch_issue=lambda _t: _issue(cves),
        comment=lambda i, b: comments.append((i, b)),
        upstream=alive,
    )
    return run(ctx, deps), comments


def test_run_opens_allowlist_and_bump_prs_as_the_two_gate_shapes(repo):
    r = FakeRunner(repo)
    out, comments = _run(repo, r)
    adds = r.cmds("git", "add")
    assert adds[0][3:] == [".security/base-allowlist.json"]
    assert adds[1][3:] == ["pyproject.toml", "uv.lock"]
    branches = [c[c.index("--head") + 1] for c in r.cmds("gh", "pr", "create")]
    assert branches[0].startswith("chore/allowlist-") and branches[1].startswith(
        "fix/bump-"
    )
    creates = r.cmds("gh", "pr", "create")
    assert all("vuln-auto-merge" in c for c in creates)
    assert creates[1][creates[1].index("--title") + 1].startswith(
        "chore(deps): bump requests to 2.32.4"
    )
    assert r.cmds("python3")  # validator ran before the push
    assert set(out.allowlist_entries) == {"CVE-1", "CVE-2"}
    assert out.allowlist_entries["CVE-1"]["expires"] == "2026-10-27"  # createdAt + 30d
    assert comments and "CVE-3" in comments[0][1] and "killed" in comments[0][1]
    assert r.calls[-1][:3] == ["git", "checkout", "--force"]  # back on base


def test_run_is_idempotent_on_an_open_pr(repo):
    r = FakeRunner(repo, open_prs={"chore/allowlist-cve-1-cve-2"})
    out, _ = _run(repo, r)
    assert out.allowlist_pr == "https://gh/existing/chore/allowlist-cve-1-cve-2"
    assert all(".security/base-allowlist.json" not in c for c in r.cmds("git", "add"))


def test_run_suffixes_a_leftover_branch_instead_of_force_pushing(repo):
    r = FakeRunner(repo, remote={"chore/allowlist-cve-1-cve-2"})
    _run(repo, r)
    pushes = [c[-1] for c in r.cmds("git", "push")]
    assert "HEAD:refs/heads/chore/allowlist-cve-1-cve-2-r42" in pushes
    assert not any("--force" in c for c in r.cmds("git", "push"))


def test_run_failed_lock_opens_no_bump_pr_and_says_why(repo):
    r = FakeRunner(repo, uv_rc=1)
    out, comments = _run(repo, r)
    assert not out.bump_pr and "could not resolve" in out.bump_problem
    assert len(r.cmds("gh", "pr", "create")) == 1  # the allowlist PR only
    assert "Needs a human" in comments[0][1]


def test_run_dry_run_pushes_and_comments_nothing(repo, capsys):
    r = FakeRunner(repo)
    out, comments = _run(repo, r, dry=True)
    assert (
        not r.cmds("git", "push") and not r.cmds("gh", "pr", "create") and not comments
    )
    assert set(out.allowlist_entries) == {"CVE-1", "CVE-2"}
    assert "dry run" in capsys.readouterr().out


def test_run_refuses_a_dirty_checkout(repo):
    class Dirty(FakeRunner):
        def __call__(self, cmd, **kw):
            if cmd[:2] == ["git", "status"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=" M run.py\n", stderr=""
                )
            return super().__call__(cmd, **kw)

    r = Dirty(repo)
    with pytest.raises(SystemExit, match="uncommitted"):
        _run(repo, r)
    assert not r.cmds("git", "checkout")


def test_uv_cause_drops_hints():
    err = "  × No solution found when resolving dependencies:\n  ╰─▶ Because x<2 ...\n  hint: limit requires-python\n"
    assert (
        _uv_cause(err)
        == "No solution found when resolving dependencies: / Because x<2 ..."
    )


def test_listing_caps_long_titles():
    assert _listing(["a", "b"]) == "a, b"
    assert _listing([f"CVE-{i}" for i in range(15)]) == "CVE-0, CVE-1, CVE-2 (+12 more)"


@pytest.mark.parametrize(
    "argv,want",
    [
        ([], False),
        (["--dry-run"], True),
        (["--dry-run", "true"], True),
        (["--dry-run", "false"], False),
        (["--dry-run", ""], False),
    ],
)
def test_cli_dry_run_accepts_the_workflow_boolean(monkeypatch, argv, want):
    seen = {}
    monkeypatch.setattr(
        cli, "run", lambda ctx, deps: seen.setdefault("dry", ctx.dry_run)
    )
    cli.main(["--repo", "o/r", "--root", ".", "--ticket", "FND-1", *argv])
    assert seen["dry"] is want


def test_newest_successful_run_ignores_failures_and_order():
    runs = [
        {"databaseId": 1, "conclusion": "success", "createdAt": "2026-09-01T00:00:00Z"},
        {"databaseId": 3, "conclusion": "failure", "createdAt": "2026-09-03T00:00:00Z"},
        {"databaseId": 2, "conclusion": "success", "createdAt": "2026-09-02T00:00:00Z"},
    ]
    assert newest_successful_run(runs) == "2"
    assert newest_successful_run([]) == ""


def test_run_medium_ticket_is_tracked_only(repo):
    write_scan(
        repo / "scan", fs=[vuln("CVE-M", "requests", "2.32.3", "2.32.4", sev="MEDIUM")]
    )
    r = FakeRunner(repo)
    out, comments = _run(repo, r, cves=("CVE-M",))
    assert not r.cmds("gh", "pr", "create") and not out.allowlist_entries
    assert "tracked" in comments[0][1]
