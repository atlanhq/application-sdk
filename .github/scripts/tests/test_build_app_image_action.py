"""Structural guards for .github/actions/build-app-image and its sdr-e2e caller.

The image build was extracted out of ``sdr-e2e`` (FND-31) so the full-DAG
pipeline can build once per run, ahead of the e2e matrix, and install that
image onto the target tenant before any leg starts. The extraction introduced
three things that break *silently* rather than loudly, so each gets a guard
here:

1. **Cross-action asset paths.** ``build-app-image`` reads the repin script out
   of the sibling ``sdr-e2e`` directory, and ``container_python_version.py``
   out of ``.github/scripts``. Both are plain relative paths resolved at run
   time — renaming or moving either directory leaves a path that only fails
   inside a live e2e run, on a tenant, minutes in.

2. **The prebuilt-image seam.** When a caller passes ``prebuilt-image`` the
   build step is skipped, so ``steps.build.outputs.image`` is empty. Anything
   still reading the build step's output directly would resolve to an empty
   image reference; the configurator would template an empty ``app_image`` and
   the failure would surface as an opaque compose pull error. The single
   resolver step exists to prevent that, and the action's ``image`` output must
   read *it*.

3. **The buildx cache scope.** It is keyed ``sdr-<app-name>``. Changing the
   string is invisible in review and merely makes every build cold — a ~2
   minute regression per leg that no assertion would otherwise catch.

These are YAML-shape assertions, deliberately. The behaviour they protect is
GitHub Actions' own (skipped-step outputs, action path resolution), which
cannot be exercised without a runner, so the contract is pinned at the point
where a human edit would break it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ACTIONS = _REPO_ROOT / ".github" / "actions"
_BUILD_ACTION_DIR = _ACTIONS / "build-app-image"
_BUILD_ACTION = _BUILD_ACTION_DIR / "action.yaml"
_SDR_ACTION = _ACTIONS / "sdr-e2e" / "action.yaml"


def _steps(action_yaml: Path) -> list[dict]:  # type: ignore[type-arg]
    parsed = yaml.safe_load(action_yaml.read_text(encoding="utf-8"))
    return parsed["runs"]["steps"]


def _step(action_yaml: Path, name: str) -> dict:  # type: ignore[type-arg]
    for step in _steps(action_yaml):
        if step.get("name") == name:
            return step
    raise AssertionError(f"{action_yaml.name} has no step named {name!r}")


# ── 1. Cross-action asset paths resolve ──────────────────────────────────────
# `${{ github.action_path }}/<rel>` is resolved against the action's own
# directory at run time, so the same arithmetic is done here against
# _BUILD_ACTION_DIR. Reaching into a sibling action is the established pattern
# in this repo (connector-unit-tests reads connector-integration-tests' copy of
# the repin script the same way), but nothing about it is checked by GitHub.


@pytest.mark.parametrize(
    "pattern, description",
    [
        (
            r"\$\{\{ github\.action_path \}\}/(\S*?repin-application-sdk\.sh)",
            "repin script",
        ),
        (
            r"\$\{\{ github\.action_path \}\}/(\S*?container_python_version\.py)",
            "interpreter-assert script",
        ),
        (
            r"\$\{\{ github\.action_path \}\}/(\S*?assert_image_platforms\.py)",
            "platform-assert script",
        ),
    ],
)
def test_build_action_asset_paths_resolve(pattern: str, description: str) -> None:
    text = _BUILD_ACTION.read_text(encoding="utf-8")
    match = re.search(pattern, text)
    assert match, f"build-app-image no longer references the {description}"

    # Strip the quote the YAML uses around the interpolated path, if any.
    relative = match.group(1).rstrip('"')
    resolved = (_BUILD_ACTION_DIR / relative).resolve()
    assert resolved.is_file(), (
        f"build-app-image resolves the {description} to {resolved}, which does "
        "not exist. An action or script directory moved without updating the "
        "relative path — this fails only inside a live e2e run otherwise."
    )


# ── 2. The prebuilt-image seam ───────────────────────────────────────────────


def test_sdr_e2e_declares_prebuilt_image_input() -> None:
    parsed = yaml.safe_load(_SDR_ACTION.read_text(encoding="utf-8"))
    assert "prebuilt-image" in parsed["inputs"], (
        "sdr-e2e no longer declares prebuilt-image; the full-DAG legs would "
        "each rebuild the image prepare-tenant already built and installed."
    )
    assert parsed["inputs"]["prebuilt-image"].get("default") == "", (
        "prebuilt-image must default to empty so callers that do not set it "
        "keep building their own image."
    )


def test_sdr_e2e_build_step_is_gated_on_prebuilt_image() -> None:
    step = _step(_SDR_ACTION, "Build and push PR image")
    assert step.get("if") == "inputs.prebuilt-image == ''", (
        "the build step must be skipped when prebuilt-image is supplied, "
        f"got if={step.get('if')!r}"
    )
    assert "build-app-image" in step["uses"], (
        "sdr-e2e must delegate the build to build-app-image so prepare-tenant "
        "and the legs share one implementation"
    )
    referenced = step["uses"].split("@")[0].rsplit("/", 1)[-1]
    assert (
        _ACTIONS / referenced / "action.yaml"
    ).is_file(), f"sdr-e2e references the action {referenced!r}, which does not exist"


def test_sdr_e2e_image_output_reads_the_resolver_not_the_build_step() -> None:
    parsed = yaml.safe_load(_SDR_ACTION.read_text(encoding="utf-8"))
    value = parsed["outputs"]["image"]["value"]
    assert "steps.image.outputs.ref" in value, (
        "sdr-e2e's `image` output must read the resolver step. Reading "
        "steps.build.outputs.image directly returns EMPTY whenever "
        "prebuilt-image is set, because a skipped step has no outputs."
    )


def test_no_step_reads_the_build_output_directly() -> None:
    """Only the resolver may read the (possibly skipped) build step's output."""
    offenders = [
        step.get("name")
        for step in _steps(_SDR_ACTION)
        if step.get("name") != "Resolve effective image reference"
        and "steps.build.outputs.image" in yaml.safe_dump(step)
    ]
    assert not offenders, (
        f"these steps read steps.build.outputs.image directly: {offenders}. "
        "That output is empty when prebuilt-image is set — read "
        "steps.image.outputs.ref instead."
    )


