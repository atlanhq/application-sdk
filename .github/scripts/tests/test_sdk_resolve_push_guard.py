"""Tests for the SDK-resolve push guard.

The regression these pin: the resolver pushing inside the `@sdk-review`
trigger -> verdict window, which moves HEAD out from under the review in flight
so `sdk_review_approve.py` discards the verdict as stale ("head moved past the
verdict") and the round produces nothing.

Both directions matter and both are covered. A guard that holds too little
lets the race back in; a guard that holds too much (a declined trigger, a dead
reviewer sandbox, an unreadable API) leaves the resolver unable to push at all,
which is the worse failure — so every fail-open path has a case.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "sdk_resolve_push_guard",
    Path(__file__).resolve().parents[1] / "sdk_resolve_push_guard.py",
)
guard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["sdk_resolve_push_guard"] = guard
SPEC.loader.exec_module(guard)


REPO = "atlanhq/application-sdk"
PR = "3276"

NOW = datetime(2026, 8, 19, 18, 0, 0, tzinfo=timezone.utc)


def at(**delta: float) -> str:
    """An API timestamp offset from NOW, e.g. `at(minutes=-5)`."""
    return (NOW + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def clock() -> datetime:
    """A frozen clock. Fine for `check`, which reads the clock exactly once."""
    return NOW


class FakeTime:
    """A clock that advances only when something sleeps.

    Stepping per clock *read* would be wrong: `wait_until_clear` reads the clock
    more than once per poll, so a per-read step blows the whole budget on the
    first iteration and the loop never sleeps at all. Advancing on sleep is both
    realistic and the thing the loop's budget arithmetic is actually about.
    """

    def __init__(self) -> None:
        self.now = NOW
        self.slept: list[float] = []

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def trigger(
    created_at: str,
    *,
    declined: bool = False,
    login: str = "atlan-ci",
    comment_id: int = 1,
) -> dict:
    return {
        "id": comment_id,
        "body": "@sdk-review",
        "created_at": created_at,
        "user": {"login": login},
        "reactions": {"eyes": 1, "confused": 1 if declined else 0},
    }


def verdict(
    created_at: str,
    *,
    login: str = "mothership-ai[bot]",
    marker: str = "<!-- SDK_REVIEW -->",
    answers: int | None = None,
) -> dict:
    """A verdict comment. `answers` stamps the trigger id it correlates to."""
    stamp = f"<!-- ANSWERS_TRIGGER: {answers} -->\n" if answers is not None else ""
    return {
        "id": 900 + (answers or 0),
        "body": f"{marker}\n<!-- VERDICT: READY_TO_MERGE -->\n{stamp}## SDK Review\n",
        "created_at": created_at,
        "user": {"login": login},
        "reactions": {},
    }


def ok(payload: object) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(payload), stderr=""
    )


def runner_for(payload: object) -> guard.Runner:
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        return ok(payload)

    return run


# --- assess: holds --------------------------------------------------------


def test_unanswered_trigger_holds_the_push():
    in_flight, reason, _ = guard.assess([trigger(at(minutes=-5))], NOW)
    assert in_flight
    assert reason == "review-in-flight"


def test_verdict_older_than_the_trigger_still_holds():
    """The exact shape of the race: a prior round's verdict, a fresh trigger."""
    comments = [verdict(at(minutes=-30)), trigger(at(minutes=-4))]
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert in_flight
    assert reason == "review-in-flight"


def test_hold_message_names_the_stale_verdict_consequence():
    _, _, message = guard.assess([trigger(at(minutes=-5))], NOW)
    assert "300s" in message
    assert "stale" in message


# --- assess: clears -------------------------------------------------------


def test_no_trigger_is_clear():
    in_flight, reason, _ = guard.assess([], NOW)
    assert not in_flight
    assert reason == "no-trigger"


def test_answered_trigger_is_clear():
    comments = [trigger(at(minutes=-20)), verdict(at(minutes=-1))]
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert not in_flight
    assert reason == "verdict-answered"


