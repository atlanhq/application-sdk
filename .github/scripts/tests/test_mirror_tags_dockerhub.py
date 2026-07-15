"""Tests for .github/scripts/mirror_tags_dockerhub.py.

Covers the conditional logic that used to be inlined in the
*Push to Docker Hub* step of build-and-publish-app.yaml:

  * Empty / whitespace-only release_tags → no crane calls (no-op).
  * Non-empty release_tags → ``crane tag`` called once per non-empty line.
  * Blank lines inside release_tags are skipped.
  * The returned list reflects exactly the Docker Hub refs that were tagged.
  * ``crane`` failures propagate (subprocess.CalledProcessError is not swallowed).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import mirror_tags_dockerhub as mod

DOCKERHUB_IMAGE = "docker.io/atlanhq/atlan-mysql-app:main-abc1234"
GHCR_BASE = "ghcr.io/atlanhq/atlan-mysql-app"
DH_REPO = "docker.io/atlanhq/atlan-mysql-app"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_release_tags(*tags: str) -> str:
    return "\n".join(f"{GHCR_BASE}:{t}" for t in tags)


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------


class TestNoOp:
    def test_empty_string_no_crane_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "run", lambda cmd: calls.append(cmd))
        result = mod.mirror_tags(DOCKERHUB_IMAGE, "")
        assert calls == []
        assert result == []

    def test_whitespace_only_no_crane_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "run", lambda cmd: calls.append(cmd))
        result = mod.mirror_tags(DOCKERHUB_IMAGE, "   \n  \n")
        assert calls == []
        assert result == []

    def test_none_release_tags_no_crane_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "run", lambda cmd: calls.append(cmd))
        # Simulate omitted --release-tags (argparse default is "")
        result = mod.mirror_tags(DOCKERHUB_IMAGE, "")
        assert calls == []
        assert result == []


# ---------------------------------------------------------------------------
# Tag mirroring
# ---------------------------------------------------------------------------


class TestMirrorTags:
    def test_single_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "run", lambda cmd: calls.append(cmd))

        release_tags = _make_release_tags("2.0.1")
        result = mod.mirror_tags(DOCKERHUB_IMAGE, release_tags)

        assert calls == [["crane", "tag", DOCKERHUB_IMAGE, "2.0.1"]]
        assert result == [f"{DH_REPO}:2.0.1"]

    def test_full_semver_ladder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All four tags (:X.Y.Z, :X.Y, :X, :latest) are mirrored."""
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "run", lambda cmd: calls.append(cmd))

        release_tags = _make_release_tags("2.0.1", "2.0", "2", "latest")
        result = mod.mirror_tags(DOCKERHUB_IMAGE, release_tags)

        assert [c[3] for c in calls] == ["2.0.1", "2.0", "2", "latest"]
        assert result == [
            f"{DH_REPO}:2.0.1",
            f"{DH_REPO}:2.0",
            f"{DH_REPO}:2",
            f"{DH_REPO}:latest",
        ]

    def test_blank_lines_in_tags_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "run", lambda cmd: calls.append(cmd))

        release_tags = f"{GHCR_BASE}:2.0.1\n\n{GHCR_BASE}:2.0\n  \n{GHCR_BASE}:2"
        result = mod.mirror_tags(DOCKERHUB_IMAGE, release_tags)

        assert [c[3] for c in calls] == ["2.0.1", "2.0", "2"]
        assert len(result) == 3

    def test_tag_extracted_from_ghcr_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The tag portion after ':' is correctly extracted from GHCR refs."""
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "run", lambda cmd: calls.append(cmd))

        ref = f"{GHCR_BASE}:3.1.4"
        mod.mirror_tags(DOCKERHUB_IMAGE, ref)

        assert calls[0] == ["crane", "tag", DOCKERHUB_IMAGE, "3.1.4"]

    def test_dockerhub_repo_prefix_derived_correctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Return value uses the DH repo prefix (without the source tag)."""
        monkeypatch.setattr(mod, "run", lambda cmd: None)

        release_tags = _make_release_tags("1.2.3")
        result = mod.mirror_tags("docker.io/atlanhq/other-app:branch-sha", release_tags)

        assert result == ["docker.io/atlanhq/other-app:1.2.3"]


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    def test_crane_failure_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail(cmd: list[str]) -> None:
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(mod, "run", _fail)

        with pytest.raises(subprocess.CalledProcessError):
            mod.mirror_tags(DOCKERHUB_IMAGE, _make_release_tags("2.0.1"))


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


class TestCLI:
    def test_main_no_release_tags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "run", lambda cmd: calls.append(cmd))
        mod.main(["--dockerhub-image", DOCKERHUB_IMAGE])
        assert calls == []

    def test_main_with_release_tags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(mod, "run", lambda cmd: calls.append(cmd))
        mod.main(
            [
                "--dockerhub-image",
                DOCKERHUB_IMAGE,
                "--release-tags",
                _make_release_tags("2.0.1", "2.0"),
            ]
        )
        assert [c[3] for c in calls] == ["2.0.1", "2.0"]