def test_resolver_prefers_prebuilt_over_built() -> None:
    step = _step(_SDR_ACTION, "Resolve effective image reference")
    assert "${PREBUILT:-$BUILT}" in step["run"], (
        "the resolver must prefer the caller-supplied prebuilt-image and fall "
        "back to the built image"
    )
    # Both empty is a bug, not a degraded mode: an empty app_image reaches the
    # configurator and surfaces as an opaque compose pull failure.
    assert "exit 1" in step["run"], (
        "the resolver must fail when neither an image was built nor one was "
        "supplied, rather than emitting an empty reference"
    )


# ── 4. Multi-arch on the install path ────────────────────────────────────────
# The image is pulled by two machines of different architectures: the runner
# (per-leg worker under docker compose) and the tenant's cluster node (the pod
# Heracles fetches the DAG from at submit). A single-arch image satisfies one and
# fails the other ~2 minutes after a successful install, as ImagePullBackOff —
# which is how FND-31's first live install ended.


def test_platforms_defaults_to_empty_so_existing_callers_are_untouched() -> None:
    parsed = yaml.safe_load(_BUILD_ACTION.read_text(encoding="utf-8"))
    spec = parsed["inputs"]["platforms"]
    assert spec.get("default") == "", (
        "platforms must default to empty. 17 repos call sdr-e2e directly and "
        "every one of them would start paying for an emulated cross-build."
    )
    assert spec.get("required") is False


def test_the_platform_flag_is_only_appended_when_requested() -> None:
    """`--platform ""` is not a no-op — buildx rejects it.

    So the default path has to stay byte-identical to the pre-input command line,
    which means a conditional append rather than an always-present flag.
    """
    build = _step(_BUILD_ACTION, "Build and push PR image")
    assert 'if [ -n "${PLATFORMS}" ]' in build["run"], (
        "the --platform flag must be appended conditionally, or every "
        "single-arch caller's build breaks on an empty value"
    )
    assert (
        build["env"].get("PLATFORMS") == "${{ inputs.platforms }}"
    ), "the value must reach the shell via env:, not be interpolated into run:"


