"""Guards for the native-per-architecture builds of `app-runtime-base`.

Every property here fails SILENTLY if it regresses, which is the only reason
these are tests rather than comments:

* An emulated arm64 leg still succeeds. `platform: linux/arm64` on an x64
  runner builds under QEMU and produces a correct image, several times slower.
  A review sees a passing build and a one-word diff.
* A clobbered build cache still succeeds. Two legs writing one unscoped
  `type=gha` scope overwrite each other's manifest and both go cold on the next
  run; the only symptom is time.
* A shared concurrency group still succeeds. A job-level `concurrency` on a
  matrix job resolves to the same key for every leg unless the matrix context
  is in it, so one leg cancels the other and the merge job never finds both.

The same properties on the *connector* image paths are pinned in
test_build_app_image_action.py; this file covers the two workflows that publish
the base image those paths build FROM.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import harbor_release_tags  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HARBOR = _REPO_ROOT / ".github/workflows/harbor-release.yaml"
_GHCR = _REPO_ROOT / ".github/workflows/build-image.yaml"

#: (workflow path, build job, merge job) for each base-image publisher.
_PUBLISHERS = (
    pytest.param(_HARBOR, "build", "merge", id="harbor-release"),
    pytest.param(_GHCR, "push-to-ghcr", "merge-ghcr", id="build-image"),
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job(path: Path, name: str) -> dict:
    jobs = _load(path)["jobs"]
    assert name in jobs, f"{path.name} has no job {name!r}; jobs are {sorted(jobs)}"
    return jobs[name]


def _legs(job: dict) -> dict:
    """Return ``{arch: matrix leg}`` for a build job."""
    include = job["strategy"]["matrix"]["include"]
    return {leg["arch"]: leg for leg in include}


def _step_with(job: dict, needle: str) -> dict:
    for step in job["steps"]:
        if needle in str(step.get("uses", "")) or needle in str(step.get("name", "")):
            return step
    raise AssertionError(f"no step matching {needle!r} in job")


# ── Native runners ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path, build, _merge", _PUBLISHERS)
def test_each_architecture_builds_on_a_runner_native_to_it(
    path: Path, build: str, _merge: str
) -> None:
    job = _job(path, build)
    assert job["runs-on"] == "${{ matrix.runner }}", (
        f"{path.name}:{build} pins a single runner, so one architecture is "
        "emulated. The runner must come from the matrix."
    )

    legs = _legs(job)
    assert set(legs) == {"amd64", "arm64"}, (
        f"{path.name} publishes {sorted(legs)}. The base image needs BOTH: "
        "arm64 for tenant nodes, amd64 for CI and local development. Dropping "
        "one retargets the image rather than speeding it up."
    )

    for arch, leg in legs.items():
        assert leg["platform"] == f"linux/{arch}", (
            f"the {arch} leg builds {leg['platform']!r} — the arch label and the "
            "platform have drifted, so the tag says one thing and the image is "
            "another."
        )

    assert "arm" in legs["arm64"]["runner"], (
        f"the arm64 leg runs on {legs['arm64']['runner']!r}, which is not an ARM "
        "runner — that build is emulated. Use `ubuntu-24.04-arm`."
    )
    assert "arm" not in legs["amd64"]["runner"]


@pytest.mark.parametrize("path, build, _merge", _PUBLISHERS)
def test_a_failing_architecture_does_not_cancel_the_other(
    path: Path, build: str, _merge: str
) -> None:
    """Whether ONE arch or BOTH broke is the entire diagnosis."""
    assert _job(path, build)["strategy"]["fail-fast"] is False


# ── The legs do not collide ──────────────────────────────────────────────────


@pytest.mark.parametrize("path, build, _merge", _PUBLISHERS)
def test_the_legs_do_not_share_a_build_cache_scope(
    path: Path, build: str, _merge: str
) -> None:
    """secure-build-push-apps defaults to an unscoped `type=gha`."""
    build_step = _step_with(_job(path, build), "secure-build-push-apps")
    for key in ("cache-from", "cache-to"):
        value = build_step["with"].get(key, "")
        assert "scope=" in value, (
            f"{path.name}:{build} leaves `{key}` unscoped, so the two legs "
            "overwrite each other's cache manifest and both go cold — visible "
            "only as build time."
        )
        assert "matrix.platform" in value or "matrix.arch" in value, (
            f"{path.name}:{build} scopes `{key}` to a constant, which is the "
            "same collision with extra steps."
        )


@pytest.mark.parametrize("path, build, _merge", _PUBLISHERS)
def test_the_legs_do_not_share_a_concurrency_group(
    path: Path, build: str, _merge: str
) -> None:
    job = _job(path, build)
    group = job.get("concurrency", {}).get("group")
    if group is None:
        pytest.skip("job declares no concurrency group")
    assert "matrix.arch" in group or "matrix.platform" in group, (
        f"{path.name}:{build} shares one concurrency group across the matrix, "
        "so the legs cancel each other and only one architecture is ever "
        "pushed. Put the matrix context in the group key."
    )


@pytest.mark.parametrize("path, build, _merge", _PUBLISHERS)
def test_the_legs_do_not_share_a_tag(path: Path, build: str, _merge: str) -> None:
    """Both legs pushing one tag means whichever finishes last wins and the
    published image is single-arch — with no error anywhere."""
    build_step = _step_with(_job(path, build), "secure-build-push-apps")
    tags = str(build_step["with"]["tags"])
    assert (
        "matrix.arch" in tags or "matrix.platform" in tags
    ), f"{path.name}:{build} pushes both architectures to the same tag."


# ── The merge job puts them back together ────────────────────────────────────


@pytest.mark.parametrize("path, build, merge", _PUBLISHERS)
def test_a_merge_job_waits_for_every_build_leg(
    path: Path, build: str, merge: str
) -> None:
    """Without this the per-arch tags are all that is ever published, and every
    consumer's `FROM` fails on `no match for platform in manifest`."""
    needs = _job(path, merge)["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert build in needs, f"{path.name}:{merge} does not wait for {build!r}"


@pytest.mark.parametrize("path, build, merge", _PUBLISHERS)
def test_the_merge_job_combines_both_architectures(
    path: Path, build: str, merge: str
) -> None:
    body = yaml.dump(_job(path, merge))
    assert "amd64" in body and "arm64" in body, (
        f"{path.name}:{merge} does not reference both per-arch images, so the "
        "manifest it publishes cannot be multi-arch."
    )


# ── The published ladder still reaches both registries ───────────────────────


def test_harbor_release_still_publishes_to_both_registries() -> None:
    """The GHCR mirror is what keeps app CI off Harbor's S3 egress. Losing it
    is a cost regression that nothing else reports."""
    registries = {repo.split("/")[0] for repo in harbor_release_tags.REPOS}
    assert registries == {"registry.atlan.com", "ghcr.io"}


def test_only_the_merge_job_holds_the_harbor_credential() -> None:
    """The per-arch legs push half-images. Harbor's project is the public,
    partner-facing catalog, so it must never see an `-amd64` tag — the build
    legs simply cannot reach it."""
    build_job = yaml.dump(_job(_HARBOR, "build"))
    merge_job = yaml.dump(_job(_HARBOR, "merge"))
    assert "HARBOR_PASSWORD" not in build_job, (
        "the per-arch build job can log in to Harbor. It pushes arch-suffixed "
        "staging tags, which do not belong in the public catalog."
    )
    assert "HARBOR_PASSWORD" in merge_job


def test_the_staging_tags_the_merge_reads_are_the_ones_the_build_wrote() -> None:
    """These are two separate expressions in two jobs; if they drift, the merge
    fails on a missing manifest at release time and nowhere earlier."""
    workflow = _load(_HARBOR)
    prepare_outputs = workflow["jobs"]["prepare"]["outputs"]
    assert "staging_base" in prepare_outputs, (
        "the staging repository is no longer a shared output, so the build and "
        "merge jobs each spell it out and can drift apart."
    )

    build_tags = str(
        _step_with(_job(_HARBOR, "build"), "secure-build-push-apps")["with"]["tags"]
    )
    merge_body = yaml.dump(_job(_HARBOR, "merge"))
    for fragment in ("staging_base", "outputs.sha"):
        assert fragment in build_tags, f"build tag lost {fragment!r}"
        assert fragment in merge_body, f"merge source lost {fragment!r}"


# ── Coverage ─────────────────────────────────────────────────────────────────


#: Workflows that reference `app-runtime-base` but only CONSUME it — they pull,
#: scan, or read the tag rather than producing it. Reviewed by hand; the test
#: below fails when a new workflow appears so the classification is a decision
#: someone makes, not one the test makes for itself.
_CONSUMERS_ONLY = frozenset(
    {
        # Builds app images FROM the base; per-arch native runners are pinned in
        # test_build_app_image_action.py, not here.
        "build-and-publish-app.yaml",
        "pull_request.yaml",
        # Read the tag / scan the published image.
        "check-dapr-version.yaml",
        "daily-security-scan.yml",
        "update-dashboard.yaml",
        "v3-readiness-check.yaml",
        "vuln-reconcile-on-release.yml",
    }
)


def test_every_base_image_publisher_is_covered() -> None:
    """A third workflow publishing app-runtime-base could otherwise emulate
    freely. Anchors the parametrisation to the tree rather than to itself."""
    referencing = {
        path.name
        for path in (_REPO_ROOT / ".github/workflows").glob("*.y*ml")
        if "app-runtime-base" in path.read_text(encoding="utf-8")
    }

    unclassified = referencing - {_HARBOR.name, _GHCR.name} - _CONSUMERS_ONLY
    assert not unclassified, (
        f"{sorted(unclassified)} reference app-runtime-base and are classified "
        "neither way. If one PUBLISHES the base image, add it to _PUBLISHERS so "
        "its runners are checked; if it only consumes it, add it to "
        "_CONSUMERS_ONLY."
    )

    stale = _CONSUMERS_ONLY - referencing
    assert not stale, f"_CONSUMERS_ONLY lists workflows that are gone: {sorted(stale)}"
