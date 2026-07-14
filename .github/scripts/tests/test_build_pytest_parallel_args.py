"""Tests for .github/actions/sdr-e2e/build_pytest_parallel_args.py.

See test_build_compose_chain.py for why the module under test lives under
.github/actions/sdr-e2e/ (co-located so it is checked out with the composite
action in consumer repos) rather than .github/scripts/, and why the test
itself still lives here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "actions" / "sdr-e2e"))

from build_pytest_parallel_args import build_xdist_flags, main  # noqa: E402

# --- sequential (default) --------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_empty_is_sequential(value: str) -> None:
    assert build_xdist_flags(value) == ("", "")


# --- parallel --------------------------------------------------------------


def test_positive_int_enables_xdist() -> None:
    assert build_xdist_flags("2") == (
        "--with pytest-xdist",
        "-n 2 --dist loadscope",
    )


def test_auto_enables_xdist() -> None:
    assert build_xdist_flags("auto") == (
        "--with pytest-xdist",
        "-n auto --dist loadscope",
    )


def test_surrounding_whitespace_is_tolerated() -> None:
    assert build_xdist_flags("  3 ") == (
        "--with pytest-xdist",
        "-n 3 --dist loadscope",
    )


# --- rejected values -------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "01", "2.5", "two", "2 3", "auto; rm -rf /", "$(whoami)", "-n 4"],
)
def test_invalid_values_raise(value: str) -> None:
    with pytest.raises(ValueError):
        build_xdist_flags(value)


# --- CLI wrapper -----------------------------------------------------------


def test_main_emits_outputs_for_parallel(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--parallel-workers", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "uv_with=--with pytest-xdist" in out
    assert "xdist_args=-n 2 --dist loadscope" in out


def test_main_emits_empty_outputs_for_sequential(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--parallel-workers", ""])
    out = capsys.readouterr().out
    assert rc == 0
    assert "uv_with=\n" in out
    assert "xdist_args=\n" in out


def test_main_defaults_to_sequential(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "uv_with=\n" in out


def test_main_returns_error_on_invalid(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--parallel-workers", "0"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "::error::" in err
