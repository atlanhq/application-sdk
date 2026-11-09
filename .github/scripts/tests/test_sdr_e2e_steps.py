"""Tests for .github/actions/sdr-e2e/sdr_steps.py.

The module lives beside the sdr-e2e composite action (it runs from the action's
/tmp stash after setup-deps wipes the workspace); the test stays here so
``scripts-tests.yaml`` picks it up, importing by explicit path like
test_select_dapr_components.py. External commands go through ``_run``, which
each test stubs.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "actions" / "sdr-e2e"))

# .github/actions/sdr-e2e is not a package (the hyphen), so pyright cannot
# resolve a plain import of it; load it by name instead.
sdr_steps = importlib.import_module("sdr_steps")


@pytest.fixture
def gh_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    files = {}
    for var in ("GITHUB_OUTPUT", "GITHUB_ENV"):
        path = tmp_path / var.lower()
        path.write_text("")
        monkeypatch.setenv(var, str(path))
        files[var] = path
    return files


class _Calls:
    def __init__(
        self, results: list[subprocess.CompletedProcess[str]] | None = None
    ) -> None:
        self.argv: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self._results = list(results or [])

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.argv.append(argv)
        self.kwargs.append(kwargs)
        if self._results:
            return self._results.pop(0)
        return subprocess.CompletedProcess(argv, 0, "", "")


def _stub(monkeypatch: pytest.MonkeyPatch, *results) -> _Calls:
    calls = _Calls(
        [subprocess.CompletedProcess([], rc, out, err) for rc, out, err in results]
    )
    monkeypatch.setattr(sdr_steps, "_run", calls)
    return calls


# --- image-ref -------------------------------------------------------------


def test_image_ref_prefers_the_prebuilt_image(
    gh_files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert sdr_steps.main(["image-ref", "--prebuilt", "pre:1", "--built", "b:2"]) == 0
    assert gh_files["GITHUB_OUTPUT"].read_text() == "ref=pre:1\n"
    assert "Using image: pre:1" in capsys.readouterr().out


def test_image_ref_falls_back_to_the_built_image(gh_files: dict[str, Path]) -> None:
    assert sdr_steps.main(["image-ref", "--prebuilt", "", "--built", "b:2"]) == 0
    assert gh_files["GITHUB_OUTPUT"].read_text() == "ref=b:2\n"


def test_image_ref_fails_when_both_are_empty(
    gh_files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert sdr_steps.main(["image-ref"]) == 1
    assert "::error::No image reference resolved" in capsys.readouterr().out
    assert gh_files["GITHUB_OUTPUT"].read_text() == ""


# --- action-root -------------------------------------------------------------


def test_action_root_uses_an_intact_action_path(
    tmp_path: Path, gh_files: dict[str, Path]
) -> None:
    action = tmp_path / "action"
    action.mkdir()
    (action / "test-config.yaml.tmpl").write_text("x")
    sdr_steps.main(["action-root", "--action-path", str(action), "--stash", "/stash"])
    assert gh_files["GITHUB_OUTPUT"].read_text() == f"path={action}\n"


@pytest.mark.parametrize("make_dir", [False, True], ids=["wiped", "no-template"])
def test_action_root_falls_back_to_the_stash(
    tmp_path: Path, gh_files: dict[str, Path], make_dir: bool
) -> None:
    action = tmp_path / "action"
    if make_dir:
        action.mkdir()
    sdr_steps.main(["action-root", "--action-path", str(action), "--stash", "/stash"])
    assert gh_files["GITHUB_OUTPUT"].read_text() == "path=/stash\n"


# --- test-config -------------------------------------------------------------


@pytest.mark.parametrize("app_override", [True, False])
def test_test_config_picks_the_app_template_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    app_override: bool,
) -> None:
    config_dir = tmp_path / "sdr"
    config_dir.mkdir()
    sdk_tmpl = tmp_path / "sdk.tmpl"
    sdk_tmpl.write_text("sdk")
    if app_override:
        (config_dir / "test-config.yaml.tmpl").write_text("app")
    calls = _stub(monkeypatch)
    out = tmp_path / "test-config.yaml"
    rc = sdr_steps.main(
        [
            "test-config",
            "--sdr-config-dir",
            str(config_dir),
            "--sdk-template",
            str(sdk_tmpl),
            "--output",
            str(out),
        ]
    )
    assert rc == 0
    assert calls.argv == [["envsubst"]]
    expected = "app" if app_override else "sdk"
    assert calls.kwargs[0]["stdin"].name.endswith(
        "test-config.yaml.tmpl" if app_override else "sdk.tmpl"
    )
    assert calls.kwargs[0]["stdout"].name == str(out)
    assert (
        "app-level" if expected == "app" else "SDK default"
    ) in capsys.readouterr().out


def test_test_config_propagates_an_envsubst_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk_tmpl = tmp_path / "sdk.tmpl"
    sdk_tmpl.write_text("sdk")
    _stub(monkeypatch, (3, "", ""))
    rc = sdr_steps.main(
        [
            "test-config",
            "--sdr-config-dir",
            str(tmp_path / "none"),
            "--sdk-template",
            str(sdk_tmpl),
            "--output",
            str(tmp_path / "o"),
        ]
    )
    assert rc == 3


# --- secrets-bundle ----------------------------------------------------------


def _secrets(tmp_path: Path, script: str) -> int:
    return sdr_steps.main(
        [
            "secrets-bundle",
            "--sdr-config-dir",
            str(tmp_path / "sdr"),
            "--script",
            script,
        ]
    )


@pytest.mark.parametrize("script", ["", "missing.sh"])
def test_secrets_bundle_requires_the_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script: str,
) -> None:
    calls = _stub(monkeypatch)
    assert _secrets(tmp_path, script) == 1
    assert "::error::secrets-script not found" in capsys.readouterr().out
    assert calls.argv == []
    assert (tmp_path / "sdr/secrets").is_dir()


@pytest.mark.parametrize(
    ("name", "interpreter"),
    [("make.py", "python3"), ("make.sh", "bash"), ("make", "bash")],
)
def test_secrets_bundle_runs_by_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, interpreter: str
) -> None:
    script = tmp_path / name
    script.write_text("")
    calls = _stub(monkeypatch)
    (tmp_path / "sdr/secrets").mkdir(parents=True)
    (tmp_path / "sdr/secrets/credentials.json").write_text("{}")
    assert _secrets(tmp_path, str(script)) == 0
    assert calls.argv == [[interpreter, str(script)]]


def test_secrets_bundle_propagates_the_script_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "make.sh"
    script.write_text("")
    _stub(monkeypatch, (4, "", ""))
    assert _secrets(tmp_path, str(script)) == 4


def test_secrets_bundle_requires_credentials_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "make.sh"
    script.write_text("")
    _stub(monkeypatch)
    assert _secrets(tmp_path, str(script)) == 1
    assert "did not produce" in capsys.readouterr().out


# --- resolve-hosts -----------------------------------------------------------


def test_resolve_hosts_requires_the_tenant(
    gh_files: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert sdr_steps.main(["resolve-hosts", "--tenant", ""]) == 1
    assert "::error::SDR_TEST_TENANT is not set" in capsys.readouterr().out


def test_resolve_hosts_exports_both_addresses(
    gh_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(monkeypatch, (0, "1.1.1.1\n9.9.9.9\n", ""), (0, "2.2.2.2\n", ""))
    assert sdr_steps.main(["resolve-hosts", "--tenant", "acme.atlan.com"]) == 0
    assert calls.argv == [
        ["dig", "+short", "A", "acme.atlan.com"],
        ["dig", "+short", "A", "acme-temporal.atlan.com"],
    ]
    assert gh_files["GITHUB_ENV"].read_text().splitlines() == [
        "AUTH_HOST=acme.atlan.com",
        "AUTH_IPV4=1.1.1.1",
        "TEMPORAL_HOST=acme-temporal.atlan.com",
        "TEMPORAL_IPV4=2.2.2.2",
    ]


def test_resolve_hosts_suffix_is_only_stripped_when_present(
    gh_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub(monkeypatch, (0, "1.1.1.1\n", ""), (0, "2.2.2.2\n", ""))
    sdr_steps.main(["resolve-hosts", "--tenant", "acme.example.com"])
    assert calls.argv[1][-1] == "acme.example.com-temporal.atlan.com"


@pytest.mark.parametrize("empty", [0, 1], ids=["auth", "temporal"])
def test_resolve_hosts_fails_on_an_empty_answer(
    gh_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    empty: int,
) -> None:
    answers = [(0, "1.1.1.1\n", ""), (0, "2.2.2.2\n", "")]
    answers[empty] = (0, "", "")
    _stub(monkeypatch, *answers)
    assert sdr_steps.main(["resolve-hosts", "--tenant", "acme.atlan.com"]) == 1
    assert "::error::Failed to resolve TEMPORAL_HOST=acme-temporal.atlan.com" in (
        capsys.readouterr().out
    )
    assert gh_files["GITHUB_ENV"].read_text() == ""


def test_resolve_hosts_propagates_a_dig_failure(
    gh_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, (9, "", ";; no servers could be reached"))
    assert sdr_steps.main(["resolve-hosts", "--tenant", "acme.atlan.com"]) == 9
    assert gh_files["GITHUB_ENV"].read_text() == ""


# --- wait-ready --------------------------------------------------------------


def test_wait_ready_returns_once_healthy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub(monkeypatch, (7, "", ""), (7, "", ""), (0, "", ""))
    assert (
        sdr_steps.main(["wait-ready", "--timeout", "5", "--compose-files", "-f a"]) == 0
    )
    assert len(calls.argv) == 3
    assert calls.argv[0] == ["curl", "-sf", sdr_steps.HEALTH_URL]
    assert "ready after 3s" in capsys.readouterr().out


def test_wait_ready_dumps_logs_and_fails_at_the_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub(monkeypatch, *[(7, "", "")] * 3)
    rc = sdr_steps.main(
        ["wait-ready", "--timeout", "3", "--compose-files", "-f a.yaml -f b.yaml"]
    )
    assert rc == 1
    assert calls.argv[-1] == [
        "docker",
        "compose",
        "-f",
        "a.yaml",
        "-f",
        "b.yaml",
        "logs",
    ]
    assert len(calls.argv) == 4
    assert "::error::SDR container failed to start within 3s" in capsys.readouterr().out


def test_wait_ready_with_zero_timeout_never_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`seq 1 0` is empty, so the shell loop never ran and the step passed."""
    calls = _stub(monkeypatch)
    assert sdr_steps.main(["wait-ready", "--timeout", "0"]) == 0
    assert calls.argv == []


