"""Tests for ``.github/scripts/pkl_version.py`` — plus the cross-file guards
that keep the pkl pin a *single* source of truth.

The unit cases below cover the driver. The repo-wide guards at the bottom are
the load-bearing half: FND-1864 was not caused by the pin being wrong, it was
caused by the pin being **six literals in six files** that nothing kept in
agreement and no app could read. Deleting the copies fixes it once; these
guards are what stop the seventh from appearing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import pkl_version as pv

REPO_ROOT = Path(__file__).resolve().parents[3]


def _sot(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pkl_version.py"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parse_pin
# ---------------------------------------------------------------------------


def test_parses_the_pin(tmp_path: Path) -> None:
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    assert pv.parse_pin(sot) == "0.32.1"


def test_ignores_mentions_in_prose(tmp_path: Path) -> None:
    # The real SoT's docstring names other versions (0.27, 0.28) while
    # explaining the skew. Anchoring at column 0 on the assignment is what keeps
    # those out.
    sot = _sot(
        tmp_path,
        '"""Valid on 0.28+, rejected on PKL_VERSION: str = "0.27.2" style prose."""\n'
        "# see PKL_VERSION below\n"
        'PKL_VERSION: str = "0.32.1"\n',
    )
    assert pv.parse_pin(sot) == "0.32.1"


def test_missing_assignment_raises(tmp_path: Path) -> None:
    sot = _sot(tmp_path, "PKL = '0.32.1'\n")
    with pytest.raises(ValueError, match="no 'PKL_VERSION"):
        pv.parse_pin(sot)


def test_conflicting_assignments_raise(tmp_path: Path) -> None:
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\nPKL_VERSION: str = "0.27.2"\n')
    with pytest.raises(ValueError, match="conflicting PKL_VERSION values"):
        pv.parse_pin(sot)


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Pkl 0.32.1 (macOS 26.4, native)", "0.32.1"),
        ("Pkl 0.27.2 (Linux 6.8, native)", "0.27.2"),
        ("0.32.1", "0.32.1"),
        ("  0.32.1\n", "0.32.1"),
        ("Pkl 0.33.0-rc.1 (Linux, native)", "0.33.0-rc.1"),
    ],
)
def test_normalize_accepted_spellings(raw: str, expected: str) -> None:
    assert pv.normalize(raw) == expected


def test_normalize_rejects_unparseable() -> None:
    with pytest.raises(ValueError, match="could not parse a pkl version"):
        pv.normalize("command not found: pkl")


# ---------------------------------------------------------------------------
# resolve — the reason every call site can now omit the version
# ---------------------------------------------------------------------------


def test_resolve_empty_requested_yields_the_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    assert pv.main(["--sot", str(sot), "resolve", "--requested", ""]) == 0
    assert capsys.readouterr().out.strip() == "0.32.1"


def test_resolve_whitespace_requested_yields_the_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An unset workflow input arrives as "", but a caller passing a quoted
    # expression that resolved to nothing can arrive as whitespace.
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    assert pv.main(["--sot", str(sot), "resolve", "--requested", "   "]) == 0
    assert capsys.readouterr().out.strip() == "0.32.1"


def test_resolve_explicit_override_wins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    assert pv.main(["--sot", str(sot), "resolve", "--requested", "0.28.2"]) == 0
    assert capsys.readouterr().out.strip() == "0.28.2"


def test_unreadable_sot_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Loud, not defaulted: a pin CI cannot read must fail the job rather than
    # fall back to a guess and quietly reintroduce the skew.
    assert pv.main(["--sot", str(tmp_path / "nope.py"), "resolve"]) == 2
    assert "cannot read the pkl pin" in capsys.readouterr().err


def test_print_version(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    assert pv.main(["--sot", str(sot), "print-version"]) == 0
    assert capsys.readouterr().out.strip() == "0.32.1"


# ---------------------------------------------------------------------------
# check-runtime
# ---------------------------------------------------------------------------


def test_check_runtime_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    code = pv.main(
        [
            "--sot",
            str(sot),
            "check-runtime",
            "--actual",
            "Pkl 0.32.1 (macOS 26.4, native)",
        ]
    )
    assert code == 0
    assert "matches the SDK pin" in capsys.readouterr().out


def test_check_runtime_skew_warns_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    code = pv.main(
        ["--sot", str(sot), "check-runtime", "--actual", "Pkl 0.27.2 (Linux, native)"]
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "::warning::pkl version skew" in err
    assert "0.27.2" in err and "0.32.1" in err


def test_check_runtime_skew_fails_under_strict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    code = pv.main(
        [
            "--sot",
            str(sot),
            "check-runtime",
            "--actual",
            "Pkl 0.27.2 (Linux, native)",
            "--strict",
        ]
    )
    assert code == 1
    assert "::error::pkl version skew" in capsys.readouterr().err


def test_check_runtime_expected_overrides_the_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deliberate override is checked against what it asked for.

    ``install-pkl`` verifies that the pkl on PATH is the version it resolved —
    which for a caller that passed an explicit ``version:`` is NOT the pin. Read
    the pin instead and the one call site allowed to differ would be the one
    that fails.
    """
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    code = pv.main(
        [
            "--sot",
            str(sot),
            "check-runtime",
            "--strict",
            "--expected",
            "0.28.2",
            "--actual",
            "Pkl 0.28.2 (Linux, native)",
        ]
    )
    assert code == 0
    assert "matches the SDK pin" in capsys.readouterr().out


