"""Tests for .github/scripts/create_multiarch_manifest.py.

The join between the per-arch builds and what anyone actually pulls. The
properties worth pinning are the ones whose failure is invisible in a green
run: a tag silently left off the index, or the whole image re-uploaded once per
name because the targets were not grouped.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import create_multiarch_manifest as mod  # noqa: E402

HARBOR = "registry.atlan.com/public/app-runtime-base"
GHCR = "ghcr.io/atlanhq/app-runtime-base"
SOURCES = [f"{GHCR}:sha-abc1234-amd64", f"{GHCR}:sha-abc1234-arm64"]


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list:
    """Capture the docker invocations instead of running them."""
    recorded: list = []
    monkeypatch.setattr(mod, "run", lambda cmd: recorded.append(list(cmd)))
    return recorded


# ── Grouping ─────────────────────────────────────────────────────────────────


def test_one_create_per_repository(calls: list) -> None:
    """Not one per tag. A ladder of five tags across two registries is two
    calls; ten would re-copy the image eight extra times."""
    tags = [f"{HARBOR}:latest", f"{HARBOR}:3", f"{GHCR}:latest", f"{GHCR}:3"]
    mod.create_manifests(tags, SOURCES)
    assert len(calls) == 2


def test_every_tag_reaches_the_index(calls: list) -> None:
    """A dropped tag is invisible: the job stays green and the alias simply
    keeps pointing at the previous release."""
    tags = [f"{HARBOR}:latest", f"{HARBOR}:3.1.4", f"{HARBOR}:3.1", f"{GHCR}:latest"]
    mod.create_manifests(tags, SOURCES)

    tagged = [
        arg for cmd in calls for arg, flag in zip(cmd[1:], cmd) if flag == "--tag"
    ]
    assert sorted(tagged) == sorted(tags)


def test_both_architectures_are_passed_as_sources(calls: list) -> None:
    """One source would produce a single-arch index that still pulls fine on
    the runner that built it — and fails on the tenant's node."""
    mod.create_manifests([f"{HARBOR}:latest"], SOURCES)
    assert calls[0][-2:] == SOURCES


def test_sources_come_after_the_tag_flags(calls: list) -> None:
    """`imagetools create [OPTIONS] [SOURCE...]` — a source before a --tag is
    parsed as the flag's value."""
    mod.create_manifests([f"{HARBOR}:latest", f"{HARBOR}:3"], SOURCES)
    cmd = calls[0]
    assert cmd.index("--tag") < cmd.index(SOURCES[0])


def test_repository_order_follows_first_appearance(calls: list) -> None:
    """Harbor first means a partial failure leaves the public catalog advanced
    and GHCR behind — the direction build-security.md's recovery note assumes."""
    tags = [f"{HARBOR}:latest", f"{GHCR}:latest", f"{HARBOR}:3"]
    mod.create_manifests(tags, SOURCES)
    assert calls[0][calls[0].index("--tag") + 1].startswith(HARBOR)
    assert calls[1][calls[1].index("--tag") + 1].startswith(GHCR)


def test_grouping_splits_on_the_tag_not_the_registry_port() -> None:
    """`localhost:5000/x:1` has two colons; splitting on the first yields a
    nonsense repo and would scatter one repo's tags across several calls."""
    grouped = mod.group_by_repo(["localhost:5000/x:1", "localhost:5000/x:2"])
    assert list(grouped) == ["localhost:5000/x"]


def test_an_untagged_reference_is_rejected() -> None:
    """Passing a bare repo to `create --tag` would publish `:latest` silently."""
    with pytest.raises(ValueError):
        mod.group_by_repo(["registry.atlan.com/public/app-runtime-base"])


# ── Input handling ───────────────────────────────────────────────────────────


def test_blank_lines_from_the_heredoc_are_dropped() -> None:
    assert mod.parse_tags(f"{HARBOR}:latest\n\n  \n{GHCR}:latest\n") == [
        f"{HARBOR}:latest",
        f"{GHCR}:latest",
    ]


def test_no_tags_is_an_error() -> None:
    """Silently doing nothing here means the release publishes no pullable tag."""
    with pytest.raises(ValueError):
        mod.create_manifests([], SOURCES)


def test_no_sources_is_an_error() -> None:
    with pytest.raises(ValueError):
        mod.create_manifests([f"{HARBOR}:latest"], [])


# ── Failure propagation ──────────────────────────────────────────────────────


def test_a_docker_failure_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-published ladder must fail the job so the re-run path is taken."""

    def boom(cmd: list) -> None:
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(mod, "run", boom)
    with pytest.raises(subprocess.CalledProcessError):
        mod.create_manifests([f"{HARBOR}:latest"], SOURCES)


def test_main_reports_a_docker_failure_as_a_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(cmd: list) -> None:
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(mod, "run", boom)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "create_multiarch_manifest.py",
            "--tags",
            f"{HARBOR}:latest",
            "--source",
            SOURCES[0],
        ],
    )
    assert mod.main() == 1
