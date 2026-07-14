"""Tests for .github/actions/discover-e2e-suites/discover_e2e_suites.py.

The module under test lives under .github/actions/discover-e2e-suites/ (co-located
so it's checked out with the composite action in consumer repos); the test lives
here alongside the other action-script tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "actions" / "discover-e2e-suites")
)

from discover_e2e_suites import discover, main  # noqa: E402


def _mk(dir_: Path, *names: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    for n in names:
        (dir_ / n).write_text("")


def test_discovers_test_files_sorted(tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_openapi_reuse_e2e.py", "test_openapi_e2e.py")
    entries = discover(str(e2e))
    assert [e["file"] for e in entries] == [
        (e2e / "test_openapi_e2e.py").as_posix(),
        (e2e / "test_openapi_reuse_e2e.py").as_posix(),
    ]


def test_leg_name_strips_test_prefix_and_suffix(tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_openapi_reuse_e2e.py")
    (name,) = (e["name"] for e in discover(str(e2e)))
    assert name == "openapi-reuse-e2e"


def test_ignores_non_test_and_non_py(tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_a.py", "conftest.py", "helpers.py", "test_b.txt", "__init__.py")
    assert {e["name"] for e in discover(str(e2e))} == {"a"}


def test_deduplicates_colliding_leg_names(tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    # Both sanitize to "a-b"; second must get a numeric suffix so artifact
    # names stay unique across legs.
    _mk(e2e, "test_a_b.py", "test_a-b.py")
    names = [e["name"] for e in discover(str(e2e))]
    assert len(set(names)) == len(names) == 2
    assert "a-b" in names


def test_empty_dir_yields_no_entries(tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    assert discover(str(e2e)) == []


def test_missing_dir_yields_no_entries(tmp_path: Path) -> None:
    assert discover(str(tmp_path / "does" / "not" / "exist")) == []


def test_main_emits_matrix_and_count(capsys, tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py", "test_two.py")
    rc = main(["--test-dir", str(e2e)])
    out = capsys.readouterr().out
    assert rc == 0
    matrix_line = next(ln for ln in out.splitlines() if ln.startswith("matrix="))
    payload = json.loads(matrix_line[len("matrix=") :])
    assert [e["name"] for e in payload["include"]] == ["one", "two"]
    assert "count=2" in out


def test_main_warns_on_nested_suites(capsys, tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_flat.py")
    _mk(e2e / "sub", "test_nested.py")
    rc = main(["--test-dir", str(e2e)])
    captured = capsys.readouterr()
    assert rc == 0
    # Flat suite is in the matrix; nested one is NOT, but is warned about.
    matrix_line = next(
        ln for ln in captured.out.splitlines() if ln.startswith("matrix=")
    )
    payload = json.loads(matrix_line[len("matrix=") :])
    assert [e["name"] for e in payload["include"]] == ["flat"]
    assert "::warning::" in captured.err
    assert "test_nested.py" in captured.err


def test_main_no_warning_when_flat_only(capsys, tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_a.py", "test_b.py")
    main(["--test-dir", str(e2e)])
    assert "::warning::" not in capsys.readouterr().err


def test_main_count_zero_for_empty(capsys, tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    rc = main(["--test-dir", str(e2e)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "count=0" in out
    matrix_line = next(ln for ln in out.splitlines() if ln.startswith("matrix="))
    assert json.loads(matrix_line[len("matrix=") :]) == {"include": []}