def test_check_runtime_expected_mismatch_names_the_request_not_the_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    code = pv.main(
        [
            "--sot",
            str(sot),
            "check-runtime",
            "--strict",
            "--expected",
            "0.28.2",
            "--actual",
            "Pkl 0.32.1 (Linux, native)",
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "requested is 0.28.2" in err
    # The pin is irrelevant to this comparison and must not be blamed for it.
    assert "SDK pin" not in err


def test_check_runtime_unparseable_is_not_reported_as_skew(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # pkl absent from the runner is an infra gap, not evidence the contract is
    # rendered by the wrong version.
    sot = _sot(tmp_path, 'PKL_VERSION: str = "0.32.1"\n')
    code = pv.main(
        ["--sot", str(sot), "check-runtime", "--actual", "pkl: command not found"]
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "could not parse a pkl version" in err
    assert "skew" not in err


# ---------------------------------------------------------------------------
# Cross-file guards: the pin is declared exactly once
# ---------------------------------------------------------------------------


def test_the_real_sot_parses() -> None:
    """The committed SoT must be readable by the parser CI depends on."""
    version = pv.read_pin()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version


def test_sot_agrees_with_the_python_constant() -> None:
    """The textual read and the import must agree.

    CI reads the pin as text (it has no SDK install); apps read it by import.
    Two readers of one line is the whole design, so a reshaping that breaks
    either one has to fail here.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from application_sdk.pkl_version import PKL_VERSION  # noqa: PLC0415

    assert pv.read_pin() == PKL_VERSION


def _ci_files() -> list[Path]:
    """Every workflow, composite action and CI script in the repo."""
    files: list[Path] = []
    for pattern in (
        ".github/workflows/*.yml",
        ".github/workflows/*.yaml",
        ".github/actions/**/*.yml",
        ".github/actions/**/*.yaml",
        ".github/scripts/*.py",
        ".github/scripts/*.sh",
    ):
        files.extend(sorted(REPO_ROOT.glob(pattern)))
    return files


# A pkl release asset URL with the version spelled out inline — the shape every
# hand-rolled copy took before install-pkl existed.
_PKL_ASSET_LITERAL = re.compile(
    r"releases/download/(\d+\.\d+\.\d+)/pkl-",
)
# The pkl release URL, however the asset name is spelled. Anchors the
# "downloads pkl" test below, which must not depend on the asset literal.
_PKL_RELEASE_URL = "apple/pkl/releases/download/"

# A `PKL_VERSION: "x.y.z"` env or `default: "x.y.z"` on a pkl-version input.
_PKL_ENV_LITERAL = re.compile(r"PKL_VERSION\s*:\s*[\"'](\d+\.\d+\.\d+)[\"']")

# Files allowed to spell a pkl version out, mapped to the reason. Empty on
# purpose: after FND-1864 there is nowhere in CI the SoT cannot reach — both
# downloads interpolate a resolved version rather than a literal. The mechanism
# stays so a genuine future exception is recorded with its reason instead of
# quietly weakening the regexes (same idiom as test_artifact_upload_retry.py).
_EXEMPT: dict[str, str] = {}


@pytest.mark.parametrize("path", _ci_files(), ids=lambda p: str(p))
def test_no_ci_file_hardcodes_a_pkl_version(path: Path) -> None:
    rel = path.relative_to(REPO_ROOT).as_posix()
    text = path.read_text(encoding="utf-8")

    hits = sorted(
        set(_PKL_ASSET_LITERAL.findall(text) + _PKL_ENV_LITERAL.findall(text))
    )
    if not hits:
        return
    assert rel in _EXEMPT, (
        f"{rel} spells a pkl version out ({hits}). The pin is declared once, in "
        f"{pv.SOT_RELPATH}; install-pkl resolves it and every call site omits "
        f"`version:`. A second literal is how FND-1864 happened — six of them, "
        f"free to drift apart, and none of them readable by the apps whose "
        f"contracts they render. If this file genuinely cannot reach the SoT, add "
        f"it to _EXEMPT here with the reason."
    )


def test_no_install_pkl_call_site_pins_a_version() -> None:
    """Every ``install-pkl`` use omits ``version:``.

    A call site that passes one is not necessarily wrong — the input exists for
    deliberate divergence — but it is exactly how the fleet drifted, so it has
    to be an explicit, reviewed exception rather than the shape a copied step
    happens to have.
    """
    offenders: list[str] = []
    for path in _ci_files():
        if path.suffix not in (".yml", ".yaml"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"uses:\s*\S*install-pkl\S*\n(.*?)(?=\n\s*-\s|\Z)", text, re.S
        ):
            block = match.group(1)
            # Stop at the next step; only look at this step's `with:`.
            if re.search(r"^\s+version:\s*\S", block, re.M):
                # The freshness gate forwards its own workflow_call input, which
                # is itself empty by default — that is the pass-through, not a pin.
                if "inputs.pkl-version" in block:
                    continue
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, (
        "these install-pkl call sites pin a pkl version instead of taking the "
        f"SoT default ({pv.SOT_RELPATH}): {sorted(set(offenders))}"
    )


def test_every_pkl_download_also_verifies_the_runtime() -> None:
    """Anything that installs pkl must assert that PATH's pkl IS that version.

    Downloading the right asset is not the same as running it: a runner image
    or an earlier step can leave another pkl ahead of ``/usr/local/bin``, and
    the drivers that follow invoke bare ``pkl``. So a download without a
    ``check-runtime --strict`` reintroduces the FND-1864 skew on whichever path
    it sits on — which is exactly what happened: the verify was added to
    ``install-pkl`` and not to ``regenerate-contract``, whose download is a
    separate copy. Echoing ``pkl --version`` is logging, not verification, so
    this looks for the assertion specifically.

    The right long-term fix is one download for the whole repo; this guard is
    what makes the interim two-copy state safe, and it will simply keep passing
    once they are folded together.
    """
    # Anchor on the release path, NOT on a `/pkl-<asset>` fragment: both
    # downloads name the asset through `${PKL_ASSET}` (chosen from runner.arch
    # in the expression layer), so a pattern wanting the literal asset name
    # matches zero files and the guard silently passes forever. Caught by
    # stripping the verify step and watching this stay green.
    downloads = [
        p for p in _ci_files() if _PKL_RELEASE_URL in p.read_text(encoding="utf-8")
    ]
    assert downloads, (
        "no pkl download found in any CI file — this guard is only meaningful "
        f"if it matches something; has the URL {_PKL_RELEASE_URL!r} changed?"
    )
    offenders = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in downloads
        if "check-runtime" not in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "these files download pkl but never assert the pkl on PATH is the "
        f"version they resolved: {sorted(offenders)}. Add a "
        "`check-runtime --strict --expected <resolved>` step after the "
        "download (see .github/actions/install-pkl/action.yaml)."
    )


def test_pin_file_is_in_the_toolkit_path_filter() -> None:
    """A pin bump must run the toolkit generate-and-diff suite.

    ``contract-toolkit-reusable.yaml``'s "Verify generated output" job
    regenerates every ``contract-toolkit/examples/`` tree and fails on any diff
    — the only check that can prove a pkl bump changes no generated artifact.
    ``sdk-gate.yaml`` gates that whole reusable on its ``toolkit`` path filter,
    so if the pin file is not listed there, a bump is a one-line Python change
    that matches no filter and merges unverified.
    """
    gate = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/sdk-gate.yaml").read_text(encoding="utf-8")
    )
    filters = None
    for job in gate["jobs"].values():
        for step in job.get("steps", []) or []:
            if "paths-filter" in str(step.get("uses", "")):
                filters = yaml.safe_load(step["with"]["filters"])
    assert filters is not None, "no dorny/paths-filter step found in sdk-gate.yaml"
    assert pv.SOT_RELPATH in filters["toolkit"], (
        f"{pv.SOT_RELPATH} is missing from sdk-gate.yaml's `toolkit` filter, so a "
        f"pkl pin bump would not run contract-toolkit-reusable.yaml — the job that "
        f"proves the bump regenerates every example byte-identically."
    )
