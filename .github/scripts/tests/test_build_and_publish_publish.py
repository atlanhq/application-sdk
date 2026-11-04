"""Tests for .github/scripts/build_and_publish/publish.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "build_and_publish"))

import _bap  # noqa: E402
import publish  # noqa: E402


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    files = {"output": tmp_path / "out", "summary": tmp_path / "summary"}
    for f in files.values():
        f.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(files["output"]))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(files["summary"]))
    return files


# ── channel-gate ──────────────────────────────────────────────────────────────


def _gate(monkeypatch: pytest.MonkeyPatch, **env: str) -> int:
    for key in ("REF", "CHANNEL", "TENANTS", "RELEASE_TAG"):
        monkeypatch.setenv(key, env.get(key, ""))
    return publish.main(["channel-gate"])


@pytest.mark.parametrize("ref", ["main", "refs/heads/main"])
@pytest.mark.parametrize("channel", ["", "all", "ALL", "All"])
def test_channel_all_on_main_proceeds(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch, ref: str, channel: str
) -> None:
    assert _gate(monkeypatch, REF=ref, CHANNEL=channel) == 0
    assert gh["summary"].read_text() == ""


@pytest.mark.parametrize("ref", ["refs/heads/feat/foo", "feat-foo", "refs/tags/v1.0.0"])
@pytest.mark.parametrize("channel", ["", "all", "ALL"])
def test_channel_all_off_main_is_blocked(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref: str,
    channel: str,
) -> None:
    assert _gate(monkeypatch, REF=ref, CHANNEL=channel) == 1
    assert (
        f"::error title=Publish blocked::Channel 'all' requires the main branch — got ref '{ref}'."
        in capsys.readouterr().out
    )
    summary = gh["summary"].read_text()
    assert summary.startswith("### Publish blocked\n\n")
    assert f"| Ref | `{ref}` |\n| Channel | `all` |\n" in summary
    assert summary.endswith("- merge to `main` and let the push-trigger run\n")


def test_matching_release_tag_ref_skips_the_gate(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _gate(monkeypatch, REF="refs/tags/v1.2.3", RELEASE_TAG="v1.2.3\r\n") == 0
    assert (
        "Tagged release (v1.2.3) — main-branch gate skipped." in capsys.readouterr().out
    )


def test_release_tag_alone_does_not_skip_the_gate(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A feature branch passing any release_tag must not dodge the main check.
    assert _gate(monkeypatch, REF="refs/heads/feat", RELEASE_TAG="v1.2.3") == 1


def test_tenant_targeting_skips_the_gate(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _gate(monkeypatch, REF="feat", TENANTS="t1,t2") == 0
    assert (
        "Tenants targeting (t1,t2) — main-branch gate skipped."
        in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    ("channel", "shown"), [("beta", "beta"), ("STAGING", "staging")]
)
def test_non_all_channel_skips_the_gate(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    channel: str,
    shown: str,
) -> None:
    assert _gate(monkeypatch, REF="feat", CHANNEL=channel) == 0
    assert f"Channel '{shown}' — main-branch gate skipped." in capsys.readouterr().out


# ── publish: body ─────────────────────────────────────────────────────────────

BASE = {
    "APP_ID": "app-uuid",
    "IMAGE_TAG": "abc1234",
    "GHCR_IMAGE": "ghcr.io/atlanhq/demo-app:main-abc1234",
    "BRANCH": "main",
    "REPO_URL": "https://github.com/atlanhq/demo-app",
}


def test_minimal_body_targets_all_and_uses_the_sha_tag() -> None:
    assert publish.build_body(dict(BASE)) == {
        "app_id": "app-uuid",
        "image": "ghcr.io/atlanhq/demo-app:main-abc1234",
        "version": "abc1234",
        "branch": "main",
        "repo": "https://github.com/atlanhq/demo-app",
        "source": "ci_publish",
        "target_channel": "all",
    }


def test_gm_version_wins_then_release_tag_then_image_tag() -> None:
    assert publish.build_body({**BASE, "GM_VERSION": "v9"})["version"] == "v9"
    assert publish.build_body({**BASE, "RELEASE_TAG": "v1.0.0"})["version"] == "v1.0.0"


def test_semver_release_sends_the_plain_version_tag_image() -> None:
    body = publish.build_body(
        {**BASE, "RELEASE_TAG": "v0.3.0", "RELEASE_MODEL": "semver"}
    )
    assert body["image"] == "ghcr.io/atlanhq/demo-app:0.3.0"
    assert body["release_model"] == "semver"


def test_cd_release_keeps_the_branch_sha_image() -> None:
    body = publish.build_body({**BASE, "RELEASE_TAG": "v0.3.0"})
    assert body["image"] == BASE["GHCR_IMAGE"]
    assert "release_model" not in body


def test_optional_fields() -> None:
    body = publish.build_body(
        {
            **BASE,
            "COMMIT_SHA": " deadbeef ",
            "DEPLOY_CONFIG": "k: v",
            "ENABLE_SDR": "true",
            "SDK_VERSION": "3.0.0",
            "ENTRYPOINTS": '[{"name": "x"}]',
            "TENANTS": " t1, ,t2 ",
            "CHANNEL": "beta",
            "CREATED_BY": "someone",
            "APP_CONFIGS": "e30=",
        }
    )
    assert body["commit_sha"] == "deadbeef"
    assert body["config"] == "self_deployed_runtime: true\nk: v"
    assert body["sdk_version"] == "3.0.0"
    assert body["entrypoints"] == [{"name": "x"}]
    assert body["allowed_tenants"] == ["t1", "t2"]
    assert "target_channel" not in body
    assert body["created_by"] == "someone"
    assert body["app_configs"] == "e30="


def test_sdr_alone_still_produces_a_config() -> None:
    assert publish.build_body({**BASE, "ENABLE_SDR": "true"})["config"] == (
        "self_deployed_runtime: true"
    )


def test_channel_passes_through_unlowered() -> None:
    assert publish.build_body({**BASE, "CHANNEL": "beta"})["target_channel"] == "beta"


# ── publish: flow ─────────────────────────────────────────────────────────────


class FakeCurl:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode, self.stdout = returncode, stdout
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def _publish(monkeypatch: pytest.MonkeyPatch, curl: FakeCurl, **env: str) -> int:
    for key, value in {
        **BASE,
        "BASE_URL": "https://gm",
        "APP_PUBLISH_TOKEN": "tok",
        "APP_NAME": "demo",
        "CHANNEL": "",
        "TENANTS": "",
        "RELEASE_TAG": "",
        "RELEASE_MODEL": "",
        **env,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(_bap, "run", curl)
    return publish.main(["publish"])


OK = json.dumps({"data": {"version_id": "v1", "release_id": "r1", "app_id": "a1"}})


def test_publish_posts_the_body_and_summarises(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    curl = FakeCurl(stdout=OK + "\n")
    assert _publish(monkeypatch, curl) == 0
    (cmd,) = curl.calls
    assert cmd[:6] == [
        "curl",
        "-sS",
        "--fail-with-body",
        "-X",
        "POST",
        "https://gm/api/service/marketplace/publish",
    ]
    assert "Authorization: Bearer tok" in cmd
    assert json.loads(cmd[cmd.index("-d") + 1])["target_channel"] == "all"
    out = capsys.readouterr().out
    assert "Publishing demo (abc1234) to marketplace..." in out
    assert "Version: v1\nRelease: r1\nApp ID:  a1\n" in out
    assert "Channel 'all' creates a draft release — admin approval required" in out
    assert "Published `demo` version `abc1234` to `all`" in gh["summary"].read_text()


def test_publish_to_tenants_reports_them(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _publish(monkeypatch, FakeCurl(stdout="{}"), TENANTS="t1") == 0
    out = capsys.readouterr().out
    assert "Version: N/A" in out
    assert "draft release" not in out
    assert "to `tenants: t1`" in gh["summary"].read_text()


def test_publish_failure_prints_the_response(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _publish(monkeypatch, FakeCurl(22, '{"error": "nope"}\n')) == 1
    assert 'Publish failed: {"error": "nope"}\n' in capsys.readouterr().out
    assert gh["summary"].read_text() == ""


def test_semver_without_release_tag_to_all_is_blocked(
    gh: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    curl = FakeCurl(stdout=OK)
    assert _publish(monkeypatch, curl, RELEASE_MODEL="semver\r\n", CHANNEL="ALL") == 1
    assert not curl.calls
    assert "::error title=Publish blocked::App declares release_model=semver" in (
        capsys.readouterr().out
    )
    summary = gh["summary"].read_text()
    assert summary.startswith("### Publish blocked — semver release required\n")
    assert "| Channel | `all` |" in summary


@pytest.mark.parametrize(
    "env",
    [
        {"RELEASE_MODEL": "semver", "RELEASE_TAG": "v1.0.0"},
        {"RELEASE_MODEL": "semver", "TENANTS": "t1"},
        {"RELEASE_MODEL": "semver", "CHANNEL": "beta"},
    ],
)
def test_semver_gate_lets_tagged_targeted_and_non_all_through(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    assert _publish(monkeypatch, FakeCurl(stdout=OK), **env) == 0


def test_crlf_in_release_tag_is_stripped_before_the_body(
    gh: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    curl = FakeCurl(stdout=OK)
    _publish(monkeypatch, curl, RELEASE_TAG="v2.0.0\r\n", RELEASE_MODEL="semver")
    body = json.loads(curl.calls[0][curl.calls[0].index("-d") + 1])
    assert body["image"] == "ghcr.io/atlanhq/demo-app:2.0.0"
    assert body["version"] == "v2.0.0"
