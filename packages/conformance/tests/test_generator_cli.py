"""Tests for ``conformance.tools._generator``, the CLI the committed-data
generators share (FND-4946)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from conformance.tools._generator import Generated, GeneratorSpec, run_generator

_MARKER = ("pkg", "__init__.py")


def _spec(outfile: Path, content: str = "data\n") -> GeneratorSpec:
    return GeneratorSpec(
        doc="",
        description="test generator",
        command="gen-test",
        outfile=outfile,
        noun="table",
        title="Test table",
        build=lambda root: (
            Generated(content, f"from {root.name}", "1 row")
            if root is not None
            else Generated(content, "no root", "1 row")
        ),
        sdk_marker=_MARKER,
        sdk_marker_label="pkg/",
    )


def _sdk_root(tmp_path: Path) -> Path:
    root = tmp_path / "sdk"
    root.joinpath(*_MARKER).parent.mkdir(parents=True)
    root.joinpath(*_MARKER).write_text("", encoding="utf-8")
    return root


def test_writes_the_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out" / "table.json"
    run_generator(_spec(out), ["--sdk-root", str(_sdk_root(tmp_path))])
    assert out.read_text(encoding="utf-8") == "data\n"
    assert capsys.readouterr().out == f"Wrote {out} (1 row).\n"


def test_check_passes_when_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "table.json"
    out.write_text("data\n", encoding="utf-8")
    run_generator(_spec(out), ["--sdk-root", str(_sdk_root(tmp_path)), "--check"])
    assert capsys.readouterr().out == "Test table up-to-date (from sdk).\n"


@pytest.mark.parametrize("committed", [None, "old\n"])
def test_check_fails_when_missing_or_stale(
    tmp_path: Path, committed: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "table.json"
    if committed is not None:
        out.write_text(committed, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        run_generator(_spec(out), ["--sdk-root", str(_sdk_root(tmp_path)), "--check"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    if committed is None:
        assert err == f"MISSING: {out}\n"
    else:
        assert err.startswith(f"STALE: {out}\n")
        assert "gen-test" in err
    if committed is not None:
        assert out.read_text(encoding="utf-8") == committed


def test_exits_2_when_sdk_root_lacks_the_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        run_generator(_spec(tmp_path / "t.json"), ["--sdk-root", str(tmp_path)])
    assert exc.value.code == 2
    assert "could not locate pkg/" in capsys.readouterr().err


def test_no_sdk_root_option_without_a_marker(tmp_path: Path) -> None:
    out = tmp_path / "t.json"
    spec = GeneratorSpec(
        doc="",
        description="d",
        command="gen-x",
        outfile=out,
        noun="table",
        title="Table",
        build=lambda root: Generated(f"{root}\n", "", ""),
    )
    with pytest.raises(SystemExit):
        run_generator(spec, ["--sdk-root", str(tmp_path)])
    run_generator(spec, [])
    assert out.read_text(encoding="utf-8") == "None\n"


def test_accepts_sdk_root_widens_an_explicit_root(tmp_path: Path) -> None:
    """``gen-deprecations`` accepts any root holding ``application_sdk/``,
    with or without ``__init__.py``, as it did before the shared CLI."""
    root = tmp_path / "sdk"
    (root / "pkg").mkdir(parents=True)
    out = tmp_path / "t.json"
    spec = dataclasses.replace(
        _spec(out), accepts_sdk_root=lambda r: (r / "pkg").is_dir()
    )
    run_generator(spec, ["--sdk-root", str(root)])
    assert out.read_text(encoding="utf-8") == "data\n"


def test_gen_deprecations_accepts_a_root_without_init(tmp_path: Path) -> None:
    from conformance.tools.generate_deprecations import SPEC

    assert SPEC.accepts_sdk_root is not None
    (tmp_path / "application_sdk").mkdir()
    assert SPEC.accepts_sdk_root(tmp_path)
    assert not SPEC.accepts_sdk_root(tmp_path / "missing")