def test_the_multi_platform_assert_is_gated_on_more_than_one_platform() -> None:
    """A dropped flag must red the build, not the tenant's pull minutes later.

    Gated on a comma rather than on `platforms` being non-empty: a
    single-platform push is not an index, so gating on non-empty would fail every
    leg of a per-architecture matrix for being exactly what it should be. Those
    legs are covered by the merge job's assert on the combined manifest instead.
    """
    step = _step(
        _BUILD_ACTION, "Assert the pushed image serves every requested platform"
    )
    assert step.get("if") == "contains(inputs.platforms, ',')", (
        "the assert must fire only for a genuinely multi-platform build, "
        f"got if={step.get('if')!r}"
    )
    assert "imagetools inspect --raw" in step["run"], (
        "read the manifest from the REGISTRY: --push means that is the copy the "
        "tenant will pull, and the local daemon holds no multi-arch index at all"
    )
    assert "assert_image_platforms.py" in step["run"]

    # Order matters: asserting before the push would inspect a stale tag.
    names = [s.get("name") for s in _steps(_BUILD_ACTION)]
    assert names.index("Build and push PR image") < names.index(step["name"])


def _reusable() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/tests-reusable.yaml").read_text(
            encoding="utf-8"
        )
    )


def test_each_architecture_builds_on_a_runner_native_to_it() -> None:
    """The whole point of the split, and it fails silently if broken.

    `platform: linux/arm64` on an x64 runner still succeeds — buildx emulates it
    under QEMU, at a cost the org measured at 5-10x native. Nothing goes red; the
    build just takes several times longer, forever, on a path that runs on every
    install-path e2e. So the pairing is pinned rather than trusted.
    """
    job = _reusable()["jobs"]["build-e2e-image"]
    assert job["runs-on"] == "${{ matrix.runner }}", (
        "the runner must come from the matrix, or both architectures land on the "
        "same one and one of them is emulated"
    )

    legs = {leg["arch"]: leg for leg in job["strategy"]["matrix"]["include"]}
    assert set(legs) == {"amd64", "arm64"}, (
        f"the install path builds {sorted(legs)}. It needs BOTH: arm64 for the "
        "tenant's node, amd64 for the per-leg worker on the runner. Retargeting "
        "to the tenant's architecture moves the breakage rather than fixing it."
    )
    for arch, leg in legs.items():
        assert leg["platform"] == f"linux/{arch}", (
            f"the {arch} leg builds {leg['platform']!r} — the arch label and the "
            "platform have drifted, so one architecture is built twice and the "
            "other not at all"
        )
    assert "arm" in legs["arm64"]["runner"], (
        f"the arm64 leg runs on {legs['arm64']['runner']!r}, which is not an ARM "
        "runner — that build would be emulated. `ubuntu-24.04-arm` is available "
        "on this plan and already used elsewhere in the org."
    )
    assert "arm" not in legs["amd64"]["runner"]


def test_the_per_arch_legs_do_not_collide_in_the_tag_or_the_cache() -> None:
    job = _reusable()["jobs"]["build-e2e-image"]
    step = next(s for s in job["steps"] if "build-app-image" in str(s.get("uses", "")))
    assert step["with"]["tag-suffix"] == "-${{ matrix.arch }}", (
        "each leg needs its own tag (the merge step combines two references) and "
        "its own buildx cache scope. A shared scope is the silent half: two "
        "concurrent builds overwrite each other's cache manifest and both go "
        "cold on every later run."
    )
    assert step["with"]["platforms"] == "${{ matrix.platform }}"


def test_the_merge_job_asserts_the_reference_the_tenant_pulls() -> None:
    """The per-arch legs cannot assert themselves — only the merged tag can be.

    A leg that quietly built the wrong architecture produces an index with two of
    the same, and nothing downstream notices: GM accepts the version, LM accepts
    the install.
    """
    job = _reusable()["jobs"]["merge-e2e-image"]
    scripts = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "imagetools create" in scripts
    assert "assert_image_platforms.py" in scripts, (
        "the merged manifest must be asserted; it is the reference "
        "prepare-tenant publishes and the tenant pulls"
    )
    assert "linux/amd64,linux/arm64" in scripts

    # Reads image-base, never a per-arch reference: base is identical from every
    # matrix leg, which is what makes last-writer-wins outputs safe here.
    assert "outputs.image-base" in yaml.safe_dump(job)


# ── 5. Reading back a just-pushed tag is a race ──────────────────────────────
# buildx's container driver (required for the gha layer cache) exports only to
# the registry, so anything wanting to run the image it just built had to fetch
# it back — 22.7s on a measured amd64 leg, and a read of a tag GHCR does not
# always serve yet (`manifest unknown` 0.7s after a successful push, on a live
# arm64 leg). `--load` removes the read; where a read is unavoidable, it retries.