def test_legacy_marker_counts_as_an_answer():
    comments = [
        trigger(at(minutes=-20)),
        verdict(at(minutes=-1), marker="<!-- TEST_SDK_REVIEW -->"),
    ]
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert not in_flight
    assert reason == "verdict-answered"


def test_declined_trigger_is_clear():
    """The gate reacts 😕 and posts nothing; without reading it this wedges."""
    in_flight, reason, _ = guard.assess([trigger(at(minutes=-5), declined=True)], NOW)
    assert not in_flight
    assert reason == "trigger-declined"


def test_stale_trigger_is_clear():
    """A reviewer sandbox that died must not hold the push indefinitely."""
    in_flight, reason, _ = guard.assess([trigger(at(minutes=-41))], NOW)
    assert not in_flight
    assert reason == "trigger-stale"


def test_stale_cap_is_configurable():
    comments = [trigger(at(minutes=-5))]
    assert guard.assess(comments, NOW, max_inflight_seconds=60)[1] == "trigger-stale"
    assert (
        guard.assess(comments, NOW, max_inflight_seconds=600)[1] == "review-in-flight"
    )


# --- round correlation ----------------------------------------------------


def test_older_rounds_late_verdict_does_not_clear_the_new_round():
    """The regression this correlation exists for.

    Round 1 triggers, the resolver's 40-min wait elapses, a new round triggers
    (a human re-tag, or a reviewer sandbox that outlived the wait — it runs up
    to 2h). Round 1's verdict then lands AFTER round 2's trigger. By timestamp
    alone it reads as round 2's answer, the guard clears, the push lands, and
    the review still running has its verdict stranded as stale — the exact bug
    this script exists to prevent, one round further along.
    """
    comments = [
        trigger(at(minutes=-50), comment_id=1),
        trigger(at(minutes=-5), comment_id=2),
        verdict(at(minutes=-1), answers=1),
    ]
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert in_flight
    assert reason == "review-in-flight"


def test_correlated_verdict_for_the_current_round_clears():
    comments = [
        trigger(at(minutes=-50), comment_id=1),
        trigger(at(minutes=-5), comment_id=2),
        verdict(at(minutes=-30), answers=1),
        verdict(at(minutes=-1), answers=2),
    ]
    in_flight, reason, message = guard.assess(comments, NOW)
    assert not in_flight
    assert reason == "verdict-answered"
    assert "correlated" in message


def test_unstamped_verdict_still_answers_by_timestamp():
    """Verdicts predating the marker, and workflow_dispatch reviews, keep working.

    A missing stamp must degrade to the old rule, not to a 40-minute false hold.
    """
    comments = [trigger(at(minutes=-20), comment_id=7), verdict(at(minutes=-1))]
    in_flight, reason, message = guard.assess(comments, NOW)
    assert not in_flight
    assert reason == "verdict-answered"
    assert "by timestamp" in message


def test_a_stamped_verdict_for_another_round_does_not_block_an_unstamped_answer():
    """Mixed state: the reviewer stamped one round and omitted it on the next."""
    comments = [
        trigger(at(minutes=-50), comment_id=1),
        trigger(at(minutes=-5), comment_id=2),
        verdict(at(minutes=-30), answers=1),
        verdict(at(minutes=-1)),
    ]
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert not in_flight
    assert reason == "verdict-answered"


def test_stamped_verdict_older_than_the_round_it_names_still_answers_it():
    """Correlation beats ordering: the id match is authoritative, not the clock."""
    comments = [
        trigger(at(minutes=-5), comment_id=2),
        verdict(at(minutes=-4), answers=2),
    ]
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert not in_flight
    assert reason == "verdict-answered"


def test_answers_trigger_is_extracted():
    body = "<!-- SDK_REVIEW -->\n<!-- ANSWERS_TRIGGER: 3312445566 -->\n"
    assert guard.extract_answers_trigger(body) == "3312445566"


def test_missing_answers_trigger_returns_none():
    assert guard.extract_answers_trigger("<!-- SDK_REVIEW -->") is None