# --- restore-assets ----------------------------------------------------------


def test_restore_assets_copies_the_stash_into_a_wiped_path(tmp_path: Path) -> None:
    stash = tmp_path / "stash"
    (stash / "components").mkdir(parents=True)
    (stash / "action.yaml").write_text("name: x")
    (stash / "components/a.yaml").write_text("a")
    action = tmp_path / "ws/action"
    rc = sdr_steps.main(
        ["restore-assets", "--action-path", str(action), "--stash", str(stash)]
    )
    assert rc == 0
    assert (action / "action.yaml").read_text() == "name: x"
    assert (action / "components/a.yaml").read_text() == "a"


def test_restore_assets_leaves_an_intact_path_alone(tmp_path: Path) -> None:
    stash = tmp_path / "stash"
    stash.mkdir()
    (stash / "action.yaml").write_text("stash")
    action = tmp_path / "action"
    action.mkdir()
    (action / "action.yaml").write_text("live")
    sdr_steps.main(
        ["restore-assets", "--action-path", str(action), "--stash", str(stash)]
    )
    assert (action / "action.yaml").read_text() == "live"


def test_restore_assets_without_a_stash_is_a_no_op(tmp_path: Path) -> None:
    action = tmp_path / "action"
    rc = sdr_steps.main(
        [
            "restore-assets",
            "--action-path",
            str(action),
            "--stash",
            str(tmp_path / "none"),
        ]
    )
    assert rc == 0
    assert not action.exists()


# --- wiring ------------------------------------------------------------------


def test_every_subcommand_is_invoked_by_the_action() -> None:
    action = (
        Path(__file__).parents[2] / "actions" / "sdr-e2e" / "action.yaml"
    ).read_text(encoding="utf-8")
    for command in (
        "image-ref",
        "action-root",
        "test-config",
        "secrets-bundle",
        "resolve-hosts",
        "wait-ready",
        "restore-assets",
    ):
        assert (
            f'sdr_steps.py" {command}' in action or f"sdr_steps.py {command}" in action
        )