def test_a_single_platform_build_is_loaded_locally() -> None:
    build = _step(_BUILD_ACTION, "Build and push PR image")
    # The flag itself, not the substring: the comment above it explains --load at
    # length, so `"--load" in run` stays true even after the flag is deleted.
    assert "BUILD_ARGS+=(--load)" in build["run"], (
        "the build must also export into the local daemon, or the interpreter "
        "assert pays a registry round-trip for an image this step just built"
    )
    # Gated: the docker exporter cannot express a manifest list, so a caller
    # naming two platforms in one build would fail outright.
    assert "*,*)" in build["run"], (
        "--load must be skipped for a multi-platform build, which cannot be "
        "represented in the local daemon"
    )


def test_the_interpreter_assert_does_not_pull() -> None:
    """The whole point of --load is that this step touches no registry."""
    run = _step(_BUILD_ACTION, "Assert worker image Python matches expected")["run"]
    assert "docker pull" not in run, (
        "a docker pull here defeats --load: the image is already local, and "
        "pulling reintroduces both the latency and the read-after-write race"
    )
    assert "docker run --rm --entrypoint python" in run


def test_both_architectures_are_asserted_not_just_one() -> None:
    """Neither leg may opt out of the interpreter check.

    A multi-arch base is a manifest list whose arm64 entry is a SEPARATELY BUILT
    image; nothing guarantees its interpreter matches amd64's unless something
    checks. Each leg runs on a runner native to what it built, so each asserts its
    own variant — and the legs are parallel, so the second check is free.
    """
    job = _reusable()["jobs"]["build-e2e-image"]
    step = next(s for s in job["steps"] if "build-app-image" in str(s.get("uses", "")))
    assert "expected-python-version" not in step.get("with", {}), (
        "the install path pins expected-python-version per leg. Leave it at the "
        "action's default so BOTH architectures are asserted — passing '' on one "
        "leg silently skips that architecture's interpreter check."
    )


def test_the_merge_job_retries_its_manifest_read() -> None:
    """The one read that --load cannot remove.

    `imagetools create` is purely registry-side: the manifest list exists only in
    the registry, so there is no local copy to inspect and reading it back a step
    later is the same race that failed a build leg.
    """
    job = _reusable()["jobs"]["merge-e2e-image"]
    inspect = next(
        s for s in job["steps"] if "imagetools inspect" in str(s.get("run", ""))
    )
    run = inspect["run"]
    assert "with-retry.sh" in run, (
        "the merge job reads back a manifest it created one step earlier, and "
        "unlike the build legs it has no local copy to fall back on"
    )
    # Captured into a variable, never piped straight into the assertion. Piping a
    # RETRIED command concatenates every attempt's output into the reader's stdin
    # — the second attempt's JSON lands appended to the first's, and the parse
    # fails on valid data. Capturing completes all retries before anything reads.
    assert "RAW=$(" in run and "printf " in run, (
        "the retried inspect must be captured before it is parsed: piping "
        "with-retry directly into the assertion concatenates the output of every "
        "attempt, so a retry produces malformed JSON from a healthy registry"
    )
    for line in run.splitlines():
        if "with-retry.sh" in line:
            assert "assert_image_platforms.py" not in line, (
                "retry the inspect, not the assertion: a genuinely single-arch "
                "manifest must fail once rather than three times"
            )


def test_pkl_is_downloaded_for_the_runners_architecture() -> None:
    """`build-app-image` puts `regenerate-contract` on an arm64 runner.

    The pkl asset was hardcoded `pkl-linux-amd64`, which on an ARM runner fetches
    a binary that cannot execute — `cannot execute binary file`, several steps
    before anything mentions architecture. Derived from `runner.arch` in the
    expression layer so there is no inlined conditional shell (ci.md).
    """
    action = (_ACTIONS / "regenerate-contract" / "action.yaml").read_text(
        encoding="utf-8"
    )
    assert "pkl-linux-amd64" not in action, (
        "the pkl download is hardcoded to amd64 again. build-app-image now runs "
        "this action on ubuntu-24.04-arm for the e2e install path's arm64 leg."
    )
    assert (
        "runner.arch" in action and "aarch64" in action
    ), "the pkl asset must be selected from runner.arch, with aarch64 for ARM"


