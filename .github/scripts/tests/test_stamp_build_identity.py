"""Tests for .github/scripts/stamp_build_identity.py (FND-1684).

The stamp is what makes a pod able to say which build it is, and every failure
mode of this script is silent: an unstamped image reports no build identity, and
the e2e version check then falls back to the marketplace install record — the
circular check this whole change removes. So the cases below are mostly about
that silence, not about string formatting.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import stamp_build_identity as stamp  # noqa: E402

_SINGLE_STAGE = """\
# syntax=docker/dockerfile:1
ARG BASE_IMAGE=registry.atlan.com/public/app-runtime-base:3
FROM ${BASE_IMAGE}
WORKDIR /app
ENV ATLAN_APP_MODULE=app.connector:OpenAPIConnector
"""

_MULTI_STAGE = """\
FROM python:3.13 AS builder
RUN uv sync --locked
FROM registry.atlan.com/public/app-runtime-base:3
COPY --from=builder /app/.venv /app/.venv
"""


def _env_line(text: str) -> str:
    match = re.search(rf"^ENV {stamp.BUILD_ID_ARG}=.*$", text, re.MULTILINE)
    assert match, f"no ENV {stamp.BUILD_ID_ARG} line in:\n{text}"
    return match.group(0)


def test_the_stanza_declares_the_arg_and_promotes_it_to_an_env() -> None:
    """A --build-arg alone is invisible at runtime.

    BuildKit ignores a build-arg the Dockerfile does not ARG, and an ARG is not
    an ENV — so without both lines the image builds cleanly, reports no build
    identity, and the check degrades with nothing red anywhere.
    """
    stamped = stamp.stamp(_SINGLE_STAGE)
    assert f"ARG {stamp.BUILD_ID_ARG}" in stamped
    assert _env_line(stamped) == f"ENV {stamp.BUILD_ID_ARG}=${stamp.BUILD_ID_ARG}"


def test_the_arg_defaults_to_empty_so_an_unstamped_build_still_builds() -> None:
    """Local `docker build .` passes no build-arg, and must not break.

    An ARG with no default is not an error either, but the explicit empty default
    is what makes "this image carries no build identity" the documented value
    rather than an implementation detail of BuildKit.
    """
    assert f'ARG {stamp.BUILD_ID_ARG}=""' in stamp.stamp(_SINGLE_STAGE)


def test_the_stanza_lands_after_the_last_stage() -> None:
    """`buildx build .` with no --target builds the LAST stage.

    Appending inside the builder stage would put the ENV in a layer that is
    thrown away, which is the same silent nothing as not stamping at all.
    """
    stamped = stamp.stamp(_MULTI_STAGE)
    last_from = stamped.rindex("FROM ")
    assert stamped.index(f"ENV {stamp.BUILD_ID_ARG}") > last_from


def test_everything_above_the_stanza_is_byte_identical() -> None:
    """The layer cache is the reason this appends rather than rewrites.

    The stamp differs on every commit; touching any earlier line would
    invalidate the ~2-minute `uv sync` layer on every single build.
    """
    stamped = stamp.stamp(_SINGLE_STAGE)
    assert stamped.startswith(_SINGLE_STAGE.rstrip("\n"))


def test_a_dockerfile_that_already_declares_the_arg_is_left_alone() -> None:
    """A connector may adopt the two lines itself; it must not get them twice."""
    adopted = _SINGLE_STAGE + f'ARG {stamp.BUILD_ID_ARG}=""\n'
    assert stamp.stamp(adopted) == adopted


def test_stamping_twice_is_a_no_op() -> None:
    once = stamp.stamp(_SINGLE_STAGE)
    assert stamp.stamp(once) == once


def test_a_dockerfile_with_no_from_is_refused_rather_than_stamped() -> None:
    """Nothing to stamp into, and a quiet success here reads as a stale pod later."""
    with pytest.raises(stamp.StampError, match="no FROM instruction"):
        stamp.stamp("# just a comment\n")


def test_a_missing_dockerfile_warns_and_does_not_fail_the_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The build step that follows dies on the same file with a better message.

    What this owes the log is the fact that no stamp was applied, so an absent
    build identity downstream is not misread as a stale pod.
    """
    assert stamp.main(["--dockerfile", str(tmp_path / "Dockerfile")]) == 0
    assert "::warning::" in capsys.readouterr().out


def test_main_writes_the_stanza_in_place(tmp_path: Path) -> None:
    path = tmp_path / "Dockerfile"
    path.write_text(_SINGLE_STAGE, encoding="utf-8")
    assert stamp.main(["--dockerfile", str(path)]) == 0
    assert f"ENV {stamp.BUILD_ID_ARG}" in path.read_text(encoding="utf-8")


def test_the_build_action_invokes_this_script_before_the_build() -> None:
    """Order is load-bearing: buildx reads the Dockerfile as it finds it.

    A step added after the build would leave every image unstamped while looking
    entirely successful — the exact shape of failure this script exists inside.
    """
    action = (
        Path(__file__).resolve().parents[1].parent
        / "actions"
        / "build-app-image"
        / "action.yaml"
    ).read_text(encoding="utf-8")
    stamp_at = action.index("stamp_build_identity.py")
    build_at = action.index("docker buildx build")
    assert stamp_at < build_at
    assert f'--build-arg "{stamp.BUILD_ID_ARG}=' in action


def test_the_build_arg_carries_the_unsuffixed_tag() -> None:
    """`tag-suffix` is per-architecture; the tenant pulls the merged manifest.

    So stamping the suffixed reference would give amd64 and arm64 different
    identities, and neither would equal what `verify --expected` compares
    against — which is the un-suffixed tag, via merge-e2e-image's `version`
    output.
    """
    action = (
        Path(__file__).resolve().parents[1].parent
        / "actions"
        / "build-app-image"
        / "action.yaml"
    ).read_text(encoding="utf-8")
    assert f'--build-arg "{stamp.BUILD_ID_ARG}=${{TAG}}"' in action
