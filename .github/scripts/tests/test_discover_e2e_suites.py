"""Tests for .github/actions/discover-e2e-suites/discover_e2e_suites.py.

The module under test lives under .github/actions/discover-e2e-suites/ (co-located
so it's checked out with the composite action in consumer repos); the test lives
here alongside the other action-script tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "actions" / "discover-e2e-suites")
)

from discover_e2e_suites import (
    DEFAULT_CLOUDS,
    discover,
    main,
    parse_clouds,
)


def _matrix(out: str) -> dict:
    line = next(ln for ln in out.splitlines() if ln.startswith("matrix="))
    return json.loads(line[len("matrix=") :])


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
    rc = main(["--test-dir", str(e2e), "--clouds", "none"])
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
    rc = main(["--test-dir", str(e2e), "--clouds", "none"])
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
    main(["--test-dir", str(e2e), "--clouds", "none"])
    assert "::warning::" not in capsys.readouterr().err


def test_main_count_zero_for_empty(capsys, tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    rc = main(["--test-dir", str(e2e)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "count=0" in out
    assert _matrix(out) == {"include": []}


# ── Cross-CSP cloud dimension (FND-6) ────────────────────────────────────────


def test_parse_clouds_orders_dedupes_and_drops_blanks() -> None:
    assert parse_clouds("aws,azure,gcp") == ["aws", "azure", "gcp"]
    # Caller order is preserved (not sorted), blanks and repeats are dropped.
    assert parse_clouds(" GCP , aws ,,aws, ") == ["gcp", "aws"]


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_clouds_means_the_default_list_not_none(raw: str) -> None:
    # An untouched GitHub input arrives as "". If that meant "no clouds", every
    # app repo forwarding the dispatch input would silently opt out of the
    # matrix — the exact regression this rounding prevents.
    assert parse_clouds(raw) == list(DEFAULT_CLOUDS)


@pytest.mark.parametrize("raw", ["none", "NONE", "  None  "])
def test_none_sentinel_disables_the_cloud_dimension(raw: str) -> None:
    assert parse_clouds(raw) == []


def test_default_clouds_is_the_three_csps() -> None:
    # Pinned so widening the fleet's default fan-out is a deliberate edit with a
    # failing test, not a drive-by.
    assert DEFAULT_CLOUDS == ("aws", "azure", "gcp")


def test_no_clouds_reproduces_legacy_entry_shape(tmp_path: Path) -> None:
    # The single-tenant fallback path must stay byte-identical: no `suite` or
    # `cloud` keys, so matrix.cloud is empty and the tenant resolver falls back.
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py")
    assert discover(str(e2e)) == discover(str(e2e), []) == discover(str(e2e), None)
    (entry,) = discover(str(e2e))
    assert set(entry) == {"file", "name"}


def test_clouds_cross_product_cardinality_and_order(tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py", "test_two.py")
    entries = discover(str(e2e), ["aws", "azure", "gcp"])
    # Suites outer, clouds inner.
    assert [e["name"] for e in entries] == [
        "one-aws",
        "one-azure",
        "one-gcp",
        "two-aws",
        "two-azure",
        "two-gcp",
    ]
    assert all(e["suite"] in {"one", "two"} for e in entries)
    assert {e["cloud"] for e in entries} == {"aws", "azure", "gcp"}


def test_clouds_leg_names_stay_unique_when_suite_names_collide(
    tmp_path: Path,
) -> None:
    # Both files sanitize to "a-b"; the de-dup suffix must survive the cross
    # product or two legs would collide on artifact name AND Temporal queue.
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_a_b.py", "test_a-b.py")
    names = [e["name"] for e in discover(str(e2e), ["aws", "gcp"])]
    assert len(names) == len(set(names)) == 4


def test_main_count_is_suites_and_leg_count_is_legs(capsys, tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py", "test_two.py")
    rc = main(["--test-dir", str(e2e), "--clouds", "aws,azure,gcp"])
    out = capsys.readouterr().out
    assert rc == 0
    # count stays the SUITE count — the caller's "requested but nothing found"
    # guard is about suites, not legs.
    assert "count=2" in out.splitlines()
    assert "leg-count=6" in out.splitlines()
    assert len(_matrix(out)["include"]) == 6


def test_main_logs_the_fan_out_explicitly(capsys, tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py")
    main(["--test-dir", str(e2e), "--clouds", "aws,azure,gcp"])
    err = capsys.readouterr().err
    assert "3 cloud(s)" in err
    assert "aws, azure, gcp" in err
    assert "3 leg(s)" in err


def test_main_zero_suites_with_clouds_still_counts_zero(capsys, tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    main(["--test-dir", str(e2e), "--clouds", "aws,azure,gcp"])
    out = capsys.readouterr().out
    assert "count=0" in out.splitlines()
    assert "leg-count=0" in out.splitlines()
    assert _matrix(out) == {"include": []}


def test_clouds_only_emits_cloud_dimension_without_files(
    capsys, tmp_path: Path
) -> None:
    # --test-dir is not read in this mode; point it at a populated dir to prove
    # the file dimension really is absent rather than coincidentally empty.
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py", "test_two.py")
    rc = main(["--test-dir", str(e2e), "--clouds", "aws,azure,gcp", "--clouds-only"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _matrix(out) == {
        "include": [
            {"cloud": "aws", "name": "aws"},
            {"cloud": "azure", "name": "azure"},
            {"cloud": "gcp", "name": "gcp"},
        ]
    }
    assert "count=3" in out.splitlines()
    assert "leg-count=3" in out.splitlines()


def test_clouds_only_with_none_is_empty(capsys, tmp_path: Path) -> None:
    # count=0 is what tells e2e-full-reusable.yaml to fall back to its
    # single-leg `{"include":[{}]}` matrix rather than running nothing.
    rc = main(["--test-dir", str(tmp_path), "--clouds", "none", "--clouds-only"])
    out = capsys.readouterr().out
    assert rc == 0
    assert _matrix(out) == {"include": []}
    assert "count=0" in out.splitlines()


def test_clouds_only_with_empty_uses_the_default_list(capsys, tmp_path: Path) -> None:
    main(["--test-dir", str(tmp_path), "--clouds", "", "--clouds-only"])
    out = capsys.readouterr().out
    assert [e["cloud"] for e in _matrix(out)["include"]] == list(DEFAULT_CLOUDS)
