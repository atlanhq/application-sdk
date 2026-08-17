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

from discover_e2e_suites import (  # noqa: E402
    DEFAULT_CLOUDS,
    CloudSelectionError,
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


def test_main_emits_the_resolved_cloud_list(capsys, tmp_path: Path) -> None:
    """The `clouds` output is what the scorecard records as observed coverage.

    It comes from the same `parse_clouds` call that built the matrix, so
    "what we recorded as covered" cannot disagree with "what ran" (FND-34).
    """
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py")
    main(["--test-dir", str(e2e), "--clouds", "aws,azure,gcp"])
    assert "clouds=aws,azure,gcp" in capsys.readouterr().out.splitlines()


def test_main_clouds_output_is_narrowed_like_the_matrix(capsys, tmp_path: Path) -> None:
    # Recording the REQUESTED list rather than the resolved one would report
    # coverage the run did not have — worse than reporting none.
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py")
    main(["--test-dir", str(e2e), "--clouds", "", "--available-clouds", "aws,gcp"])
    assert "clouds=aws,gcp" in capsys.readouterr().out.splitlines()


def test_main_clouds_output_is_empty_without_a_cloud_dimension(
    capsys, tmp_path: Path
) -> None:
    """The degraded single-tenant fallback records as "", not as absent.

    A consumer must be able to tell "ran against one legacy tenant" from "e2e
    never ran"; the latter is signalled by the field being omitted upstream.
    """
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py")
    main(["--test-dir", str(e2e), "--clouds", "none"])
    assert "clouds=" in capsys.readouterr().out.splitlines()


def test_clouds_only_also_emits_the_resolved_cloud_list(capsys, tmp_path: Path) -> None:
    # The scorecard job resolves `configured` through this mode, so the output
    # has to exist on both paths or the rollout signal is missing exactly where
    # it is read.
    rc = main(["--clouds", "aws,azure", "--clouds-only"])
    assert rc == 0
    assert "clouds=aws,azure" in capsys.readouterr().out.splitlines()


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


# ── Defaulted narrows to the secret's keys, named does not (FND-354) ─────────
#
# The two branches below are the whole of FND-354, and they are exactly the pair
# a later reader is liable to flatten into one — they "both just check the cloud
# list". They do the opposite thing on purpose:
#
#   defaulted + absent -> dropped with a warning. Nobody named it; DEFAULT_CLOUDS
#     did. Editing the secret is the only cloud-rotation lever that is fleet-wide
#     and needs no PR, so it has to narrow rather than red a leg in every repo.
#   named + absent     -> still emitted, so the per-leg resolver still exits
#     non-zero. Someone asserted that cloud should run; a silent skip there is a
#     coverage hole.
#
# Delete either and the lever breaks in one direction or the other, silently.


def test_a_defaulted_absent_cloud_is_dropped_with_a_warning(capsys) -> None:
    clouds = parse_clouds("", ["aws", "gcp"])
    assert clouds == ["aws", "gcp"]

    warning = capsys.readouterr().err
    assert "::warning::" in warning
    assert "azure" in warning, "the dropped cloud must be named, not just counted"
    assert "aws, gcp" in warning, "the surviving fan-out must be stated too"


def test_a_named_absent_cloud_still_reaches_the_resolver(capsys) -> None:
    # No narrowing and no warning: the per-leg tenant resolver is meant to fail
    # this leg, which is what makes an explicitly named cloud an assertion.
    assert parse_clouds("aws,azure", ["aws", "gcp"]) == ["aws", "azure"]
    assert capsys.readouterr().err == ""


def test_narrowing_is_silent_when_nothing_is_dropped(capsys) -> None:
    assert parse_clouds("", list(DEFAULT_CLOUDS)) == list(DEFAULT_CLOUDS)
    assert "::warning::" not in capsys.readouterr().err


@pytest.mark.parametrize("available", [None, [], [""], ["  "]])
def test_unknown_availability_narrows_nothing(available) -> None:
    # "" is what a skipped key-reading step emits — no secret shared with the
    # repo, or a payload that could not be parsed. Narrowing on that would turn
    # an unreadable secret into a silently smaller matrix.
    assert parse_clouds("", available) == list(DEFAULT_CLOUDS)


def test_an_extra_key_in_the_secret_does_not_widen_the_fan_out(capsys) -> None:
    # Intersection, never union. Adding a fourth CSP stays a reviewed edit to
    # DEFAULT_CLOUDS; a stray key in the secret must not fan out to a cloud the
    # SDK does not ship.
    assert parse_clouds("", ["aws", "azure", "gcp", "onprem"]) == list(DEFAULT_CLOUDS)
    assert "::warning::" not in capsys.readouterr().err


def test_availability_does_not_resurrect_the_none_sentinel() -> None:
    assert parse_clouds("none", ["aws", "azure", "gcp"]) == []


def test_narrowing_to_nothing_is_an_error_not_an_empty_matrix() -> None:
    # Zero legs would leave `count` (the SUITE count) non-zero, so the caller's
    # "requested but nothing found" guard would not fire and the gate would go
    # green having run no e2e at all.
    with pytest.raises(CloudSelectionError) as excinfo:
        parse_clouds("", ["onprem"])
    assert "onprem" in str(excinfo.value)


def test_main_narrows_the_defaulted_list_from_available_clouds(
    capsys, tmp_path: Path
) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py")
    rc = main(["--test-dir", str(e2e), "--clouds", "", "--available-clouds", "aws,gcp"])
    captured = capsys.readouterr()
    assert rc == 0
    assert [e["cloud"] for e in _matrix(captured.out)["include"]] == ["aws", "gcp"]
    assert "leg-count=2" in captured.out.splitlines()
    assert "::warning::" in captured.err


def test_main_does_not_narrow_an_explicit_list(capsys, tmp_path: Path) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py")
    rc = main(
        ["--test-dir", str(e2e), "--clouds", "azure", "--available-clouds", "aws,gcp"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert [e["cloud"] for e in _matrix(out)["include"]] == ["azure"]


def test_main_exits_non_zero_when_narrowing_empties_the_fan_out(
    capsys, tmp_path: Path
) -> None:
    e2e = tmp_path / "tests" / "e2e"
    _mk(e2e, "test_one.py")
    rc = main(["--test-dir", str(e2e), "--clouds", "", "--available-clouds", "onprem"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "::error::" in captured.err
    # Nothing may be written to $GITHUB_OUTPUT on the failing path, or the
    # caller reads a matrix from a run that errored.
    assert captured.out == ""


def test_clouds_only_narrows_the_same_way(capsys, tmp_path: Path) -> None:
    # prepare-tenant installs from this matrix; it must cover exactly the clouds
    # the legs run against, so the two call sites narrow identically.
    rc = main(
        ["--test-dir", str(tmp_path), "--clouds-only", "--available-clouds", "aws,gcp"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert [e["cloud"] for e in _matrix(out)["include"]] == ["aws", "gcp"]
    assert "count=2" in out.splitlines()