def test_empty_answers_trigger_is_not_a_correlation():
    """The reviewer's sed deletes an empty marker; if one slips through, ignore it.

    Reading `ANSWERS_TRIGGER:` with no digits as "names a round, not yours" would
    hold every push for the full stale window on a marker that says nothing.
    """
    assert guard.extract_answers_trigger("<!-- ANSWERS_TRIGGER:  -->") is None
    comments = [trigger(at(minutes=-20), comment_id=7)]
    comments.append(verdict(at(minutes=-1)))
    comments[-1]["body"] = "<!-- SDK_REVIEW -->\n<!-- ANSWERS_TRIGGER:  -->\n"
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert not in_flight
    assert reason == "verdict-answered"


# --- burst collapsing -----------------------------------------------------


def test_a_duplicate_burst_is_one_round():
    """Two triggers seconds apart get ONE verdict; two rounds would never clear.

    The reviewer's dedupe guard answers a burst once, stamping whichever comment
    id won. Counting the burst as two rounds would leave one permanently
    unanswered and hold every later push for the full stale window.
    """
    comments = [
        trigger(at(minutes=-20), comment_id=1),
        trigger(at(minutes=-20, seconds=11), comment_id=2),
        verdict(at(minutes=-1), answers=1),
    ]
    assert len(guard.logical_rounds(comments)) == 1
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert not in_flight
    assert reason == "verdict-answered"


def test_triggers_beyond_the_burst_window_are_separate_rounds():
    comments = [
        trigger(at(minutes=-20), comment_id=1),
        trigger(at(minutes=-5), comment_id=2),
    ]
    assert len(guard.logical_rounds(comments)) == 2


def test_burst_window_is_configurable():
    comments = [
        trigger(at(minutes=-20), comment_id=1),
        trigger(at(minutes=-19), comment_id=2),
    ]
    assert len(guard.logical_rounds(comments, burst_window_seconds=30)) == 2
    assert len(guard.logical_rounds(comments, burst_window_seconds=120)) == 1


def test_a_burst_is_declined_only_when_every_trigger_was():
    """One accepted trigger in the burst means a review ran."""
    mixed = [
        trigger(at(minutes=-5), comment_id=1, declined=True),
        trigger(at(minutes=-5, seconds=11), comment_id=2),
    ]
    assert not guard.logical_rounds(mixed)[0].declined
    assert guard.assess(mixed, NOW)[0]

    both = [
        trigger(at(minutes=-5), comment_id=1, declined=True),
        trigger(at(minutes=-5, seconds=11), comment_id=2, declined=True),
    ]
    assert guard.logical_rounds(both)[0].declined
    assert guard.assess(both, NOW)[1] == "trigger-declined"


def test_round_age_is_measured_from_its_last_trigger():
    comments = [
        trigger(at(minutes=-41), comment_id=1),
        trigger(at(minutes=-40, seconds=-30), comment_id=2),
    ]
    # One round (30s apart); its last trigger is 2430s old, past the 2400 cap.
    assert len(guard.logical_rounds(comments)) == 1
    assert guard.assess(comments, NOW)[1] == "trigger-stale"


def test_a_trigger_without_an_id_does_not_break_the_round():
    """Defensive: a comment payload missing `id` still forms a usable round."""
    comment = trigger(at(minutes=-5))
    del comment["id"]
    rounds = guard.logical_rounds([comment])
    assert rounds[0].ids == frozenset()
    assert guard.assess([comment], NOW)[0]


# --- comment classification ----------------------------------------------


def test_only_a_leading_mention_is_a_trigger():
    """Prose that merely names the reviewer is not a trigger.

    The resolver's own `<!-- SDK_RESOLVE_STATUS -->` comment says it will
    "re-run `@sdk-review`" mid-sentence; treating that as a trigger would make
    the resolver hold its own push against a review nobody asked for.
    """
    status = {
        "body": "🤖 SDK Resolve — round 2. Fixing, then I'll re-run `@sdk-review`.",
        "created_at": at(minutes=-1),
        "user": {"login": "mothership-ai[bot]"},
        "reactions": {},
    }
    assert not guard.is_trigger(status)
    in_flight, reason, _ = guard.assess([status], NOW)
    assert not in_flight
    assert reason == "no-trigger"


