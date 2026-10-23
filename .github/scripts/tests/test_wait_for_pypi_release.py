"""Tests for .github/scripts/wait_for_pypi_release.py.

`curl` is stubbed through the module's `run` seam. Each case scripts what the
index returns per probe, so the streak, reset, timeout and settle branches are
all exercised without the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import wait_for_pypi_release as mod

SDK = "atlan-application-sdk"
CONF = "atlan-application-sdk-conformance"


def _simple(name: str, versions: list[str], *, with_files: bool = True) -> str:
    files = (
        [
            {"filename": f"{name.replace('-', '_')}-{v}-py3-none-any.whl"}
            for v in versions
        ]
        if with_files
        else []
    )
    return json.dumps({"name": name, "versions": versions, "files": files})


def _legacy(versions: list[str]) -> str:
    return json.dumps({"releases": {v: [{"filename": "x"}] for v in versions}})


class FakeIndex:
    """Serves per-package version lists; `script` lets a round see a stale list.

    `stale_rounds[name]` is how many leading probes of that package's plain
    (non-busted) Simple URL return the previous release list, modelling a CDN
    edge that lags the origin.
    """

    def __init__(
        self,
        live: dict[str, list[str]],
        stale: dict[str, list[str]] | None = None,
        stale_probes: int = 0,
        fail: bool = False,
    ):
        self.live = live
        self.stale = stale or {}
        self.stale_probes = stale_probes
        self.fail = fail
        self.urls: list[str] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        assert cmd[0] == "curl", cmd
        url = cmd[-1]
        self.urls.append(url)
        if self.fail:
            return subprocess.CompletedProcess(cmd, 22, "", "404")
        name = url.split("/")[4]
        versions = self.live[name]
        plain_simple = "/simple/" in url and "cachebust" not in url
        if plain_simple and name in self.stale and self.stale_probes > 0:
            self.stale_probes -= 1
            versions = self.stale[name]
        body = _simple(name, versions) if "/simple/" in url else _legacy(versions)
        return subprocess.CompletedProcess(cmd, 0, body, "")


def _wait(monkeypatch, index, packages, **kwargs):
    clock = {"t": 0.0}
    sleeps: list[float] = []

    def sleep(s):
        sleeps.append(s)
        clock["t"] += s

    monkeypatch.setattr(mod, "run", index)
    ok = mod.wait_for_release(
        packages,
        sleep=sleep,
        monotonic=lambda: clock["t"],
        nonce=lambda: "n",
        **kwargs,
    )
    return ok, sleeps


def test_visible_after_consecutive_rounds_then_settles(monkeypatch):
    index = FakeIndex({SDK: ["3.51.1", "3.51.2"]})
    ok, sleeps = _wait(
        monkeypatch,
        index,
        {SDK: "3.51.2"},
        consecutive=3,
        interval_seconds=10,
        settle_seconds=60,
    )
    assert ok is True
    # Two intervals between three passing rounds, then the settle buffer.
    assert sleeps == [10, 10, 60]
    # Each round: Simple + legacy JSON, busted + plain.
    assert len(index.urls) == 12
    assert any("cachebust=n" in u for u in index.urls)
    assert any(
        u == "https://pypi.org/simple/atlan-application-sdk/" for u in index.urls
    )
    assert any(
        u == "https://pypi.org/pypi/atlan-application-sdk/json" for u in index.urls
    )


def test_stale_edge_resets_the_streak(monkeypatch):
    # The origin has it (busted probe passes) but the plain probe lags for two
    # rounds: those rounds must not count toward the streak.
    index = FakeIndex(
        {CONF: ["0.51.1", "0.52.0"]},
        stale={CONF: ["0.51.1"]},
        stale_probes=2,
    )
    ok, sleeps = _wait(
        monkeypatch,
        index,
        {CONF: "0.52.0"},
        consecutive=2,
        interval_seconds=10,
        settle_seconds=5,
    )
    assert ok is True
    assert sleeps == [10, 10, 10, 5]


def test_waits_for_every_package(monkeypatch):
    # The SDK is visible, conformance is not: that is not a pass.
    index = FakeIndex({SDK: ["3.51.2"], CONF: ["0.51.1"]})
    ok, _ = _wait(
        monkeypatch,
        index,
        {SDK: "3.51.2", CONF: "0.52.0"},
        timeout_seconds=30,
        interval_seconds=10,
    )
    assert ok is False


def test_listed_version_without_files_is_not_visible():
    payload = json.loads(_simple(SDK, ["3.51.2"], with_files=False))
    assert mod._simple_has(payload, "3.51.2") is False


def test_timeout_warns_and_returns_false(monkeypatch, capsys):
    index = FakeIndex({SDK: ["3.51.1"]}, fail=True)
    ok, sleeps = _wait(
        monkeypatch, index, {SDK: "3.51.2"}, timeout_seconds=30, interval_seconds=10
    )
    assert ok is False
    assert sleeps == [10, 10, 10]
    assert (
        "::warning::atlan-application-sdk==3.51.2 not consistently visible"
        in capsys.readouterr().out
    )


def test_main_reads_pyprojects_writes_output_and_exits_zero_on_timeout(
    monkeypatch, tmp_path
):
    sdk = tmp_path / "sdk.toml"
    sdk.write_text('[project]\nname = "atlan-application-sdk"\nversion = "3.51.2"\n')
    conf = tmp_path / "conf.toml"
    conf.write_text(
        '[project]\nname = "atlan-application-sdk-conformance"\nversion = "0.52.0"\n'
    )
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(mod, "run", FakeIndex({}, fail=True))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    rc = mod.main(
        ["--pyproject", str(sdk), "--pyproject", str(conf), "--timeout-seconds", "0"]
    )

    assert rc == 0
    line = out.read_text().strip()
    assert line.startswith("packages=")
    assert json.loads(line.removeprefix("packages=")) == {
        "atlan-application-sdk": "3.51.2",
        "atlan-application-sdk-conformance": "0.52.0",
    }


def test_a_stale_round_mid_streak_starts_it_over(monkeypatch):
    # pass, stale, pass, pass: with consecutive=2 the stale round must reset
    # the count, so it takes the fourth round, not the third, to pass.
    index = FakeIndex({CONF: ["0.51.1", "0.52.0"]}, stale={CONF: ["0.51.1"]})
    plain_simple_seen = {"n": 0}
    inner = index.__call__

    def run(cmd):
        url = cmd[-1]
        if "/simple/" in url and "cachebust" not in url:
            plain_simple_seen["n"] += 1
            index.stale_probes = 1 if plain_simple_seen["n"] == 2 else 0
        return inner(cmd)

    clock = {"t": 0.0}
    sleeps: list[float] = []
    monkeypatch.setattr(mod, "run", run)
    ok = mod.wait_for_release(
        {CONF: "0.52.0"},
        consecutive=2,
        interval_seconds=10,
        settle_seconds=5,
        sleep=lambda s: (sleeps.append(s), clock.__setitem__("t", clock["t"] + s)),
        monotonic=lambda: clock["t"],
        nonce=lambda: "n",
    )
    assert ok is True
    assert sleeps == [10, 10, 10, 5]
