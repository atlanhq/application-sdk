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


# ── 3. The buildx cache scope ────────────────────────────────────────────────


def test_buildx_cache_scope_is_unchanged() -> None:
    text = _BUILD_ACTION.read_text(encoding="utf-8")
    # `[^"]` rather than `\S`: the scope interpolates `${{ inputs.app-name }}`,
    # which contains spaces.
    scopes = set(re.findall(r"scope=([^\"]+)\"", text))
    assert scopes == {"sdr-${{ inputs.app-name }}"}, (
        f"buildx cache scope changed to {scopes}. The scope was `sdr-<app-name>` "
        "before the build was extracted from sdr-e2e; changing it silently "
        "discards every connector's warm `uv sync` layer."
    )
    # The set comparison above catches a *changed* scope string but not a
    # *deleted* cache direction — removing `--cache-from` entirely still leaves
    # the `--cache-to` match, so the set stays a singleton and the test passes
    # while every build goes cold-read. Pin both directions explicitly.
    assert '--cache-from "type=gha,scope=sdr-${{ inputs.app-name }}"' in text, (
        "the `--cache-from` line was removed. Builds would go cold-read on "
        "every run, a ~2 minute regression per leg that no assertion would "
        "otherwise catch."
    )
    assert '--cache-to "type=gha,mode=max,scope=sdr-${{ inputs.app-name }}"' in text, (
        "the `--cache-to` line was removed (or `mode=max` was dropped). Builds "
        "would never write the cache, so every leg goes cold on the next run."
    )
