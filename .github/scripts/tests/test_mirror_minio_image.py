"""Tests for .github/scripts/mirror_minio_image.py.

The refresh must never repoint an existing mirror tag, never mistake an auth
failure for "tag absent", and never report success unless the mirror serves the
exact source digest.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import mirror_minio_image as mod

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
RELEASE = "RELEASE.2026-09-22T19-25-18Z"
DEST = f"{mod.MIRROR_REPO}:{RELEASE}"
VERSION_OUT = (
    f"minio version {RELEASE} (commit-id=0123abc)\n"
    "Runtime: go1.25 linux/amd64\nLicense: GNU AGPLv3\n"
)


def _done(cmd: list[str], rc: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)


class FakeRegistry:
    """Answers the crane/docker calls the script makes; records them."""

    def __init__(self, mirror: str | None = None, digest_err: str = "") -> None:
        self.mirror = mirror
        self.digest_err = digest_err
        self.calls: list[list[str]] = []
        self.copy_result: str | None = DIGEST

    def __call__(self, cmd: list[str]):
        self.calls.append(cmd)
        if cmd[:2] == ["crane", "digest"] and cmd[2] == f"{mod.SOURCE_REPO}:latest":
            return _done(cmd, out=DIGEST + "\n")
        if cmd[:2] == ["crane", "digest"]:
            if self.mirror is not None:
                return _done(cmd, out=self.mirror + "\n")
            return _done(cmd, 1, err=self.digest_err or "MANIFEST_UNKNOWN: manifest")
        if cmd[:2] == ["crane", "copy"]:
            self.mirror = self.copy_result
            return _done(cmd)
        if cmd[:2] == ["docker", "run"]:
            return _done(cmd, out=VERSION_OUT)
        raise AssertionError(f"unexpected command {cmd}")

    @property
    def copies(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["crane", "copy"]]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    fake = FakeRegistry()
    monkeypatch.setattr(mod, "run", fake)
    return fake


class TestParseReleaseTag:
    def test_reads_the_tag_from_version_output(self) -> None:
        assert mod.parse_release_tag(VERSION_OUT) == RELEASE

    def test_repeated_same_tag_is_one_tag(self) -> None:
        assert mod.parse_release_tag(f"{RELEASE} {RELEASE}") == RELEASE

    @pytest.mark.parametrize(
        "text",
        [
            "minio version DEVELOPMENT.GOGET",
            f"{RELEASE} RELEASE.2026-01-01T00-00-00Z",
        ],
    )
    def test_none_or_ambiguous_is_refused(self, text: str) -> None:
        with pytest.raises(mod.MirrorError, match="exactly one"):
            mod.parse_release_tag(text)


class TestValidateDigest:
    @pytest.mark.parametrize(
        "bad", ["", "latest", "sha256:abc", "sha256:" + "A" * 64, "sha512:" + "a" * 64]
    )
    def test_rejects_non_digests(self, bad: str) -> None:
        with pytest.raises(mod.MirrorError, match="not an image digest"):
            mod.validate_digest(bad)

    def test_strips_whitespace(self) -> None:
        assert mod.validate_digest(f"  {DIGEST}\n") == DIGEST


class TestPlan:
    def test_absent_tag_is_copied(self) -> None:
        p = mod.plan(DIGEST, RELEASE, None)
        assert p.action is mod.Action.COPY
        assert p.source == f"{mod.SOURCE_REPO}@{DIGEST}"
        assert p.pinned_ref == f"{DEST}@{DIGEST}"

    def test_same_digest_is_a_noop(self) -> None:
        assert mod.plan(DIGEST, RELEASE, DIGEST).action is mod.Action.ALREADY_MIRRORED

    def test_different_digest_is_refused(self) -> None:
        with pytest.raises(mod.MirrorError, match="immutable"):
            mod.plan(DIGEST, RELEASE, OTHER)


class TestMirrorDigestOf:
    def test_auth_failure_is_not_absence(self, registry: FakeRegistry) -> None:
        registry.digest_err = "DENIED: permission_denied: read_package"
        with pytest.raises(mod.MirrorError, match="Manage Actions access"):
            mod.mirror_digest_of(DEST)

    def test_manifest_unknown_is_absence(self, registry: FakeRegistry) -> None:
        assert mod.mirror_digest_of(DEST) is None


class TestMain:
    def test_copies_latest_and_writes_outputs(
        self,
        registry: FakeRegistry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        out, summ = tmp_path / "out", tmp_path / "summary"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))

        assert mod.main([]) == 0

        assert registry.copies == [
            ["crane", "copy", f"{mod.SOURCE_REPO}@{DIGEST}", DEST]
        ]
        assert out.read_text() == f"image={DEST}@{DIGEST}\naction=copy\n"
        assert f"{DEST}@{DIGEST}" in summ.read_text()

    def test_explicit_digest_skips_latest(self, registry: FakeRegistry) -> None:
        assert mod.main(["--source-digest", DIGEST]) == 0
        assert ["crane", "digest", f"{mod.SOURCE_REPO}:latest"] not in registry.calls

    def test_dry_run_does_not_copy(self, registry: FakeRegistry) -> None:
        assert mod.main(["--dry-run", "true"]) == 0
        assert registry.copies == []

    def test_already_mirrored_does_not_copy(self, registry: FakeRegistry) -> None:
        registry.mirror = DIGEST
        assert mod.main([]) == 0
        assert registry.copies == []

    def test_repoint_is_refused_without_copy(
        self, registry: FakeRegistry, capsys: pytest.CaptureFixture[str]
    ) -> None:
        registry.mirror = OTHER
        assert mod.main([]) == 1
        assert registry.copies == []
        assert "::error::" in capsys.readouterr().err

    def test_copy_that_changes_the_digest_fails(self, registry: FakeRegistry) -> None:
        registry.copy_result = OTHER
        assert mod.main([]) == 1