# ── 3. The buildx cache scope ────────────────────────────────────────────────


def test_buildx_cache_scope_is_unchanged() -> None:
    text = _BUILD_ACTION.read_text(encoding="utf-8")
    # `[^"]` rather than `\S`: the scope interpolates `${{ inputs.app-name }}`,
    # which contains spaces.
    scopes = set(re.findall(r"scope=([^\"]+)\"", text))
    expected = "sdr-${{ inputs.app-name }}${{ inputs.tag-suffix }}"
    assert scopes == {expected}, (
        f"buildx cache scope changed to {scopes}. The scope was `sdr-<app-name>` "
        "before the build was extracted from sdr-e2e; changing it silently "
        "discards every connector's warm `uv sync` layer."
    )
    # `tag-suffix` defaults to empty, so for every caller that does not build
    # per-architecture the scope resolves to the byte-identical string it always
    # was — the suffix buys per-arch isolation without costing anyone a cold build.
    parsed = yaml.safe_load(text)
    assert parsed["inputs"]["tag-suffix"].get("default") == "", (
        "tag-suffix must default to empty, or the 17 repos calling sdr-e2e "
        "directly all lose their warm `uv sync` layer on the next run"
    )
    # The set comparison above catches a *changed* scope string but not a
    # *deleted* cache direction — removing `--cache-from` entirely still leaves
    # the `--cache-to` match, so the set stays a singleton and the test passes
    # while every build goes cold-read. Pin both directions explicitly.
    assert f'--cache-from "type=gha,scope={expected}"' in text, (
        "the `--cache-from` line was removed. Builds would go cold-read on "
        "every run, a ~2 minute regression per leg that no assertion would "
        "otherwise catch."
    )
    assert f'--cache-to "type=gha,mode=max,scope={expected}"' in text, (
        "the `--cache-to` line was removed (or `mode=max` was dropped). Builds "
        "would never write the cache, so every leg goes cold on the next run."
    )


# --- the PR-scoped SDK runtime base ----------------------------------------
# `build-sdk-base-image` (pull_request.yaml) publishes a PR-scoped
# app-runtime-base so an e2e-labelled SDK PR exercises its own Dockerfile /
# daprd / base changes. Every leg of `build-e2e-image` above then does
# `FROM <that image>`, which makes the two a cross-file contract: whatever
# architectures the connector matrix builds, the base must serve. Nothing else
# enforced it — the two live in different workflows, and the base build stayed
# amd64-only after the matrix went two-arch, failing every e2e-labelled SDK PR's
# arm64 leg on `no match for platform in manifest` before a single line of the
# connector Dockerfile ran.


def _pull_request_workflow() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/pull_request.yaml").read_text(encoding="utf-8")
    )


def _sdk_base_job() -> dict:  # type: ignore[type-arg]
    return _pull_request_workflow()["jobs"]["build-sdk-base-image"]


def _sdk_base_build_step() -> dict:  # type: ignore[type-arg]
    return next(
        s
        for s in _sdk_base_job()["steps"]
        if "secure-build-push-apps" in str(s.get("uses", ""))
    )


def _legs(job: dict) -> dict:  # type: ignore[type-arg]
    return {leg["arch"]: leg for leg in job["strategy"]["matrix"]["include"]}


def test_the_pr_base_image_serves_every_arch_the_connector_matrix_builds() -> None:
    """Both matrices are read from the YAML, so adding a third architecture to
    the app image cannot silently leave the base behind."""
    consumed = {
        leg["platform"]
        for leg in _legs(_reusable()["jobs"]["build-e2e-image"]).values()
    }
    published = {leg["platform"] for leg in _legs(_sdk_base_job()).values()}

    missing = consumed - published
    assert not missing, (
        f"build-e2e-image builds {sorted(consumed)} FROM the PR base image, but "
        f"the base publishes only {sorted(published)}. The {sorted(missing)} "
        "leg(s) fail on `no match for platform in manifest` — a failure that "
        "happens before the Dockerfile runs and reads as nothing to do with the "
        "base image."
    )


def test_the_base_image_legs_are_native_like_the_app_image_legs() -> None:
    # Same property test_each_architecture_builds_on_a_runner_native_to_it pins
    # for the app image, and it fails just as silently here: an emulated leg
    # still succeeds, only slower, on a path that runs on every e2e-labelled PR.
    job = _sdk_base_job()
    assert job["runs-on"] == "${{ matrix.runner }}"
    legs = _legs(job)
    for arch, leg in legs.items():
        assert leg["platform"] == f"linux/{arch}"
    assert "arm" in legs["arm64"]["runner"]
    assert "arm" not in legs["amd64"]["runner"]


