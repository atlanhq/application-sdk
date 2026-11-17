"""Tests for ``detect --since``: gate on the change, not the repository.

A whole-repo run fails on every blocking finding, old or new. ``--since REF``
fails only on blocking findings the working tree has over the merge-base of
``REF`` and ``HEAD``. The load-bearing properties:

* a finding the base already carries never fails the run, however the change
  moves or rewords it;
* a finding the change adds always does, including in a file the base had
  violations in already;
* warnings never gate, and ``--exit-zero`` still wins;
* the SARIF keeps the whole-repo result, so the dashboard is unaffected;
* when the base cannot be compared, the verdict falls back to the stricter
  whole-repo one.

The repos here are real git repositories, because the base tree is read
through ``git worktree``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conformance.suite import runner
from conformance.suite.introduced import introduced
from conformance.suite.schema.findings import Finding

# E002 (a typed ``except`` that swallows) is BLOCK tier.
_SWALLOW = (
    "def {name}():\n    try:\n        pass\n    except Exception:\n        pass\n"
)
# E010 (``gather(return_exceptions=True)`` never inspected) is WARN tier.
_WARN_ONLY = (
    "import asyncio\n\n\n"
    "async def run(tasks):\n"
    "    results = await asyncio.gather(*tasks, return_exceptions=True)\n"
    "    return len(results)\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        },
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """An app whose base commit already carries one blocking finding."""
    root = tmp_path / "app-repo"
    (root / "app").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "atlan-demo-app"\nversion = "0.1.0"\n'
    )
    (root / "app" / "old.py").write_text(_SWALLOW.format(name="old"))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "change")
    return root


def _run(repo: Path, *extra: str, out: Path | None = None) -> int:
    argv = ["--repo", str(repo), "--series", "E", *extra]
    argv += ["--output", str(out or repo.parent / "out.sarif")]
    return runner.main(argv)


def test_whole_repo_run_fails_on_the_inherited_finding(repo: Path) -> None:
    """The control: without --since this repo is red before any change."""
    assert _run(repo) == 1


def test_an_inherited_finding_does_not_fail_the_change(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(repo, "--since", "main") == 0
    out = capsys.readouterr().out
    assert "0 blocking introduced by this change, 1 inherited" in out


def test_a_finding_in_a_new_file_fails_the_change(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "app" / "new.py").write_text(_SWALLOW.format(name="new"))
    assert _run(repo, "--since", "main") == 1
    out = capsys.readouterr().out
    assert "1 blocking introduced by this change, 1 inherited" in out
    assert "[E002] [INTRODUCED] app/new.py" in out
    assert "[INTRODUCED] app/old.py" not in out


def test_a_committed_change_is_judged_the_same_as_an_uncommitted_one(
    repo: Path,
) -> None:
    """CI sees commits; a local pre-PR run sees a dirty tree. Same verdict."""
    (repo / "app" / "new.py").write_text(_SWALLOW.format(name="new"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add")
    assert _run(repo, "--since", "main") == 1


def test_moving_an_inherited_finding_is_not_introducing_it(repo: Path) -> None:
    """The line number is not identity: code above the finding changed."""
    old = repo / "app" / "old.py"
    old.write_text("import os\n\nX = os.sep\n\n\n" + old.read_text())
    assert _run(repo, "--since", "main") == 0


def test_a_second_finding_in_a_file_that_already_had_one_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = repo / "app" / "old.py"
    old.write_text(old.read_text() + "\n\n" + _SWALLOW.format(name="again"))
    assert _run(repo, "--since", "main") == 1
    assert "1 blocking introduced by this change, 1 inherited" in (
        capsys.readouterr().out
    )


def test_renaming_a_file_does_not_introduce_its_findings(repo: Path) -> None:
    _git(repo, "mv", "app/old.py", "app/renamed.py")
    assert _run(repo, "--since", "main") == 0


def test_fixing_the_inherited_finding_passes(repo: Path) -> None:
    (repo / "app" / "old.py").write_text("def old():\n    return 1\n")
    assert _run(repo, "--since", "main") == 0


def test_an_added_warning_never_gates(repo: Path) -> None:
    (repo / "app" / "warn.py").write_text(_WARN_ONLY)
    assert _run(repo, "--since", "main") == 0


def test_the_sarif_keeps_the_whole_repo_result(repo: Path) -> None:
    """The dashboard and the scheduled run read the SARIF, not the exit code."""
    out = repo.parent / "since.sarif"
    assert _run(repo, "--since", "main", out=out) == 0
    run = json.loads(out.read_text())["runs"][0]
    assert "E002" in {r["ruleId"] for r in run["results"]}
    assert run["invocations"][0]["exitCode"] == 1


def test_an_unresolvable_base_falls_back_to_the_stricter_verdict(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(repo, "--since", "no-such-ref") == 1
    assert "falling back to the whole-repository verdict" in capsys.readouterr().out


def test_outside_a_git_repo_falls_back_too(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "atlan-demo-app"\n')
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "old.py").write_text(_SWALLOW.format(name="old"))
    out = tmp_path / "out.sarif"
    argv = ["--repo", str(tmp_path), "--series", "E", "--output", str(out)]
    assert runner.main([*argv, "--since", "main"]) == 1


def test_exit_zero_still_wins(repo: Path) -> None:
    (repo / "app" / "new.py").write_text(_SWALLOW.format(name="new"))
    assert _run(repo, "--since", "main", "--exit-zero") == 0


def test_the_environment_supplies_the_default(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI sets it once on the job; each per-series step inherits it."""
    monkeypatch.setenv(runner.SINCE_ENV, "main")
    assert _run(repo) == 0
    monkeypatch.setenv(runner.SINCE_ENV, "")
    assert _run(repo) == 1


