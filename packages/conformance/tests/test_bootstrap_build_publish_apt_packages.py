"""The certify gate's ``apt_packages`` on ``build-and-publish.yaml``.

``build-and-publish.yaml`` is an always-overwrite managed shim. An app whose
dependencies build a native extension (hive's ``pykerberos`` needs
``libkrb5-dev`` for ``gssapi/gssapi.h``) must pass the reusable's
``apt_packages`` input, or "Certify app" cannot install its dependencies and
the release is blocked while every PR check stays green. The template had no
slot for it, so a conformance resync deleted a hand-added line and recorded
nothing as lost (managed shims are rewritten without a ``.bak``).

Both halves of the round trip are pinned here: bootstrap keeps (or inherits
from ``system_deps``) the value, and C002 does not report it as drift.
"""

from __future__ import annotations

import pathlib

import pytest

from conformance.bootstrap.args import parse_bootstrap_args
from conformance.bootstrap.autodetect import apply_bootstrap_autodetection
from conformance.bootstrap.extract import extract_build_publish_apt_packages
from conformance.bootstrap.render import render
from conformance.suite.checks.bootstrap_drift import scan_path

PKGS = "libkrb5-dev gcc python3-dev"
LINE = f'      apt_packages: "{PKGS}"\n'


def _write(root: pathlib.Path, name: str, text: str) -> pathlib.Path:
    path = root / ".github" / "workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _detected(root: pathlib.Path) -> dict[str, str]:
    kwargs = parse_bootstrap_args([])
    apply_bootstrap_autodetection(kwargs, root)
    return kwargs


# ── render / extract ─────────────────────────────────────────────────────────


def test_rendered_only_when_set() -> None:
    assert "apt_packages" not in render("build-and-publish.yaml")
    assert LINE in render("build-and-publish.yaml", build_publish_apt_packages=PKGS)


def test_extract_round_trips_through_render() -> None:
    rendered = render("build-and-publish.yaml", build_publish_apt_packages=PKGS)
    assert extract_build_publish_apt_packages(rendered) == PKGS


def test_extract_is_empty_when_absent_or_commented_out() -> None:
    assert extract_build_publish_apt_packages(render("build-and-publish.yaml")) == ""
    commented = render("build-and-publish.yaml").replace(
        "    secrets: inherit", f'      # apt_packages: "{PKGS}"\n    secrets: inherit'
    )
    assert extract_build_publish_apt_packages(commented) == ""


def test_extract_stops_at_anything_that_is_not_a_package_name() -> None:
    """Same filter as system_deps: the list is cut at the first token that is
    not a package name, so a shell construct never reaches the rendered file."""
    text = render("build-and-publish.yaml").replace(
        "    secrets: inherit",
        '      apt_packages: "libkrb5-dev $(curl x) && gcc"\n    secrets: inherit',
    )
    assert extract_build_publish_apt_packages(text) == "libkrb5-dev"


@pytest.mark.parametrize(
    "token",
    ["libkrb5-dev=1.2.3", "libkrb5-dev/bookworm", "libkrb5-dev:amd64", "LibFoo"],
)
def test_extract_drops_tokens_the_certify_validation_rejects(token: str) -> None:
    """The reusable validates apt_packages as ``[a-z0-9][a-z0-9+.-]*`` per
    token and fails certify over one outlier, so the extractor never hands
    one to the render."""
    text = render("build-and-publish.yaml").replace(
        "    secrets: inherit",
        f'      apt_packages: "gcc {token} python3-dev"\n    secrets: inherit',
    )
    assert extract_build_publish_apt_packages(text) == "gcc python3-dev"


# ── C002 ─────────────────────────────────────────────────────────────────────


def test_rendered_value_is_not_drift(tmp_path: pathlib.Path) -> None:
    wf = _write(
        tmp_path,
        "build-and-publish.yaml",
        render("build-and-publish.yaml", build_publish_apt_packages=PKGS),
    )
    assert scan_path(wf, tmp_path) == []


