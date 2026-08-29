"""Tests for the bounded-lock refusal alarm (FND-909).

Two things are under test here. The first is the alarm's own logic: which
classifications fail the run, which are merely reported, and that it stays quiet
on a healthy fleet.

The second is the vocabulary pin at the bottom. The driver that *writes* refusal
stamps and the classifier that *reads* them hold separate copies of the
self-healing set, because the driver runs as a bare `python3` on the fleet runner
and cannot import the conformance package. Separate copies can drift, and the way
they would drift is silent: add a self-healing reason to the writer alone and the
reader classifies it as a standing fault, so the alarm never fires and the freeze
is invisible again — exactly the FND-909 failure. The pin turns that into a red
test instead.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))

import renovate_refusal_alarm as alarm  # noqa: E402
import renovate_uv_lock_bounded as bounded  # noqa: E402

try:  # The vocabulary pin's other half — see _classifier() below.
    from conformance.renovate import classify as _conformance_classify
    from conformance.renovate.models import BlockingReason as _BlockingReason
except ImportError:  # pragma: no cover - depends on the runner's environment
    _conformance_classify = None  # type: ignore[assignment]
    _BlockingReason = None  # type: ignore[assignment]


def _write_repo_report(out_dir, slug: str, prs: list[dict]) -> None:
    """Write one repos/<slug>.json in the shape the renovate-scan CLI emits."""
    repos = out_dir / "repos"
    repos.mkdir(parents=True, exist_ok=True)
    (repos / f"{slug}.json").write_text(
        json.dumps({"repo": f"atlanhq/{slug}", "openPRs": prs}),
        encoding="utf-8",
    )


def _pr(number: int, reason: str, blocking: str, age: int = 1) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/atlanhq/example/pull/{number}",
        "blockingReason": blocking,
        "lockRefusalWindow": "P3D",
        "lockRefusalReason": reason,
        "ageDays": age,
    }


def test_clean_fleet_passes(tmp_path, capsys) -> None:
    _write_repo_report(tmp_path, "app-one", [_pr(1, "", "checks_failing")])

    assert alarm.main(["--out-dir", str(tmp_path)]) == 0
    assert "frozen self-healing refusals: 0" in capsys.readouterr().out


def test_frozen_refusal_fails_the_run(tmp_path, capsys) -> None:
    _write_repo_report(
        tmp_path, "app-one", [_pr(410, "window-empty", alarm.EXPIRED, age=2)]
    )

    assert alarm.main(["--out-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "app-one#410" in captured.out
    assert "outlived the reaper" in captured.err


def test_standing_fault_is_reported_but_never_fatal(tmp_path, capsys) -> None:
    """A wedge a human owns must not red a six-hourly job until they clear it."""
    _write_repo_report(
        tmp_path, "app-two", [_pr(77, "yanked-pin", alarm.STANDING, age=9)]
    )

    assert alarm.main(["--out-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "standing faults (human-owned, not alarmed): 1" in captured.out
    assert "::warning::" in captured.out
    assert captured.err == ""


def test_frozen_still_fails_when_a_standing_fault_is_also_present(tmp_path) -> None:
    """The quiet case must not suppress the loud one."""
    _write_repo_report(tmp_path, "app-one", [_pr(410, "window-empty", alarm.EXPIRED)])
    _write_repo_report(tmp_path, "app-two", [_pr(77, "rollback", alarm.STANDING)])

    assert alarm.main(["--out-dir", str(tmp_path)]) == 1


def test_offenders_names_the_repo_and_pr(tmp_path) -> None:
    """The aggregate carries counts alone; the alarm has to say where."""
    _write_repo_report(
        tmp_path, "app-one", [_pr(410, "window-empty", alarm.EXPIRED, age=3)]
    )

    found = alarm.offenders(str(tmp_path), alarm.EXPIRED)

    assert found == [
        {
            "repo": "atlanhq/app-one",
            "number": 410,
            "url": "https://github.com/atlanhq/example/pull/410",
            "window": "P3D",
            "reason": "window-empty",
            "ageDays": 3,
        }
    ]


def test_missing_output_directory_is_not_an_alarm(tmp_path) -> None:
    """A scanner that wrote nothing is a scanner failure, not a frozen refusal.

    The scanner step precedes this one and fails the job on its own if it breaks.
    Reporting a phantom freeze here would point the reader at the wrong system.
    """
    assert alarm.main(["--out-dir", str(tmp_path / "nonexistent")]) == 0


def test_malformed_repo_report_warns_and_keeps_going(tmp_path, capsys) -> None:
    """One corrupt file must not hide a real freeze in the next one."""
    repos = tmp_path / "repos"
    repos.mkdir(parents=True)
    (repos / "broken.json").write_text("{not json", encoding="utf-8")
    _write_repo_report(tmp_path, "app-one", [_pr(410, "window-empty", alarm.EXPIRED)])

    assert alarm.main(["--out-dir", str(tmp_path)]) == 1
    assert "unreadable repo report" in capsys.readouterr().err


def test_json_output_is_machine_readable(tmp_path, capsys) -> None:
    _write_repo_report(tmp_path, "app-one", [_pr(410, "window-empty", alarm.EXPIRED)])

    alarm.main(["--out-dir", str(tmp_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert [p["number"] for p in payload["frozen"]] == [410]
    assert payload["standing"] == []


# --- the writer/reader vocabulary pin -------------------------------------


def _classifier():
    """The conformance classifier, or a skip when it is not installed.

    Optional at import time: the CI-script suite is runnable on a bare
    interpreter, and these two tests are the only ones in it that need the
    package. Skipping keeps the rest working there while the pin still fires in
    any environment that has both — which the repo's own
    `uv sync --all-extras --all-groups` always does.
    """
    if _conformance_classify is None:  # pragma: no cover - depends on runner env
        pytest.skip("conformance package not installed in this environment")
    return _conformance_classify


def _classifier_models():
    """`BlockingReason`, or a skip when the package is not installed."""
    if _BlockingReason is None:  # pragma: no cover - depends on runner env
        pytest.skip("conformance package not installed in this environment")
    return _BlockingReason


def test_self_healing_vocabulary_matches_the_classifier() -> None:
    """The set the driver stamps and the set the reader trusts must be identical."""
    assert bounded.SELF_HEALING_REFUSALS == _classifier().SELF_HEALING_REFUSALS


def test_alarm_blocking_reasons_match_the_classifier_enum() -> None:
    """The alarm's literals must equal the enum values the scanner actually emits.

    Same drift as the self-healing set, one layer down and easier to miss: the
    alarm matches `blockingReason` as a bare string because it runs outside the
    uv environment and cannot import BlockingReason. Every other test in this
    file asserts against `alarm.EXPIRED` itself, so renaming the enum value would
    leave both suites green while the dashboard run went silent on a real freeze
    — the alarm would simply match nothing. This is the only assertion that
    would catch it.
    """
    reasons = _classifier_models()
    assert alarm.EXPIRED == reasons.BOUNDED_LOCK_REFUSAL_EXPIRED.value
    assert alarm.STANDING == reasons.BOUNDED_LOCK_REFUSAL_STANDING.value


def test_every_reason_the_driver_can_write_is_known_to_the_reader() -> None:
    """A reason outside the reader's vocabulary must still classify as standing.

    That is the safe direction — an unrecognised reason gets a human rather than
    a machine — but it is only safe because the reader treats "not self-healing"
    as standing rather than ignoring the stamp. Pin the whole writable set so a
    new refusal path cannot land without someone reading this.

    The writable set is collected from the driver module rather than hand-listed:
    a literal set would silently omit a newly added REFUSAL_* constant, which is
    precisely the case this test exists to notice.
    """
    classify = _classifier()
    writable = {
        getattr(bounded, name)
        for name in dir(bounded)
        if name.startswith("REFUSAL_") and isinstance(getattr(bounded, name), str)
    }
    # Guard the collection itself: a rename of the REFUSAL_* prefix would empty
    # the set and make every assertion below vacuously true.
    assert len(writable) == 5, f"unexpected refusal constants: {sorted(writable)}"
    standing = writable - classify.SELF_HEALING_REFUSALS

    assert standing == {
        "no-packaging",
        "unsatisfiable-floor",
        "floor-admitted-still-failed",
        "rollback",
    }