def test_trigger_with_trailing_text_still_counts():
    comment = trigger(at(minutes=-2))
    comment["body"] = "@sdk-review please look at the retry path"
    assert guard.is_trigger(comment)


def test_verdict_from_a_non_reviewer_login_is_not_an_answer():
    """A human pasting the marker must not clear the hold."""
    comments = [trigger(at(minutes=-5)), verdict(at(minutes=-1), login="someone")]
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert in_flight
    assert reason == "review-in-flight"


def test_newest_trigger_wins_over_an_older_answered_one():
    comments = [
        trigger(at(minutes=-40), comment_id=1),
        verdict(at(minutes=-25), answers=1),
        trigger(at(minutes=-3), comment_id=2),
    ]
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert in_flight
    assert reason == "review-in-flight"


def test_out_of_order_listing_is_sorted_by_timestamp():
    """Correct even if the API stops returning comments oldest-first."""
    comments = [
        trigger(at(minutes=-3), comment_id=2),
        trigger(at(minutes=-40), comment_id=1),
    ]
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert in_flight
    assert reason == "review-in-flight"


def test_undated_comments_are_skipped_not_sorted_first():
    undated = trigger(at(minutes=-3), comment_id=2)
    undated["created_at"] = "not-a-timestamp"
    comments = [undated, trigger(at(minutes=-41), comment_id=1)]
    in_flight, reason, _ = guard.assess(comments, NOW)
    assert not in_flight
    assert reason == "trigger-stale"


def test_malformed_reactions_do_not_crash_or_decline():
    comment = trigger(at(minutes=-5))
    comment["reactions"] = "not-a-dict"
    assert not guard.was_declined(comment)
    assert guard.assess([comment], NOW)[0]


def test_non_numeric_reaction_count_is_not_a_decline():
    comment = trigger(at(minutes=-5))
    comment["reactions"] = {"confused": "many"}
    assert not guard.was_declined(comment)


def test_naive_timestamps_are_treated_as_utc():
    parsed = guard.parse_timestamp("2026-08-19T17:55:00")
    assert parsed is not None
    assert parsed.tzinfo is timezone.utc


# --- fetch + fail-open ----------------------------------------------------


def test_fetch_unwraps_slurped_pages():
    payload = [[trigger(at(minutes=-5))], [verdict(at(minutes=-1))]]
    comments = guard.fetch_comments(REPO, PR, runner_for(payload))
    assert comments is not None
    assert len(comments) == 2


def test_fetch_tolerates_an_unwrapped_array():
    payload = [trigger(at(minutes=-5))]
    comments = guard.fetch_comments(REPO, PR, runner_for(payload))
    assert comments is not None
    assert len(comments) == 1


def test_fetch_uses_paginate_and_slurp_not_jq(capsys: pytest.CaptureFixture[str]):
    """`--jq` would truncate a multiline verdict body to its marker line."""
    seen: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        seen.append(argv)
        return ok([])

    guard.fetch_comments(REPO, PR, run)
    assert "--paginate" in seen[0]
    assert "--slurp" in seen[0]
    assert "--jq" not in seen[0]


def test_gh_failure_fails_open(capsys: pytest.CaptureFixture[str]):
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    in_flight, reason, _ = guard.check(REPO, PR, run, clock)
    assert not in_flight
    assert reason == "state-unreadable"
    assert "::warning::" in capsys.readouterr().out


def test_unparseable_json_fails_open():
    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="{not json", stderr=""
        )

    in_flight, reason, _ = guard.check(REPO, PR, run, clock)
    assert not in_flight
    assert reason == "state-unreadable"


def test_empty_listing_is_distinct_from_unreadable():
    in_flight, reason, _ = guard.check(REPO, PR, runner_for([[]]), clock)
    assert not in_flight
    assert reason == "no-trigger"


# --- wait mode ------------------------------------------------------------