def test_the_base_worktree_is_removed(repo: Path) -> None:
    (repo / "app" / "new.py").write_text(_SWALLOW.format(name="new"))
    _run(repo, "--since", "main")
    listed = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "worktree", "list"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert len(listed.strip().splitlines()) == 1


def test_a_plain_mv_is_followed_and_the_index_is_left_alone(repo: Path) -> None:
    """``mv`` leaves the destination untracked; ``git mv`` is not required.

    The rename is found by staging into a throwaway index, so nothing the user
    has (or has not) staged may change.
    """
    (repo / "app" / "old.py").rename(repo / "app" / "moved.py")
    before = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "status", "--porcelain"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert _run(repo, "--since", "main") == 0
    after = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "status", "--porcelain"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after == before
    assert "?? app/moved.py" in after


def test_the_change_is_judged_against_the_merge_base_not_the_ref_tip(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main`` moved on after the fork and gained a finding in the same file
    the change adds one to. Compared with the tip of ``main``, that later
    finding would pay for the change's own; compared with the merge-base, it
    cannot."""
    _git(repo, "checkout", "-q", "main")
    (repo / "app" / "shared.py").write_text(_SWALLOW.format(name="on_main"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main moves on")
    _git(repo, "checkout", "-q", "change")
    (repo / "app" / "shared.py").write_text(_SWALLOW.format(name="on_change"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the change")

    assert _run(repo, "--since", "main") == 1
    out = capsys.readouterr().out
    assert "1 blocking introduced by this change, 1 inherited" in out
    assert "[E002] [INTRODUCED] app/shared.py" in out


def test_a_subdirectory_repo_is_compared_with_the_same_subdirectory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--repo`` names an app inside a larger checkout. A finding elsewhere in
    the checkout is neither the app's backlog nor its baseline."""
    root = tmp_path / "mono"
    app = root / "apps" / "connector"
    (app / "app").mkdir(parents=True)
    (app / "pyproject.toml").write_text(
        '[project]\nname = "atlan-demo-app"\nversion = "0.1.0"\n'
    )
    (app / "app" / "old.py").write_text(_SWALLOW.format(name="old"))
    (root / "elsewhere").mkdir()
    (root / "elsewhere" / "noise.py").write_text(_SWALLOW.format(name="noise"))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "change")

    out_file = tmp_path / "out.sarif"
    argv = ["--repo", str(app), "--series", "E", "--output", str(out_file)]
    assert runner.main([*argv, "--since", "main"]) == 0
    assert "0 blocking introduced by this change, 1 inherited" in (
        capsys.readouterr().out
    )

    (app / "app" / "new.py").write_text(_SWALLOW.format(name="new"))
    assert runner.main([*argv, "--since", "main"]) == 1
    out = capsys.readouterr().out
    assert "1 blocking introduced by this change, 1 inherited" in out
    assert "[E002] [INTRODUCED] app/new.py" in out


def test_an_unparseable_file_still_fails_and_still_annotates_as_an_error(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing blocking was introduced, but a file went unanalysed. The run
    fails, so its annotations must not be downgraded to warnings as they are
    when --since turns an inherited-only failure into a pass."""
    (repo / "app" / "broken.py").write_text("def broken(:\n")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert _run(repo, "--since", "main") == 1
    out = capsys.readouterr().out
    assert "0 blocking introduced by this change" in out
    assert "::error" in out


def test_an_inherited_only_pass_annotates_as_a_warning(
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert _run(repo, "--since", "main") == 0
    out = capsys.readouterr().out
    assert "::error" not in out
    assert "::warning" in out


# -- introduced(): the counting rule on its own --------------------------------


def _f(rule: str, file: str, line: int, disc: str | None = None) -> Finding:
    return Finding(rule, file, line, 1, "m", discriminator=disc)


def test_counts_are_per_rule_file_and_discriminator() -> None:
    base = [_f("E002", "a.py", 3), _f("T025", "pyproject.toml", 1, "crawl")]
    head = [
        _f("E002", "a.py", 9),
        _f("E002", "b.py", 3),
        _f("L010", "a.py", 9),
        _f("T025", "pyproject.toml", 1, "crawl"),
        _f("T025", "pyproject.toml", 1, "mine"),
    ]
    got = introduced(head, base)
    assert {(f.rule_id, f.file, f.discriminator) for f in got} == {
        ("E002", "b.py", None),
        ("L010", "a.py", None),
        ("T025", "pyproject.toml", "mine"),
    }


def test_the_later_findings_are_the_ones_named() -> None:
    head = [_f("E002", "a.py", 40), _f("E002", "a.py", 5), _f("E002", "a.py", 20)]
    got = introduced(head, [_f("E002", "a.py", 5)])
    assert [f.line for f in got] == [20, 40]


def test_a_base_finding_follows_its_file_through_a_rename() -> None:
    head = [_f("E002", "new/a.py", 3)]
    base = [_f("E002", "old/a.py", 3)]
    assert introduced(head, base, {"old/a.py": "new/a.py"}) == []
    assert len(introduced(head, base)) == 1


def test_a_dotfile_and_its_undotted_namesake_are_different_files() -> None:
    """Only an exact leading ``./`` is normalised away."""
    head = [_f("E002", "foo.py", 3)]
    assert len(introduced(head, [_f("E002", ".foo.py", 3)])) == 1
    assert introduced(head, [_f("E002", "./foo.py", 3)]) == []
