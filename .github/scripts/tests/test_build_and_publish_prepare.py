"""Tests for .github/scripts/build_and_publish/prepare.py and manifest.py.

manifest.py consumes the ``release_tags`` output prepare.py writes, so both
are covered here.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "build_and_publish"))

import _bap  # noqa: E402
import manifest  # noqa: E402
import prepare  # noqa: E402

SHA = "abcdef0123456789abcdef0123456789abcdef01"


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    files = {"output": tmp_path / "out", "summary": tmp_path / "summary"}
    for f in files.values():
        f.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(files["output"]))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(files["summary"]))
    return files


def _outputs(path: Path) -> dict[str, str]:
    """Parse GITHUB_OUTPUT, including ``key<<DELIM`` multiline values."""
    out: dict[str, str] = {}
    lines = iter(path.read_text().split("\n"))
    for line in lines:
        if not line:
            continue
        if match := re.fullmatch(r"(\w+)<<(\w+)", line):
            key, delim, value = match.group(1), match.group(2), []
            for inner in lines:
                if inner == delim:
                    break
                value.append(inner)
            out[key] = "\n".join(value)
        else:
            key, _, value = line.partition("=")
            out[key] = value
    return out


# ── secrets ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "flag", "message"),
    [
        ("sdr-secrets", "HAS_PAT", "DOCKER_HUB_PAT_RW secret is not set"),
        ("publish-secrets", "HAS_TOKEN", "APP_PUBLISH_TOKEN secret is not set"),
    ],
)
@pytest.mark.parametrize(("value", "expected"), [("true", 0), ("false", 1), ("", 1)])
def test_required_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    flag: str,
    message: str,
    value: str,
    expected: int,
) -> None:
    monkeypatch.setenv(flag, value)
    assert prepare.main([command]) == expected
    out = capsys.readouterr().out
    assert ("::error::" in out and message in out) is (expected == 1)


# ── app-configs ───────────────────────────────────────────────────────────────


def test_app_configs_bundles_every_generated_json(
    tmp_path: Path, gh: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    gen = tmp_path / "generated"
    (gen / "nested").mkdir(parents=True)
    (gen / "b.json").write_text('{"b": 1}')
    (gen / "nested" / "a.json").write_text('{"a": "<x>"}')
    (gen / "ignored.yaml").write_text("x: 1")
    assert prepare.app_configs(str(gen)) == 0
    value = _outputs(gh["output"])["app_configs"]
    assert json.loads(base64.b64decode(value)) == {
        "b.json": {"b": 1},
        "a.json": {"a": "<x>"},
    }
    assert (
        "[app_configs] found 2 files: ['b.json', 'a.json']" in capsys.readouterr().out
    )


def test_app_configs_without_generated_dir_is_empty(
    tmp_path: Path, gh: dict[str, Path]
) -> None:
    assert prepare.app_configs(str(tmp_path / "missing")) == 0
    assert gh["output"].read_text() == "app_configs=\n"


def test_app_configs_invalid_json_blocks(
    tmp_path: Path, gh: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "bad.json").write_text("{")
    assert prepare.app_configs(str(tmp_path)) == 1
    assert f"::error file={tmp_path / 'bad.json'}::Invalid JSON in connector form:" in (
        capsys.readouterr().out
    )
    assert gh["output"].read_text() == ""


# ── compute-tags ──────────────────────────────────────────────────────────────


def _tags(
    monkeypatch: pytest.MonkeyPatch, ref: str, release_tag: str = "", rc: int = 0
) -> int:
    monkeypatch.setenv("REPO", "demo-app")
    monkeypatch.setenv("REF", ref)
    monkeypatch.setenv("RELEASE_TAG", release_tag)

    def fake(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert cmd == ["git", "rev-parse", "HEAD"]
        return subprocess.CompletedProcess(cmd, rc, f"{SHA}\n", "")

    monkeypatch.setattr(_bap, "run", fake)
    return prepare.main(["compute-tags"])


def test_branch_build_tags(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _tags(monkeypatch, "refs/heads/Feat/My_Branch") == 0
    assert _outputs(gh["output"]) == {
        "branch": "feat-my-branch",
        "image_tag": "abcdef0",
        "ghcr_base": "ghcr.io/atlanhq/demo-app",
        "ghcr_image": "ghcr.io/atlanhq/demo-app:feat-my-branch-abcdef0",
        "ghcr_branch_tag": "ghcr.io/atlanhq/demo-app:feat-my-branch",
        "dockerhub_image": "atlanhq/demo-app:feat-my-branch-abcdef0",
        "release_tags": "",
        "gm_version": "abcdef0",
        "commit_sha": SHA,
    }


def test_release_tags_output_uses_a_random_delimiter(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _tags(monkeypatch, "main")
    assert re.search(
        r"^release_tags<<([0-9a-f]{16})\n\n\1\n", gh["output"].read_text(), re.M
    )


def test_stable_release_gets_the_full_ladder(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _tags(monkeypatch, "refs/tags/v2.0.1", "v2.0.1\r\n") == 0
    out = _outputs(gh["output"])
    assert out["branch"] == "main"
    assert out["ghcr_image"] == "ghcr.io/atlanhq/demo-app:main-abcdef0"
    assert out["gm_version"] == "v2.0.1"
    base = "ghcr.io/atlanhq/demo-app"
    assert out["release_tags"].split("\n") == [
        f"{base}:latest",
        f"{base}:2.0.1",
        f"{base}:2.0",
        f"{base}:2",
        f"{base}:sha-abcdef0",
    ]
    assert f"Release tags:\n{base}:latest\n" in capsys.readouterr().out


def test_prerelease_gets_version_only(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _tags(monkeypatch, "refs/tags/1.0.0-rc.1", "1.0.0-rc.1") == 0
    base = "ghcr.io/atlanhq/demo-app"
    assert _outputs(gh["output"])["release_tags"].split("\n") == [
        f"{base}:1.0.0-rc.1",
        f"{base}:sha-abcdef0",
    ]


def test_release_tag_on_another_ref_keeps_the_branch_slug(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _tags(monkeypatch, "refs/heads/feat", "v1.0.0") == 0
    out = _outputs(gh["output"])
    assert out["branch"] == "feat"
    assert out["gm_version"] == "v1.0.0"


def test_tag_ref_without_release_tag_is_slugged(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _tags(monkeypatch, "refs/tags/v1.0.0") == 0
    assert _outputs(gh["output"])["branch"] == "refs-tags-v1.0.0"


@pytest.mark.parametrize(
    "ref", ["refs/heads/feat$(x)", "feat branch", "", "refs/heads/a`b`"]
)
def test_invalid_ref_is_rejected(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref: str,
) -> None:
    assert _tags(monkeypatch, ref) == 1
    assert f"::error::Invalid ref format: {ref}" in capsys.readouterr().out
    assert gh["output"].read_text() == ""


@pytest.mark.parametrize("tag", ["1.0", "v1.0.0.0", "release-1", "v1.0.0+build"])
def test_invalid_release_tag_is_rejected(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tag: str,
) -> None:
    assert _tags(monkeypatch, "main", tag) == 1
    assert f"::error::Invalid release_tag format: '{tag}'" in capsys.readouterr().out
    # The image tags were already written, as the inline step did.
    assert "release_tags" not in _outputs(gh["output"])


def test_git_failure_ends_the_step(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _tags(monkeypatch, "main", rc=128) == 128
    assert gh["output"].read_text() == ""


# ── manifest ──────────────────────────────────────────────────────────────────


def _manifest(
    monkeypatch: pytest.MonkeyPatch, release_tags: str, rc: int = 0
) -> tuple[int, list[list[str]]]:
    env = {
        "GHCR_BASE": "ghcr.io/atlanhq/demo-app",
        "GHCR_IMAGE": "ghcr.io/atlanhq/demo-app:main-abc1234",
        "GHCR_BRANCH_TAG": "ghcr.io/atlanhq/demo-app:main",
        "BRANCH": "main",
        "IMAGE_TAG": "abc1234",
        "RELEASE_TAGS": release_tags,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    calls: list[list[str]] = []

    def fake(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, rc, "", "")

    monkeypatch.setattr(_bap, "run", fake)
    return manifest.create(), calls


def test_branch_manifest_carries_the_two_branch_tags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, calls = _manifest(monkeypatch, "")
    assert rc == 0
    assert calls == [
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--tag",
            "ghcr.io/atlanhq/demo-app:main-abc1234",
            "--tag",
            "ghcr.io/atlanhq/demo-app:main",
            "ghcr.io/atlanhq/demo-app:main-abc1234-amd64",
            "ghcr.io/atlanhq/demo-app:main-abc1234-arm64",
        ]
    ]
    assert "Release tags" not in capsys.readouterr().out


def test_release_manifest_adds_every_ladder_tag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, calls = _manifest(monkeypatch, "ghcr.io/x:latest\n\nghcr.io/x:1.0.0\n")
    assert rc == 0
    tags = [calls[0][i + 1] for i, a in enumerate(calls[0]) if a == "--tag"]
    assert tags[2:] == ["ghcr.io/x:latest", "ghcr.io/x:1.0.0"]
    assert (
        "  Release tags:\n    ghcr.io/x:latest\n    ghcr.io/x:1.0.0\n"
        in capsys.readouterr().out
    )


def test_manifest_failure_fails_the_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _manifest(monkeypatch, "", rc=1)
    assert rc == 1
    assert "Multi-arch manifest pushed" not in capsys.readouterr().out
