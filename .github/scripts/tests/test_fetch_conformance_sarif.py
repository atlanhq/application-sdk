"""Tests for .github/scripts/fetch_conformance_sarif.py.

The regression these guard is the hardcoded 7-series download loop this script
replaced: the conformance suite grew to 10 series, the loop kept asking for its
own 7, and every per-repo dashboard summary was silently built from 6 of them.
`test_downloads_every_series_the_run_published` is that case, using the real
artifact names from the hive run that exposed it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import fetch_conformance_sarif as fcs

# The 10 series a real Conformance run published on 2026-08-10 (hive). The old
# loop knew only: ci, error-handling, prescriptions, optimizations, dependency,
# logging, tests — note "dependency" is not here at all, and 4 of these are new.
LIVE_SERIES = [
    "deprecation",
    "error-handling",
    "logging",
    "tests",
    "container-image",
    "ci",
    "contract-toolkit",
    "prescriptions",
    "security",
    "optimizations",
]


def _artifacts(names_expired: list[tuple[str, bool]]) -> str:
    return json.dumps(
        {"artifacts": [{"name": n, "expired": e} for n, e in names_expired]}
    )


class FakeGh:
    """Records argv and replays canned (rc, stdout) per gh subcommand."""

    def __init__(
        self, runs="[]", artifacts_by_run=None, download_rc=0, head=None, head_rc=0
    ):
        self.runs = runs
        self.artifacts_by_run = artifacts_by_run or {}
        self.download_rc = download_rc
        self.head = head or {"headSha": "abc123", "headBranch": "main"}
        self.head_rc = head_rc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(args)
        if args[:2] == ["run", "list"]:
            return 0, self.runs
        if args[0] == "api":
            run_id = args[1].split("/")[-2]
            payload = self.artifacts_by_run.get(int(run_id))
            if payload is None:
                return 1, ""
            return 0, payload
        if args[:2] == ["run", "download"]:
            rc = self.download_rc
            if callable(rc):
                rc = rc(args)
            return rc, ""
        if args[:2] == ["run", "view"]:
            if self.head_rc != 0:
                return self.head_rc, ""
            return 0, json.dumps(self.head)
        raise AssertionError(f"unexpected gh call: {args}")

    def downloaded_names(self) -> list[str]:
        return [
            c[c.index("--name") + 1] for c in self.calls if c[:2] == ["run", "download"]
        ]


# --------------------------------------------------------------------------
# series_name / live_sarif_series
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "artifact,expected",
    [
        ("conformance-ci-sarif", "ci"),
        ("conformance-error-handling-sarif", "error-handling"),
        ("conformance-container-image-sarif", "container-image"),
        ("unit-test-coverage", None),
        ("conformance-ci", None),
        ("ci-sarif", None),
        ("conformance--sarif", None),
        ("", None),
    ],
)
def test_series_name_parses_only_the_contract(artifact, expected):
    assert fcs.series_name(artifact) == expected


def test_live_sarif_series_skips_expired_and_foreign_artifacts():
    payload = json.loads(
        _artifacts(
            [
                ("conformance-ci-sarif", False),
                ("conformance-security-sarif", True),  # expired
                ("unit-test-coverage", False),  # not SARIF
                ("conformance-logging-sarif", False),
            ]
        )
    )
    assert fcs.live_sarif_series(payload) == ["ci", "logging"]


def test_live_sarif_series_dedupes():
    payload = json.loads(
        _artifacts([("conformance-ci-sarif", False), ("conformance-ci-sarif", False)])
    )
    assert fcs.live_sarif_series(payload) == ["ci"]


def test_live_sarif_series_handles_empty_and_null_payloads():
    assert fcs.live_sarif_series({}) == []
    assert fcs.live_sarif_series({"artifacts": None}) == []


# --------------------------------------------------------------------------
# candidate_runs
# --------------------------------------------------------------------------


def test_candidate_runs_keeps_failures_but_drops_cancelled():
    payload = [
        {"databaseId": 3, "conclusion": "success"},
        {"databaseId": 2, "conclusion": "cancelled"},
        {"databaseId": 1, "conclusion": "failure"},
        {"databaseId": None, "conclusion": "success"},
    ]
    # failure is kept on purpose: a Conformance run that reports findings exits
    # non-zero but still uploads its SARIF.
    assert fcs.candidate_runs(payload) == [3, 1]


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------


def test_discover_falls_back_to_an_older_run_when_newest_has_no_live_sarif():
    gh = FakeGh(
        runs=json.dumps(
            [
                {"databaseId": 99, "conclusion": "success"},
                {"databaseId": 98, "conclusion": "failure"},
            ]
        ),
        artifacts_by_run={
            99: _artifacts([("conformance-ci-sarif", True)]),  # all expired
            98: _artifacts([("conformance-ci-sarif", False)]),
        },
    )
    run_id, series, error = fcs.discover(
        "atlanhq/x", "conformance.yaml", "main", 20, gh=gh
    )
    assert (run_id, series, error) == (98, ["ci"], False)


def test_discover_returns_nothing_when_no_run_has_live_sarif():
    gh = FakeGh(
        runs=json.dumps([{"databaseId": 99, "conclusion": "success"}]),
        artifacts_by_run={99: _artifacts([("unit-test-coverage", False)])},
    )
    assert fcs.discover("atlanhq/x", "conformance.yaml", "main", 20, gh=gh) == (
        None,
        [],
        False,
    )


def test_discover_survives_an_unlistable_run():
    gh = FakeGh(
        runs=json.dumps(
            [
                {"databaseId": 99, "conclusion": "success"},
                {"databaseId": 98, "conclusion": "success"},
            ]
        ),
        artifacts_by_run={98: _artifacts([("conformance-tests-sarif", False)])},
    )  # 99 missing -> rc 1, but 98 lists fine -> not an error
    assert fcs.discover("atlanhq/x", "conformance.yaml", "main", 20, gh=gh) == (
        98,
        ["tests"],
        False,
    )


# --------------------------------------------------------------------------
# discover: error signal
# --------------------------------------------------------------------------


class _FailingGh:
    """Every gh call fails — simulates a transport/auth outage."""

    def __call__(self, args: list[str]):
        return 1, ""


def test_discover_flags_error_when_run_list_fails():
    assert fcs.discover(
        "atlanhq/x", "conformance.yaml", "main", 20, gh=_FailingGh()
    ) == (
        None,
        [],
        True,
    )


def test_discover_flags_error_when_run_list_is_unparseable():
    gh = FakeGh(runs="not json")
    assert fcs.discover("atlanhq/x", "conformance.yaml", "main", 20, gh=gh) == (
        None,
        [],
        True,
    )


def test_discover_flags_error_when_every_probe_fails():
    # Two candidates, neither has a retrievable artifact listing -> operational
    # fault, not "nothing to publish".
    gh = FakeGh(
        runs=json.dumps(
            [
                {"databaseId": 99, "conclusion": "success"},
                {"databaseId": 98, "conclusion": "success"},
            ]
        ),
        artifacts_by_run={},  # both runs unlistable -> rc 1 each
    )
    assert fcs.discover("atlanhq/x", "conformance.yaml", "main", 20, gh=gh) == (
        None,
        [],
        True,
    )


def test_discover_no_error_when_a_probe_succeeds_with_zero_live_sarif():
    # Newest run unlistable, older run lists fine but has no SARIF -> the API is
    # healthy; this is the routine empty case, not an error.
    gh = FakeGh(
        runs=json.dumps(
            [
                {"databaseId": 99, "conclusion": "success"},
                {"databaseId": 98, "conclusion": "success"},
            ]
        ),
        artifacts_by_run={98: _artifacts([("conformance-ci-sarif", True)])},  # expired
    )
    assert fcs.discover("atlanhq/x", "conformance.yaml", "main", 20, gh=gh) == (
        None,
        [],
        False,
    )


# --------------------------------------------------------------------------
# end-to-end via main()
# --------------------------------------------------------------------------


def _run_main(gh, tmp_path, monkeypatch, extra=None):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    rc = fcs.main(
        ["--repo", "atlanhq/atlan-hive-app", "--dir", str(tmp_path / "sarif")]
        + (extra or []),
        gh=gh,
    )
    parsed = {}
    if out.exists():
        for line in out.read_text().splitlines():
            if line:
                k, _, v = line.partition("=")
                parsed[k] = v
    return rc, parsed


def test_downloads_every_series_the_run_published(tmp_path, monkeypatch):
    """The 7-vs-10 regression: all live series must be fetched, none assumed."""
    gh = FakeGh(
        runs=json.dumps([{"databaseId": 31363142757, "conclusion": "success"}]),
        artifacts_by_run={
            31363142757: _artifacts(
                [(f"conformance-{s}-sarif", False) for s in LIVE_SERIES]
            )
        },
    )
    rc, out = _run_main(gh, tmp_path, monkeypatch)

    assert rc == 0
    assert out["has_artifacts"] == "true"
    assert sorted(out["series"].split()) == sorted(LIVE_SERIES)
    assert sorted(gh.downloaded_names()) == sorted(
        f"conformance-{s}-sarif" for s in LIVE_SERIES
    )
    # The four series the old hardcoded loop dropped on the floor.
    for missed in ("deprecation", "container-image", "contract-toolkit", "security"):
        assert f"conformance-{missed}-sarif" in gh.downloaded_names()
    # ...and the one it asked for that no longer exists.
    assert "conformance-dependency-sarif" not in gh.downloaded_names()


def test_partial_download_still_publishes_what_landed(tmp_path, monkeypatch):
    def rc_for(args):
        return 1 if "conformance-security-sarif" in args else 0

    gh = FakeGh(
        runs=json.dumps([{"databaseId": 5, "conclusion": "success"}]),
        artifacts_by_run={
            5: _artifacts(
                [("conformance-ci-sarif", False), ("conformance-security-sarif", False)]
            )
        },
        download_rc=rc_for,
    )
    rc, out = _run_main(gh, tmp_path, monkeypatch)
    assert rc == 0
    assert out["has_artifacts"] == "true"
    assert out["series"] == "ci"


def test_no_run_is_a_clean_skip_not_a_failure(tmp_path, monkeypatch):
    gh = FakeGh(runs="[]")
    rc, out = _run_main(gh, tmp_path, monkeypatch)
    assert rc == 0
    assert out == {
        "has_run": "false",
        "has_artifacts": "false",
        "discovery_error": "false",
    }


def test_discovery_failure_sets_discovery_error_output(tmp_path, monkeypatch):
    """An operational fault (run list fails) must surface as discovery_error=true
    while still exiting 0 — the workflow warns loudly without failing the
    best-effort publish red."""
    rc, out = _run_main(_FailingGh(), tmp_path, monkeypatch)
    assert rc == 0
    assert out["has_run"] == "false"
    assert out["has_artifacts"] == "false"
    assert out["discovery_error"] == "true"


def test_all_downloads_failing_is_a_clean_skip(tmp_path, monkeypatch):
    gh = FakeGh(
        runs=json.dumps([{"databaseId": 5, "conclusion": "success"}]),
        artifacts_by_run={5: _artifacts([("conformance-ci-sarif", False)])},
        download_rc=1,
    )
    rc, out = _run_main(gh, tmp_path, monkeypatch)
    assert rc == 0
    assert out["has_run"] == "true"
    assert out["has_artifacts"] == "false"
    assert "series" not in out


def test_head_sha_and_branch_are_published(tmp_path, monkeypatch):
    gh = FakeGh(
        runs=json.dumps([{"databaseId": 5, "conclusion": "success"}]),
        artifacts_by_run={5: _artifacts([("conformance-ci-sarif", False)])},
        head={"headSha": "0be829be", "headBranch": "main"},
    )
    _, out = _run_main(gh, tmp_path, monkeypatch)
    assert out["commit_sha"] == "0be829be"
    assert out["branch"] == "main"
    assert out["run_id"] == "5"


def test_failed_run_view_aborts_instead_of_publishing_blank_provenance(
    tmp_path, monkeypatch
):
    """Regression: the prior shell ran under ``set -euo pipefail``, so a failed
    ``gh run view`` on an already-confirmed run aborted the step. Publishing
    with empty ``commit_sha``/``branch`` would bake blank provenance into the
    dashboard JSON — the lookup failure must propagate as a nonzero exit."""
    gh = FakeGh(
        runs=json.dumps([{"databaseId": 5, "conclusion": "success"}]),
        artifacts_by_run={5: _artifacts([("conformance-ci-sarif", False)])},
        head_rc=1,
    )
    rc, out = _run_main(gh, tmp_path, monkeypatch)
    assert rc == 1
    # Nothing is published: no blank commit_sha/branch rows reach the dashboard.
    assert "commit_sha" not in out
    assert "branch" not in out


@pytest.mark.parametrize(
    "head",
    [
        {"headSha": None, "headBranch": None},  # null fields
        {"headSha": "", "headBranch": ""},  # empty strings
        {"headSha": "0be829be"},  # headBranch missing entirely
    ],
)
def test_blank_run_view_fields_abort_instead_of_publishing_blank_provenance(
    head, tmp_path, monkeypatch
):
    """Regression: a ``gh run view`` that exits 0 with valid JSON but missing/
    null/empty ``headSha``/``headBranch`` must also abort — returning ``("", "")``
    here would publish the same blank provenance the rc!=0 guard rejects."""
    gh = FakeGh(
        runs=json.dumps([{"databaseId": 5, "conclusion": "success"}]),
        artifacts_by_run={5: _artifacts([("conformance-ci-sarif", False)])},
        head=head,
    )
    rc, out = _run_main(gh, tmp_path, monkeypatch)
    assert rc == 1
    assert "commit_sha" not in out
    assert "branch" not in out


def test_artifacts_land_in_the_requested_dir(tmp_path, monkeypatch):
    gh = FakeGh(
        runs=json.dumps([{"databaseId": 5, "conclusion": "success"}]),
        artifacts_by_run={5: _artifacts([("conformance-ci-sarif", False)])},
    )
    _run_main(gh, tmp_path, monkeypatch)
    dl = [c for c in gh.calls if c[:2] == ["run", "download"]][0]
    assert dl[dl.index("--dir") + 1] == str(tmp_path / "sarif")
    assert (tmp_path / "sarif").is_dir()