def test_the_line_as_a_repo_hand_wrote_it_is_not_drift(tmp_path: pathlib.Path) -> None:
    """hive's own line (atlan-hive-app#516), in the position it wrote it."""
    text = render("build-and-publish.yaml").replace(
        '      unit_tests_workflow_file: "tests.yaml"\n',
        '      unit_tests_workflow_file: "tests.yaml"\n' + LINE,
    )
    wf = _write(tmp_path, "build-and-publish.yaml", text)
    assert scan_path(wf, tmp_path) == []


def test_other_edits_next_to_it_are_still_drift(tmp_path: pathlib.Path) -> None:
    text = render("build-and-publish.yaml", build_publish_apt_packages=PKGS).replace(
        "    secrets: inherit", "      channel: nightly\n    secrets: inherit"
    )
    wf = _write(tmp_path, "build-and-publish.yaml", text)
    findings = scan_path(wf, tmp_path)
    assert [f.rule_id for f in findings] == ["C002"]


# ── bootstrap autodetection ──────────────────────────────────────────────────


def test_a_resync_keeps_a_hand_added_line(tmp_path: pathlib.Path) -> None:
    """The atlan-hive-app#519 case: the line is on disk, so the re-render keeps it."""
    _write(
        tmp_path,
        "build-and-publish.yaml",
        render("build-and-publish.yaml", build_publish_apt_packages=PKGS),
    )
    kwargs = _detected(tmp_path)
    assert kwargs["build_publish_apt_packages"] == PKGS
    assert LINE in render(
        "build-and-publish.yaml",
        build_publish_apt_packages=kwargs["build_publish_apt_packages"],
    )


def test_a_repo_that_declares_system_deps_gets_them_in_certify(
    tmp_path: pathlib.Path,
) -> None:
    """checks.yml already installs the headers; certify inherits the same list."""
    _write(tmp_path, "checks.yml", render("checks.yml", system_deps=PKGS))
    _write(tmp_path, "build-and-publish.yaml", render("build-and-publish.yaml"))
    kwargs = _detected(tmp_path)
    assert kwargs["system_deps"] == PKGS
    assert kwargs["build_publish_apt_packages"] == PKGS


def test_the_value_on_build_and_publish_wins_over_system_deps(
    tmp_path: pathlib.Path,
) -> None:
    _write(tmp_path, "checks.yml", render("checks.yml", system_deps="gcc"))
    _write(
        tmp_path,
        "build-and-publish.yaml",
        render("build-and-publish.yaml", build_publish_apt_packages=PKGS),
    )
    assert _detected(tmp_path)["build_publish_apt_packages"] == PKGS


@pytest.mark.parametrize("with_files", [False, True])
def test_a_repo_with_no_system_packages_renders_no_line(
    tmp_path: pathlib.Path, with_files: bool
) -> None:
    if with_files:
        _write(tmp_path, "checks.yml", render("checks.yml"))
        _write(tmp_path, "build-and-publish.yaml", render("build-and-publish.yaml"))
    kwargs = _detected(tmp_path)
    assert kwargs["build_publish_apt_packages"] == ""
    assert "apt_packages" not in render(
        "build-and-publish.yaml",
        build_publish_apt_packages=kwargs["build_publish_apt_packages"],
    )


def test_inherited_system_deps_are_narrowed_to_the_certify_grammar(
    tmp_path: pathlib.Path,
) -> None:
    """system_deps may pin (apt-get accepts it); certify's validation does not."""
    _write(
        tmp_path,
        "checks.yml",
        render("checks.yml", system_deps="libkrb5-dev=1.2.3 gcc python3-dev/bookworm"),
    )
    _write(tmp_path, "build-and-publish.yaml", render("build-and-publish.yaml"))
    kwargs = _detected(tmp_path)
    assert kwargs["system_deps"] == "libkrb5-dev=1.2.3 gcc python3-dev/bookworm"
    assert kwargs["build_publish_apt_packages"] == "gcc"