def test_wait_returns_as_soon_as_the_verdict_lands():
    answered = [[trigger(at(minutes=-5)), verdict(at(minutes=-1))]]
    pending = [[trigger(at(minutes=-5))]]
    fake = FakeTime()

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        # Answered on the third poll; the first two still see the bare trigger.
        return ok(answered if len(fake.slept) >= 2 else pending)

    in_flight, reason, _ = guard.wait_until_clear(
        REPO, PR, run, fake.clock, fake.sleep, interval_seconds=30
    )
    assert not in_flight
    assert reason == "verdict-answered"
    assert fake.slept == [30, 30]


def test_wait_gives_up_at_the_deadline():
    fake = FakeTime()
    in_flight, reason, message = guard.wait_until_clear(
        REPO,
        PR,
        runner_for([[trigger(at(minutes=-5))]]),
        fake.clock,
        fake.sleep,
        deadline_seconds=90,
        interval_seconds=30,
    )
    assert in_flight
    assert reason == "wait-deadline"
    assert "deadline 90s" in message
    # Sleeps to exactly the budget, then stops rather than overrunning it.
    assert fake.slept == [30, 30, 30]


def test_wait_terminates_under_a_stalled_clock():
    """The poll-count backstop, pinned.

    Without it a clock that never advances makes the elapsed check
    unfalsifiable and the loop spins forever — a guard that never returns is
    strictly worse than one that lets the push through.
    """
    slept: list[float] = []
    in_flight, reason, _ = guard.wait_until_clear(
        REPO,
        PR,
        runner_for([[trigger(at(minutes=-5))]]),
        clock,
        slept.append,
        deadline_seconds=90,
        interval_seconds=30,
    )
    assert in_flight
    assert reason == "wait-deadline"
    assert slept == [30, 30, 30]


def test_wait_does_not_sleep_when_already_clear():
    slept: list[float] = []
    in_flight, _, _ = guard.wait_until_clear(
        REPO, PR, runner_for([[]]), clock, slept.append
    )
    assert not in_flight
    assert slept == []


def test_wait_prints_a_heartbeat(capsys: pytest.CaptureFixture[str]):
    """Keeps bytes flowing so the sandbox stream watchdogs don't fire."""
    guard.wait_until_clear(
        REPO,
        PR,
        runner_for([[trigger(at(minutes=-5))]]),
        FakeTime().clock,
        FakeTime().sleep,
        deadline_seconds=60,
        interval_seconds=30,
    )
    assert "[push-guard] review in flight" in capsys.readouterr().out


# --- CLI ------------------------------------------------------------------


def test_main_exits_10_when_in_flight(capsys: pytest.CaptureFixture[str]):
    code = guard.main(
        ["--pr", PR], runner_for([[trigger(at(minutes=-5))]]), clock, lambda _s: None
    )
    assert code == guard.EXIT_IN_FLIGHT
    assert "HOLD (review-in-flight)" in capsys.readouterr().out


def test_main_exits_0_when_clear(capsys: pytest.CaptureFixture[str]):
    code = guard.main(["--pr", PR], runner_for([[]]), clock, lambda _s: None)
    assert code == guard.EXIT_CLEAR
    assert "clear (no-trigger)" in capsys.readouterr().out


def test_main_wait_mode_polls():
    fake = FakeTime()
    code = guard.main(
        ["--pr", PR, "--wait", "--deadline-seconds", "60", "--interval-seconds", "30"],
        runner_for([[trigger(at(minutes=-5))]]),
        fake.clock,
        fake.sleep,
    )
    assert code == guard.EXIT_IN_FLIGHT
    assert fake.slept == [30, 30]


def test_main_reads_repo_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REPO", "atlanhq/somewhere-else")
    seen: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        seen.append(argv)
        return ok([[]])

    guard.main(["--pr", PR], run, clock, lambda _s: None)
    assert "repos/atlanhq/somewhere-else/issues/3276/comments" in seen[0]


def test_main_defaults_repo_to_application_sdk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REPO", raising=False)
    seen: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        seen.append(argv)
        return ok([[]])

    guard.main(["--pr", PR], run, clock, lambda _s: None)
    assert f"repos/{REPO}/issues/3276/comments" in seen[0]


def test_pr_argument_is_required():
    with pytest.raises(SystemExit):
        guard.build_parser().parse_args([])
