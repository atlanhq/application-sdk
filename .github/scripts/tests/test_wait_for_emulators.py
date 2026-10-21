"""Tests for .github/scripts/wait_for_emulators.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import wait_for_emulators as mod


class _FakeClock:
    """Monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _status_from(table: dict[str, list[int | None]]):
    """A status function answering each URL from a queue (last value repeats)."""

    def status(url: str) -> int | None:
        queue = table[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return status


def _probes():
    return {p.name: p for p in mod.PROBES}


def _completed(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout="", stderr=stderr
    )


# ---------------------------------------------------------------------------
# wait_for
# ---------------------------------------------------------------------------


def test_all_ready_first_poll_returns_empty_without_sleeping():
    clock = _FakeClock()
    p = _probes()
    status = _status_from(
        {
            p["MinIO"].url: [200],
            p["Azurite"].url: [400],
            p["GCS proxy"].url: [200],
        }
    )
    missing = mod.wait_for(
        mod.PROBES, 120, 2, status=status, sleep=clock.sleep, clock=clock
    )
    assert missing == []
    assert clock.sleeps == []


def test_late_emulator_is_waited_for():
    clock = _FakeClock()
    p = _probes()
    status = _status_from(
        {
            p["MinIO"].url: [None, None, 503, 200],
            p["Azurite"].url: [400],
            p["GCS proxy"].url: [200],
        }
    )
    missing = mod.wait_for(
        mod.PROBES, 120, 2, status=status, sleep=clock.sleep, clock=clock
    )
    assert missing == []
    assert clock.sleeps == [2, 2, 2]


def test_dead_emulator_fails_at_the_deadline_and_is_named(capsys):
    clock = _FakeClock()
    p = _probes()
    status = _status_from(
        {
            p["MinIO"].url: [200],
            p["Azurite"].url: [None],
            p["GCS proxy"].url: [200],
        }
    )
    missing = mod.wait_for(
        mod.PROBES, 10, 2, status=status, sleep=clock.sleep, clock=clock
    )
    assert missing == ["Azurite"]
    assert clock.now == 10
    out = capsys.readouterr().out
    assert "::error::Azurite not ready after 10s" in out
    assert "no response" in out


@pytest.mark.parametrize(
    ("name", "code", "ready"),
    [
        ("MinIO", 200, True),
        ("MinIO", 503, False),
        # A reachable Azurite answers the bare account URL with 400.
        ("Azurite", 400, True),
        ("Azurite", 502, False),
        ("GCS proxy", 200, True),
        # nginx is up but the testbench behind it is not.
        ("GCS proxy", 502, False),
    ],
)
def test_readiness_predicates(name, code, ready):
    assert _probes()[name].ready(code) is ready


def test_unhealthy_status_is_reported_with_its_code(capsys):
    clock = _FakeClock()
    p = _probes()
    status = _status_from(
        {
            p["MinIO"].url: [200],
            p["Azurite"].url: [400],
            p["GCS proxy"].url: [502],
        }
    )
    assert mod.wait_for(
        mod.PROBES, 4, 2, status=status, sleep=clock.sleep, clock=clock
    ) == ["GCS proxy"]
    assert "HTTP 502" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# provisioning
# ---------------------------------------------------------------------------


def test_s3_bucket_created(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs["env"]))
        return _completed()

    monkeypatch.setattr(mod, "run", fake_run)
    assert mod.create_s3_bucket("b1") is True
    cmd, env = calls[0]
    assert cmd[:3] == ["aws", "--endpoint-url", mod.MINIO_ENDPOINT]
    assert cmd[-2:] == ["--bucket", "b1"]
    assert env["AWS_ACCESS_KEY_ID"] == "minioadmin"


def test_s3_bucket_already_owned_is_success(monkeypatch):
    monkeypatch.setattr(
        mod,
        "run",
        lambda cmd, **kw: _completed(
            254, "An error occurred (BucketAlreadyOwnedByYou) when calling ..."
        ),
    )
    assert mod.create_s3_bucket("b1") is True


def test_s3_bucket_other_error_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        mod,
        "run",
        lambda cmd, **kw: _completed(255, "Could not connect to the endpoint URL"),
    )
    assert mod.create_s3_bucket("b1") is False
    assert "::error::S3 bucket b1 not created" in capsys.readouterr().out


def test_azure_container_exit_code_is_the_verdict(monkeypatch):
    monkeypatch.setattr(mod, "run", lambda cmd, **kw: _completed(0))
    assert mod.create_azure_container("c") is True
    monkeypatch.setattr(mod, "run", lambda cmd, **kw: _completed(1, "boom"))
    assert mod.create_azure_container("c") is False


@pytest.mark.parametrize(
    ("code", "ok"), [(200, True), (409, True), (500, False), (None, False)]
)
def test_gcs_bucket(code, ok):
    seen = {}

    def request(url, *, method, body):
        seen.update(url=url, method=method, body=body)
        return code

    assert mod.create_gcs_bucket("g", request=request) is ok
    assert seen["method"] == "POST"
    assert b'"name": "g"' in seen["body"]


def test_provision_attempts_every_bucket_after_a_failure(monkeypatch):
    attempted = []

    def fake_run(cmd, **kwargs):
        attempted.append(cmd[cmd.index("--bucket") + 1] if "--bucket" in cmd else "az")
        return _completed(1, "boom")

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(
        mod, "create_gcs_bucket", lambda b: attempted.append("gcs") or True
    )
    assert mod.provision() is False
    assert attempted == [*mod.S3_BUCKETS, "az", "gcs"]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_fails_without_provisioning_when_an_emulator_is_down(monkeypatch):
    monkeypatch.setattr(mod, "wait_for", lambda *a, **k: ["MinIO"])

    def no_provision():
        raise AssertionError("must not provision when an emulator is down")

    monkeypatch.setattr(mod, "provision", no_provision)
    assert mod.main([]) == 1


def test_main_fails_when_provisioning_fails(monkeypatch):
    monkeypatch.setattr(mod, "wait_for", lambda *a, **k: [])
    monkeypatch.setattr(mod, "provision", lambda: False)
    assert mod.main([]) == 1


def test_main_succeeds(monkeypatch):
    monkeypatch.setattr(mod, "wait_for", lambda *a, **k: [])
    monkeypatch.setattr(mod, "provision", lambda: True)
    assert mod.main([]) == 0