def test_the_base_image_legs_do_not_collide_in_the_tag_or_the_cache() -> None:
    # The action defaults to an unscoped `type=gha`, so without an explicit
    # per-arch scope the two legs overwrite each other's cache manifest and both
    # go cold on every later run — silently.
    with_ = _sdk_base_build_step()["with"]
    assert with_["tags"].endswith("-${{ matrix.arch }}")
    assert with_["platforms"] == "${{ matrix.platform }}"
    assert "${{ matrix.arch }}" in with_["cache-from"]
    assert "${{ matrix.arch }}" in with_["cache-to"]


def test_the_base_image_build_is_reached_through_the_scanning_action() -> None:
    # Swapping in a raw docker/build-push-action would publish unscanned images.
    step = _sdk_base_build_step()
    assert "secure-build-push-apps" in step["uses"]
    assert step["with"]["push"] is True


def test_the_merge_job_asserts_the_reference_the_connector_builds_from() -> None:
    """The per-arch legs cannot assert themselves — only the merged tag can be.

    A leg that quietly built the wrong architecture produces an index with two
    of the same, and the failure surfaces a repo away as the connector's
    `no match for platform in manifest`.
    """
    job = _pull_request_workflow()["jobs"]["merge-sdk-base-image"]
    scripts = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "imagetools create" in scripts
    assert "assert_image_platforms.py" in scripts
    assert "linux/amd64,linux/arm64" in scripts
    assert "with-retry.sh" in scripts, (
        "`imagetools create` writes the manifest list only in the registry, so "
        "reading it back a step later is a read-after-write race"
    )


def test_the_connector_dispatch_gates_on_both_base_image_jobs() -> None:
    """A failed arch leg leaves the merge SKIPPED, not failed.

    'skipped' is the benign value in these clauses, so gating on the merge alone
    would dispatch connectors against a manifest tag that was never created.
    """
    gate = _pull_request_workflow()["jobs"]["connector-tests"]["if"]
    for job in ("build-sdk-base-image", "merge-sdk-base-image"):
        assert f"needs.{job}.result == 'success'" in gate
        assert f"needs.{job}.result == 'skipped'" in gate


def test_the_container_filter_covers_the_action_that_builds_the_image() -> None:
    """Otherwise the base-image jobs skip on the PRs most likely to break them.

    `build-sdk-base-image` and `trivy-container` are gated on the `container`
    paths-filter. A PR that changes only the action doing the building and
    scanning matches nothing in that filter, so both jobs skip and the change
    merges without either ever running against it — which is exactly what
    happened to the per-arch split and the scan-platform fix on their own PR.

    Derived from the build step rather than hardcoded, so pointing the base
    image at a different action moves this assertion with it.
    """
    action = _sdk_base_build_step()["uses"].rstrip("/").rsplit("/", 1)[-1]
    filters = yaml.safe_load(
        next(
            step
            for step in _pull_request_workflow()["jobs"]["changes"]["steps"]
            if "paths-filter" in str(step.get("uses", ""))
        )["with"]["filters"]
    )
    covered = [p for p in filters["container"] if action in p]
    assert covered, (
        f"the `container` filter does not mention {action!r}, the action "
        "build-sdk-base-image uses. A PR changing only that action skips both "
        f"the base-image build and the container scan. Add "
        f"'.github/actions/{action}/**'."
    )


def test_the_scan_step_builds_the_runners_own_architecture() -> None:
    """Otherwise the native split moves the emulation into the Trivy scan.

    The scan builds and `--load`s an image for Trivy, then the push builds the
    requested platform. Hardcoding amd64 there means an arm64 leg scans an
    emulated amd64 image that is not the artefact being pushed — the scan stops
    describing the thing it gates.
    """
    action = yaml.safe_load(
        (_ACTIONS / "secure-build-push-apps" / "action.yaml").read_text(
            encoding="utf-8"
        )
    )
    scan = next(s for s in action["runs"]["steps"] if s.get("id") == "build_for_scan")
    assert (
        "runner.arch" in scan["with"]["platforms"]
    ), "the scan platform must follow the runner, not be hardcoded"
