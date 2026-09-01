"""Tests for .github/scripts/release_guard.py.

The guard fails open on every error path, so a test suite that only checked
"nothing breaks" would pass even if the guard never fired at all. The tests
below therefore assert the *positive* case first and hardest: given a remote
branch that is already at or beyond the computed version, the guard must say
skip.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import release_guard

PYPROJECT = "packages/conformance/pyproject.toml"
PKLPROJECT = "contract-toolkit/src/PklProject"
PKLPROJECT_VERSION = 'package {\n  version = "0.23.0"\n}\n'


def _fake_git(monkeypatch, *, show: dict[str, str] | None = None, fetch_rc: int = 0):
    """Stub subprocess.run for git fetch/show.

    ``show`` maps a ref-qualified path (``"origin/main:some/path"``) to the file
    content git should return; anything absent exits non-zero, as git does for
    an unknown ref or path.
    """
    show = show or {}

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, fetch_rc, "", "")
        if cmd[:2] == ["git", "show"]:
            key = cmd[2]
            if key in show:
                return subprocess.CompletedProcess(cmd, 0, show[key], "")
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: invalid object")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(release_guard.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# The guard fires — the case the incident needed
# ---------------------------------------------------------------------------


class TestGuardFires:
    def test_indented_pklproject_version_is_already_released(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_git(monkeypatch, show={f"origin/main:{PKLPROJECT}": PKLPROJECT_VERSION})

        skip, remote = release_guard.already_released(PKLPROJECT, "0.23.0")

        assert skip is True
        assert remote == "0.23.0"

    def test_equal_version_is_already_released(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact application-sdk#3570 shape.

        A frozen checkout read 0.24.0 and computed 0.25.0, while origin/main was
        already at 0.25.0 from the release that had just merged.
        """
        _fake_git(
            monkeypatch, show={f"origin/main:{PYPROJECT}": 'version = "0.25.0"\n'}
        )

        skip, remote = release_guard.already_released(PYPROJECT, "0.25.0")

        assert skip is True
        assert remote == "0.25.0"

    def test_remote_ahead_is_already_released(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_git(
            monkeypatch, show={f"origin/main:{PYPROJECT}": 'version = "0.26.0"\n'}
        )

        skip, _ = release_guard.already_released(PYPROJECT, "0.25.0")

        assert skip is True

    def test_patch_race_against_a_newer_minor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """String comparison would get this wrong; tuple comparison must not.

        A fix-only sibling computes 0.24.1 while main has already moved to
        0.25.0. ``"0.25.0" >= "0.24.1"`` happens to hold as strings here, but
        the 0.9.0/0.10.0 case below is where string ordering breaks.
        """
        _fake_git(
            monkeypatch, show={f"origin/main:{PYPROJECT}": 'version = "0.25.0"\n'}
        )

        skip, _ = release_guard.already_released(PYPROJECT, "0.24.1")

        assert skip is True

    def test_double_digit_minor_beats_string_ordering(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``"0.9.0" > "0.10.0"`` as strings — the guard must not be fooled."""
        _fake_git(
            monkeypatch, show={f"origin/main:{PYPROJECT}": 'version = "0.10.0"\n'}
        )

        skip, _ = release_guard.already_released(PYPROJECT, "0.9.0")

        assert skip is True


# ---------------------------------------------------------------------------
# The guard stays out of the way — normal releases must not be blocked
# ---------------------------------------------------------------------------


class TestGuardAllowsRealRelease:
    def test_remote_behind_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ordinary case: main is at the version we are bumping *from*."""
        _fake_git(
            monkeypatch, show={f"origin/main:{PYPROJECT}": 'version = "0.24.0"\n'}
        )

        skip, remote = release_guard.already_released(PYPROJECT, "0.25.0")

        assert skip is False
        assert remote == "0.24.0"

    def test_double_digit_minor_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_git(monkeypatch, show={f"origin/main:{PYPROJECT}": 'version = "0.9.0"\n'})

        skip, _ = release_guard.already_released(PYPROJECT, "0.10.0")

        assert skip is False


# ---------------------------------------------------------------------------
# Fail-open paths — never block a release because the check itself failed
# ---------------------------------------------------------------------------


class TestFailsOpen:
    def test_unreadable_path_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_git(monkeypatch, show={})

        skip, remote = release_guard.already_released(PYPROJECT, "0.25.0")

        assert skip is False
        assert remote is None

    def test_fetch_failure_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No network / no token scope must not turn into a red release lane."""
        _fake_git(monkeypatch, show={}, fetch_rc=128)

        skip, _ = release_guard.already_released(PYPROJECT, "0.25.0")

        assert skip is False

    def test_unparseable_remote_version_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_git(
            monkeypatch, show={f"origin/main:{PYPROJECT}": "name = 'no version here'\n"}
        )

        skip, _ = release_guard.already_released(PYPROJECT, "0.25.0")

        assert skip is False

    def test_prerelease_computed_version_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering a pre-release needs full semver; fail open rather than guess."""
        _fake_git(monkeypatch, show={f"origin/main:{PYPROJECT}": 'version = "1.0.0"\n'})

        skip, _ = release_guard.already_released(PYPROJECT, "1.0.0-rc1")

        assert skip is False

    def test_prerelease_remote_version_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_git(
            monkeypatch, show={f"origin/main:{PYPROJECT}": 'version = "1.0.0-rc1"\n'}
        )

        skip, _ = release_guard.already_released(PYPROJECT, "1.0.0")

        assert skip is False


# ---------------------------------------------------------------------------
# Ref selection
# ---------------------------------------------------------------------------


class TestRefHandling:
    def test_reads_remote_tracking_ref_not_fetch_head(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FETCH_HEAD is mutable global state; the tracking ref must win.

        Both refs are readable here and they disagree — the guard must report
        the remote-tracking one.
        """
        _fake_git(
            monkeypatch,
            show={
                f"origin/main:{PYPROJECT}": 'version = "0.25.0"\n',
                f"FETCH_HEAD:{PYPROJECT}": 'version = "0.11.0"\n',
            },
        )

        assert release_guard.version_on_branch(PYPROJECT) == "0.25.0"

    def test_falls_back_to_fetch_head(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A checkout with no remote-tracking ref still gets an answer."""
        _fake_git(monkeypatch, show={f"FETCH_HEAD:{PYPROJECT}": 'version = "0.25.0"\n'})

        assert release_guard.version_on_branch(PYPROJECT) == "0.25.0"

    def test_honours_non_main_target_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """App repos pass target_branch; the guard must read that branch."""
        _fake_git(
            monkeypatch,
            show={f"origin/release-2.x:{PYPROJECT}": 'version = "2.4.0"\n'},
        )

        skip, remote = release_guard.already_released(
            PYPROJECT, "2.4.0", branch="release-2.x"
        )

        assert skip is True
        assert remote == "2.4.0"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


class TestParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ('version = "1.2.3"', "1.2.3"),
            ("version = '1.2.3'", None),  # single quotes are not the spelling used
            ('name = "x"\nversion = "0.1.0"\n', "0.1.0"),
            ('amends "../App.pkl"\n  version = "9.9.9"\n', "9.9.9"),  # PklProject
            (PKLPROJECT_VERSION, "0.23.0"),
            ("no version at all", None),
            ("", None),
        ],
    )
    def test_parse_version(self, text: str, expected: str | None) -> None:
        assert release_guard.parse_version(text) == expected

    @pytest.mark.parametrize(
        "version,expected",
        [
            ("1.2.3", (1, 2, 3)),
            ("0.10.0", (0, 10, 0)),
            ("10.0.0", (10, 0, 0)),
            ("1.0.0-rc1", None),
            ("1.0", None),
            ("1.2.3.4", None),
            ("", None),
            (None, None),
        ],
    )
    def test_version_tuple(self, version: str | None, expected) -> None:
        assert release_guard.version_tuple(version) == expected


class TestSkipMessage:
    def test_names_both_versions_and_the_path(self) -> None:
        msg = release_guard.skip_message(PYPROJECT, "0.25.0", "0.25.0")

        assert PYPROJECT in msg
        assert "0.25.0" in msg
        assert "origin/main" in msg
