"""Tests for .github/scripts/docker_registry_mirror.py (FND-3622).

``systemctl``/``docker`` are stubbed through the script's ``run`` seam; the
daemon.json handling runs for real against ``tmp_path``. The wiring tests pin
the step order in tests-reusable.yaml, which is where the mirror can be lost.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

_MODULE_PATH = Path(__file__).resolve().parents[1] / "docker_registry_mirror.py"
_spec = importlib.util.spec_from_file_location("docker_registry_mirror", _MODULE_PATH)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_WORKFLOW = (
    Path(__file__).resolve().parents[3] / ".github/workflows/tests-reusable.yaml"
)
MIRROR = "https://mirror.gcr.io"


class FakeDocker:
    """Stands in for systemctl + docker; the daemon reads daemon.json on restart."""

    def __init__(self, daemon_json: Path, *, starts_with_mirror: bool = True) -> None:
        self.daemon_json = daemon_json
        self.starts_with_mirror = starts_with_mirror
        self.up = True
        self.restarts = 0
        self.mirrors: list[str] = []
        self.timeouts: list[float] = []
        # Simulates `systemctl restart` hanging until killed at its timeout.
        self.restart_hangs = False
        # Called with the config the daemon is restarting on; return False to
        # simulate a daemon that refuses to start.
        self.accepts = lambda config: True

    def __call__(
        self, cmd: list[str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        self.timeouts.append(timeout_s)
        if cmd[:2] == ["systemctl", "restart"]:
            self.restarts += 1
            if self.restart_hangs:
                self.up = False
                return subprocess.CompletedProcess(cmd, mod.TIMED_OUT, "", "timed out")
            config = (
                json.loads(self.daemon_json.read_text())
                if self.daemon_json.exists()
                else {}
            )
            self.up = self.accepts(config)
            self.mirrors = (
                config.get("registry-mirrors", []) if self.starts_with_mirror else []
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["docker", "info"]:
            if not self.up:
                return subprocess.CompletedProcess(cmd, 1, "", "Cannot connect")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.mirrors), "")
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def daemon_json(tmp_path: Path) -> Path:
    return tmp_path / "docker" / "daemon.json"


@pytest.fixture
def docker(daemon_json: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker(daemon_json)
    monkeypatch.setattr(mod, "run", fake)
    return fake


TIMEOUT_S = 5.0


def _configure(daemon_json: Path) -> int:
    return mod.main(
        [
            "--mirror",
            MIRROR,
            "--daemon-json",
            str(daemon_json),
            "--timeout-seconds",
            str(TIMEOUT_S),
        ]
    )


def test_creates_daemon_json_when_absent(
    daemon_json: Path, docker: FakeDocker, capsys
) -> None:
    assert _configure(daemon_json) == 0
    assert json.loads(daemon_json.read_text()) == {"registry-mirrors": [MIRROR]}
    assert docker.restarts == 1
    assert "now try https://mirror.gcr.io first" in capsys.readouterr().out


def test_merges_and_keeps_resubnet_keys(daemon_json: Path, docker: FakeDocker) -> None:
    # The shape globalprotect-connect's resubnet-docker writes.
    existing = {
        "bip": "192.168.49.1/24",
        "default-address-pools": [{"base": "192.168.128.0/17", "size": 24}],
        "registry-mirrors": ["https://other.example"],
    }
    daemon_json.parent.mkdir(parents=True)
    daemon_json.write_text(json.dumps(existing))
    assert _configure(daemon_json) == 0
    assert json.loads(daemon_json.read_text()) == {
        **existing,
        "registry-mirrors": [MIRROR, "https://other.example"],
    }


def test_already_configured_does_not_restart(
    daemon_json: Path, docker: FakeDocker
) -> None:
    daemon_json.parent.mkdir(parents=True)
    daemon_json.write_text(json.dumps({"registry-mirrors": [MIRROR]}))
    assert _configure(daemon_json) == 0
    assert docker.restarts == 0


@pytest.mark.parametrize(
    "content", ["{not json", "[1, 2]", '{"registry-mirrors": "https://x"}']
)
def test_unmergeable_config_is_left_alone(
    daemon_json: Path, docker: FakeDocker, capsys, content: str
) -> None:
    daemon_json.parent.mkdir(parents=True)
    daemon_json.write_text(content)
    assert _configure(daemon_json) == 0
    assert daemon_json.read_text() == content
    assert docker.restarts == 0
    assert "::warning::" in capsys.readouterr().out


def test_empty_file_is_treated_as_absent(daemon_json: Path, docker: FakeDocker) -> None:
    daemon_json.parent.mkdir(parents=True)
    daemon_json.write_text("  \n")
    assert _configure(daemon_json) == 0
    assert json.loads(daemon_json.read_text()) == {"registry-mirrors": [MIRROR]}


def test_rolls_back_when_docker_rejects_new_config(
    daemon_json: Path, docker: FakeDocker, capsys
) -> None:
    original = '{"bip": "192.168.49.1/24"}'
    daemon_json.parent.mkdir(parents=True)
    daemon_json.write_text(original)
    docker.accepts = lambda config: "registry-mirrors" not in config
    assert _configure(daemon_json) == 0
    assert daemon_json.read_text() == original
    assert docker.restarts == 2
    assert docker.up
    assert "restored the previous daemon.json" in capsys.readouterr().out


def test_rollback_removes_file_it_created(
    daemon_json: Path, docker: FakeDocker
) -> None:
    docker.accepts = lambda config: "registry-mirrors" not in config
    assert _configure(daemon_json) == 0
    assert not daemon_json.exists()


def test_fails_when_docker_stays_down(
    daemon_json: Path, docker: FakeDocker, capsys
) -> None:
    docker.accepts = lambda config: False
    assert _configure(daemon_json) == 1
    assert "::error::Docker is not running" in capsys.readouterr().out


def test_warns_when_daemon_ignores_mirror(
    daemon_json: Path, daemon_json_docker_without_mirror: FakeDocker, capsys
) -> None:
    assert _configure(daemon_json) == 0
    assert "does not report https://mirror.gcr.io" in capsys.readouterr().out


@pytest.fixture
def daemon_json_docker_without_mirror(
    daemon_json: Path, monkeypatch: pytest.MonkeyPatch
) -> FakeDocker:
    fake = FakeDocker(daemon_json, starts_with_mirror=False)
    monkeypatch.setattr(mod, "run", fake)
    return fake


def test_invalid_utf8_config_is_left_alone(
    daemon_json: Path, docker: FakeDocker, capsys
) -> None:
    content = b'{"bip": "\xff"}'
    daemon_json.parent.mkdir(parents=True)
    daemon_json.write_bytes(content)
    assert _configure(daemon_json) == 0
    assert daemon_json.read_bytes() == content
    assert docker.restarts == 0
    assert "not valid UTF-8" in capsys.readouterr().out


def test_hung_restart_still_reaches_rollback(
    daemon_json: Path, docker: FakeDocker, capsys
) -> None:
    original = b'{"bip": "192.168.49.1/24"}'
    daemon_json.parent.mkdir(parents=True)
    daemon_json.write_bytes(original)
    docker.restart_hangs = True
    assert _configure(daemon_json) == 1
    assert daemon_json.read_bytes() == original
    assert docker.restarts == 2
    assert "::error::Docker is not running" in capsys.readouterr().out


def test_every_command_is_bounded_by_the_budget(
    daemon_json: Path, docker: FakeDocker
) -> None:
    docker.accepts = lambda config: False
    _configure(daemon_json)
    assert docker.timeouts
    assert all(0 < t <= TIMEOUT_S for t in docker.timeouts)


def test_run_kills_a_command_at_its_timeout() -> None:
    result = mod.run(["sleep", "30"], 0.2)
    assert result.returncode == mod.TIMED_OUT
    assert "timed out" in result.stderr


def test_rejects_non_https_mirror(daemon_json: Path) -> None:
    with pytest.raises(SystemExit):
        mod.main(
            ["--mirror", "http://mirror.gcr.io", "--daemon-json", str(daemon_json)]
        )


# ── wiring in tests-reusable.yaml ─────────────────────────────────────────


@pytest.fixture(scope="module")
def integration_steps() -> list[dict]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["integration"]["steps"]


def _index(steps: list[dict], predicate) -> int:
    matches = [i for i, s in enumerate(steps) if predicate(s)]
    assert len(matches) == 1, matches
    return matches[0]


def test_mirror_step_runs_between_vpn_and_tests(integration_steps: list[dict]) -> None:
    mirror = _index(
        integration_steps,
        lambda s: "docker_registry_mirror.py" in str(s.get("run", "")),
    )
    # resubnet-docker overwrites daemon.json; running before it loses the mirror.
    vpn = _index(
        integration_steps, lambda s: "globalprotect-connect" in str(s.get("uses", ""))
    )
    tests = _index(
        integration_steps,
        lambda s: "connector-integration-tests" in str(s.get("uses", "")),
    )
    assert vpn < mirror < tests
    step = integration_steps[mirror]
    assert "if" not in step
    assert "--mirror https://mirror.gcr.io" in " ".join(step["run"].split())


def test_scripts_checkout_is_unconditional(integration_steps: list[dict]) -> None:
    checkout = _index(
        integration_steps,
        lambda s: s.get("with", {}).get("path") == "application-sdk-scripts",
    )
    mirror = _index(
        integration_steps,
        lambda s: "docker_registry_mirror.py" in str(s.get("run", "")),
    )
    assert "if" not in integration_steps[checkout]
    assert checkout < mirror
