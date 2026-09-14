"""Tests for .github/scripts/contract_toolkit_release.py.

Covers the two pure functions that decide what version a toolkit release cuts.
The rest of the script is git/gh plumbing exercised by the workflow itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import contract_toolkit_release

# ---------------------------------------------------------------------------
# compute_bump — which KIND of bump the commits call for
# ---------------------------------------------------------------------------


class TestComputeBump:
    def test_bang_marker_is_major(self) -> None:
        assert (
            contract_toolkit_release.compute_bump("feat!: drop a widget", "") == "major"
        )

    def test_bang_marker_with_scope_is_major(self) -> None:
        subject = "feat(contract-toolkit)!: generate default from defaultSelection"
        assert contract_toolkit_release.compute_bump(subject, "") == "major"

    def test_breaking_change_body_trailer_is_major(self) -> None:
        body = "BREAKING CHANGE: DropDown.default is now generated\n"
        assert (
            contract_toolkit_release.compute_bump("feat: add defaultSelection", body)
            == "major"
        )

    def test_feat_is_minor(self) -> None:
        assert (
            contract_toolkit_release.compute_bump("feat: add a widget", "") == "minor"
        )

    def test_fix_is_patch(self) -> None:
        assert (
            contract_toolkit_release.compute_bump("fix: correct a regex", "") == "patch"
        )

    def test_chore_is_patch(self) -> None:
        assert contract_toolkit_release.compute_bump("chore: tidy docs", "") == "patch"

    def test_empty_is_patch(self) -> None:
        assert contract_toolkit_release.compute_bump("", "") == "patch"


# ---------------------------------------------------------------------------
# bump_version — what that bump does to the NUMBER
# ---------------------------------------------------------------------------


class TestBumpVersion:
    def test_minor_bump_resets_patch(self) -> None:
        assert contract_toolkit_release.bump_version("1.2.3", "minor") == "1.3.0"

    def test_patch_bump_increments_patch(self) -> None:
        assert contract_toolkit_release.bump_version("1.2.3", "patch") == "1.2.4"

    def test_breaking_past_one_bumps_major(self) -> None:
        assert contract_toolkit_release.bump_version("1.2.3", "major") == "2.0.0"

    # Semver §4: while the major version is zero the API is not declared stable,
    # so a breaking change bumps the MINOR. Cutting 1.0.0 declares stability for
    # the whole package and stays a human decision — a single `feat!:` commit
    # must never trigger it. Regression guard for the toolkit release that tried
    # to cut v1.0.0 off the FND-1980 breaking change.
    def test_breaking_on_zero_major_bumps_minor(self) -> None:
        assert contract_toolkit_release.bump_version("0.25.2", "major") == "0.26.0"

    def test_breaking_on_zero_major_never_reaches_one(self) -> None:
        assert contract_toolkit_release.bump_version("0.1.0", "major") == "0.2.0"

    def test_breaking_on_zero_zero_bumps_minor(self) -> None:
        assert contract_toolkit_release.bump_version("0.0.5", "major") == "0.1.0"


# ---------------------------------------------------------------------------
# The two together, on the actual release this fixed
# ---------------------------------------------------------------------------


class TestEndToEndVersionChoice:
    def test_fnd1980_breaking_change_cuts_0_26_0_not_1_0_0(self) -> None:
        """The real subject that produced a v1.0.0 bump PR."""
        subject = "feat(contract-toolkit)!: generate a multiSelect DropDown default from a structured defaultSelection"
        bump = contract_toolkit_release.compute_bump(subject, "")
        assert bump == "major"
        assert contract_toolkit_release.bump_version("0.25.2", bump) == "0.26.0"
