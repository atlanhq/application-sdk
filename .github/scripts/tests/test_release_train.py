"""Tests for release_train.py (FND-4036). No network: a fake ``gh`` models the
slice of GitHub the job reads and writes, so a re-run can be checked against
the state the first run left behind."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_train as rt  # noqa: E402
from _gha_expr import evaluate_operand  # noqa: E402

REPO_ROOT = SCRIPTS.parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/release-train.yml"
# The shape of connector-pulse's data/release_train.yaml (the real file is
# validated by connector-pulse's own test; this job re-validates it every run).
PILOT_LIST = """\
schema_version: 1
timezone: IST
days: [mon, tue, wed, thu]
slots: ["09:00", "12:00", "15:00", "18:00", "21:00"]
per_slot_cap: 10
per_day_cap: 40
max_regate_per_pr: 2
schedule:
  atlanhq/atlan-openapi-app: [{day: mon, slot: "12:00"}]
  atlanhq/atlan-mysql-app: [{day: mon, slot: "12:00"}]
  atlanhq/atlan-metabase-app: [{day: mon, slot: "12:00"}]
"""

APP = "atlanhq/atlan-example-app"
BOT = "atlan-app-fleet[bot]"
HEAD = "a" * 40
# Mon 26 Oct 2026 (ISO 2026-W44). 06:40 UTC = 12:10 IST, just after the 12:00 slot.
MON_1210 = datetime(2026, 10, 26, 6, 40, tzinfo=UTC)
MON_0800 = datetime(2026, 10, 26, 2, 30, tzinfo=UTC)
TUE_0800 = datetime(2026, 10, 27, 2, 30, tzinfo=UTC)
THU_2300 = datetime(2026, 10, 29, 17, 30, tzinfo=UTC)


def _config(**overrides) -> rt.Config:
    data = {
        "schema_version": 1,
        "timezone": "IST",
        "days": ["mon", "tue", "wed", "thu"],
        "slots": ["09:00", "12:00", "15:00", "18:00", "21:00"],
        "per_slot_cap": 10,
        "per_day_cap": 40,
        "max_regate_per_pr": 2,
        "schedule": {APP: [{"day": "mon", "slot": "12:00"}]},
    }
    data.update(overrides)
    return rt.parse_config(yaml.safe_dump(data))


# ── Config ───────────────────────────────────────────────────────────────


def test_the_pilot_list_parses() -> None:
    config = rt.parse_config(PILOT_LIST)
    assert config.days == ("mon", "tue", "wed", "thu")
    assert config.slots == rt.CRON_SLOTS
    # The pilot: the three canonical apps. Widening the train is an edit here.
    assert set(config.schedule) == {
        "atlanhq/atlan-openapi-app",
        "atlanhq/atlan-mysql-app",
        "atlanhq/atlan-metabase-app",
    }
    for releases in config.schedule.values():
        assert 1 <= len(releases) <= len(config.days)


@pytest.mark.parametrize("day", ["fri", "sat", "sun"])
def test_weekend_days_are_rejected(day: str) -> None:
    with pytest.raises(rt.ConfigError, match="not allowed"):
        _config(days=["mon", day])


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"days": ["monday"]}, "not a weekday"),
        ({"days": []}, "non-empty"),
        ({"days": ["mon", "mon"]}, "twice"),
        ({"slots": ["10:00"]}, "no slot cron"),
        ({"slots": ["9:00"]}, "HH:MM"),
        ({"slots": [900]}, "HH:MM"),
        ({"slots": ["12:00", "12:00"]}, "twice"),
        ({"per_slot_cap": 0}, "per_slot_cap"),
        ({"per_slot_cap": True}, "per_slot_cap"),
        ({"per_day_cap": 5}, "per_day_cap must be >= per_slot_cap"),
        ({"max_regate_per_pr": -1}, "max_regate_per_pr"),
        ({"schema_version": 2}, "schema_version"),
        ({"timezone": "UTC"}, "IST"),
        ({"schedule": {"not-a-repo": [{"day": "mon", "slot": "12:00"}]}}, "owner/repo"),
        ({"schedule": [APP]}, "must map owner/repo"),
        ({"schedule": {APP: []}}, "at least one release"),
        ({"schedule": {APP: {"day": "mon"}}}, "must be a list"),
        ({"schedule": {APP: ["mon"]}}, "is not {day, slot}"),
        ({"schedule": {APP: [{"day": "fri", "slot": "12:00"}]}}, "train day"),
        ({"schedule": {APP: [{"day": "mon", "slot": "13:00"}]}}, "train slot"),
        ({"scope": [APP]}, "unknown keys"),
        ({"default_schedule": {}}, "unknown keys"),
        (
            {
                "schedule": {
                    APP: [
                        {"day": "mon", "slot": "09:00"},
                        {"day": "mon", "slot": "12:00"},
                    ]
                }
            },
            "more than one",
        ),
    ],
)
def test_invalid_config_is_rejected(overrides: dict, match: str) -> None:
    with pytest.raises(rt.ConfigError, match=match):
        _config(**overrides)


def test_schedule_entries_are_sorted_by_day_then_slot() -> None:
    config = _config(
        schedule={
            APP: [
                {"day": "thu", "slot": "09:00"},
                {"day": "mon", "slot": "21:00"},
                {"day": "tue", "slot": "12:00"},
            ]
        }
    )
    assert config.schedule[APP] == (
        ("mon", "21:00"),
        ("tue", "12:00"),
        ("thu", "09:00"),
    )


def test_an_unquoted_slot_is_rejected_not_guessed() -> None:
    # Unquoted 12:00 is YAML 1.1 sexagesimal (720); it must fail, not shift.
    text = PILOT_LIST.replace('slot: "12:00"', "slot: 12:00")
    with pytest.raises(rt.ConfigError, match="train slot"):
        rt.parse_config(text)


def test_an_empty_or_missing_schedule_lists_no_apps() -> None:
    assert _config(schedule={}).schedule == {}
    assert _config(schedule=None).schedule == {}


# ── Roster ───────────────────────────────────────────────────────────────


def test_roster_is_the_list_and_never_the_whole_fleet() -> None:
    other = "atlanhq/atlan-other-app"
    config = _config(
        schedule={
            APP: [{"day": "mon", "slot": "12:00"}],
            other: [{"day": "tue", "slot": "09:00"}],
        }
    )
    assert rt.roster(config, []) == sorted([APP, other])
    assert rt.roster(config, [other]) == [other]
    with pytest.raises(ValueError, match="not in the schedule list"):
        rt.roster(config, ["atlanhq/atlan-unlisted-app"])
    # An empty list releases nothing: no fallback to discovering the fleet.
    assert rt.roster(_config(schedule={}), []) == []


# ── Phases and time ──────────────────────────────────────────────────────


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_cron_map_matches_the_workflow_exactly() -> None:
    doc = _workflow()
    on = doc.get("on", doc.get(True))
    crons = [entry["cron"] for entry in on["schedule"]]
    assert sorted(crons) == sorted(rt.CRON_PHASES)


@pytest.mark.parametrize(
    ("cron", "phase"),
    [
        ("30 2 * * 1-5", "sync"),
        ("30 3,6,9,12,15 * * 1-4", "slot"),
        ("30 17 * * 4", "close"),
    ],
)
def test_phase_from_cron(cron: str, phase: str) -> None:
    assert rt.phase_from_cron(cron) == phase
    assert rt.resolve_phase("auto", cron) == phase


def test_resolve_phase_explicit_and_manual_auto() -> None:
    assert rt.resolve_phase("close", "30 2 * * 1-5") == "close"
    assert rt.resolve_phase("auto", "") == "sync"
    with pytest.raises(ValueError):
        rt.resolve_phase("auto", "0 0 * * *")
    with pytest.raises(ValueError):
        rt.resolve_phase("deploy", "")


def test_each_slot_cron_hour_lands_on_a_slot() -> None:
    hours = [3, 6, 9, 12, 15]
    landed = [
        rt.current_slot(datetime(2026, 10, 26, h, 30, tzinfo=UTC), rt.CRON_SLOTS)
        for h in hours
    ]
    assert landed == list(rt.CRON_SLOTS)


def test_ist_time_math() -> None:
    moment = rt.slot_at(date(2026, 10, 26), "12:00")
    assert rt.slot_key(moment) == "2026-10-26T12:00+05:30"
    assert rt.parse_slot_key("2026-10-26T12:00+05:30") == moment
    assert moment.astimezone(UTC).hour == 6 and moment.astimezone(UTC).minute == 30
    assert rt.format_slot(moment) == "Mon 26 Oct 12:00 IST"
    assert (
        rt.format_slot(rt.slot_at(date(2026, 11, 2), "09:00")) == "Mon 2 Nov 09:00 IST"
    )
    assert rt.iso_week(date(2026, 10, 26)) == "2026-W44"
    assert rt.previous_week("2026-W44") == "2026-W43"
    assert rt.previous_week("2027-W01") == "2026-W53"
    assert rt.slot_week("2026-10-29T21:00+05:30") == "2026-W44"


def test_current_slot_reads_the_clock_not_the_cron() -> None:
    assert rt.current_slot(MON_0800, rt.CRON_SLOTS) is None
    assert rt.current_slot(MON_1210, rt.CRON_SLOTS) == "12:00"
    # A slot run that starts late still belongs to the slot it is in.
    late = datetime(2026, 10, 26, 9, 55, tzinfo=UTC)  # 15:25 IST
    assert rt.current_slot(late, rt.CRON_SLOTS) == "15:00"
    with pytest.raises(ValueError):
        rt.current_slot(datetime(2026, 10, 26, 6, 40), rt.CRON_SLOTS)


def test_next_slot_crosses_weeks_and_honours_skips() -> None:
    schedule = (("mon", "12:00"),)
    after = rt.next_slot(schedule, MON_1210)
    assert after is not None and rt.slot_key(after) == "2026-11-02T12:00+05:30"
    before = rt.next_slot(schedule, MON_0800)
    assert before is not None and rt.slot_key(before) == "2026-10-26T12:00+05:30"
    skipped = rt.next_slot(schedule, MON_0800, skipped={"2026-10-26T12:00+05:30"})
    assert skipped is not None and rt.slot_key(skipped) == "2026-11-02T12:00+05:30"
    at = rt.slot_at(date(2026, 10, 26), "12:00")
    assert rt.next_slot(schedule, at, inclusive=True) == at
    assert rt.next_slot((), MON_0800) is None


# ── Caps and overflow ────────────────────────────────────────────────────

SLOTS = rt.CRON_SLOTS


def test_plan_places_apps_in_their_slot() -> None:
    plan = rt.plan_day([rt.Due("a/x", "12:00"), rt.Due("a/y", "09:00")], SLOTS, 10, 40)
    assert plan == {"a/x": "12:00", "a/y": "09:00"}


def test_plan_overflows_to_the_next_slot_with_room() -> None:
    dues = [rt.Due(f"a/app{i}", "12:00") for i in range(5)]
    plan = rt.plan_day(dues, SLOTS, 2, 40)
    assert [plan[f"a/app{i}"] for i in range(5)] == [
        "12:00",
        "12:00",
        "15:00",
        "15:00",
        "18:00",
    ]


def test_plan_respects_the_day_cap_and_the_last_slot() -> None:
    dues = [rt.Due(f"a/app{i}", "21:00") for i in range(3)]
    assert rt.plan_day(dues, SLOTS, 1, 40) == {
        "a/app0": "21:00",
        "a/app1": None,
        "a/app2": None,
    }
    dues = [rt.Due(f"a/app{i}", "09:00") for i in range(4)]
    plan = rt.plan_day(dues, SLOTS, 10, 3)
    assert sorted(v for v in plan.values() if v) == ["09:00"] * 3
    assert plan["a/app3"] is None


def test_plan_keeps_released_apps_in_their_slot_and_is_order_independent() -> None:
    dues = [
        rt.Due("a/z", "12:00"),
        rt.Due("a/done", "12:00", done="12:00"),
        rt.Due("a/b", "12:00"),
    ]
    plan = rt.plan_day(dues, SLOTS, 2, 40)
    assert plan == {"a/done": "12:00", "a/b": "12:00", "a/z": "15:00"}
    assert rt.plan_day(list(reversed(dues)), SLOTS, 2, 40) == plan
    assert rt.plan_day([rt.Due("a/x", "10:00")], SLOTS, 2, 40) == {"a/x": None}


# ── Eligibility ──────────────────────────────────────────────────────────

GATE_SCRIPT = "# reads the gate-check status\n"
RULES = [
    {
        "type": "required_status_checks",
        "parameters": {
            "required_status_checks": [
                {"context": "Release Gate"},
                {"context": "tests / Tests Gate"},
            ]
        },
    },
    {"type": "merge_queue", "parameters": {}},
]


def _eligible(**overrides) -> str | None:
    kwargs = {
        "atlan_yaml": "name: example\nrelease_model: semver\n",
        "release_gate": GATE_SCRIPT,
        "rules": RULES,
    }
    kwargs.update(overrides)
    return rt.eligibility(APP, **kwargs)


def test_an_eligible_app() -> None:
    assert _eligible() is None
    assert _eligible(atlan_yaml="name: x\nrelease_model: versioned\n") is None


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"atlan_yaml": None}, "no atlan.yaml"),
        ({"atlan_yaml": "name: x\n"}, "release_model: semver"),
        ({"atlan_yaml": "name: x\nrelease_model: cd\n"}, "is cd"),
        ({"atlan_yaml": "name: [unclosed"}, "not valid YAML"),
        ({"release_gate": None}, "bootstrap --resync"),
        ({"release_gate": "# label based\n"}, "bootstrap --resync"),
        ({"rules": None}, "unreadable"),
        ({"rules": []}, "Release Gate and Tests Gate"),
        ({"rules": RULES[:0] + [RULES[1]]}, "Release Gate and Tests Gate"),
        (
            {
                "rules": [
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "required_status_checks": [{"context": "Release Gate"}]
                        },
                    }
                ]
            },
            "must require Tests Gate",
        ),
    ],
)
def test_ineligible_apps_record_the_reason(overrides: dict, match: str) -> None:
    reason = _eligible(**overrides)
    assert reason is not None and match in reason


def test_codeowners_and_dispatchability() -> None:
    text = (
        "# owners\n* @atlanhq/old\n*   @atlanhq/team-a @someone # trailing\n/docs/ @x\n"
    )
    assert rt.codeowners(text) == ["@atlanhq/team-a", "@someone"]
    assert rt.codeowners(None) == []
    assert rt.dispatchable("on:\n  workflow_dispatch:\n  push:\n")
    assert rt.dispatchable("on: [push, workflow_dispatch]\n")
    assert not rt.dispatchable("on:\n  pull_request:\n    types: [closed]\n")
    assert not rt.dispatchable(None)


def test_gm_canary_is_a_stub() -> None:
    assert rt.gm_canary_stage(APP) == "unknown"


# ── Commands ─────────────────────────────────────────────────────────────


def _comment(cid: int, body: str, login: str = "owner", **extra) -> dict:
    user = {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"}
    user.update(extra.pop("user", {}))
    return {
        "id": cid,
        "body": body,
        "user": user,
        "created_at": extra.pop("created_at", "2026-10-26T02:00:00Z"),
    }


@pytest.mark.parametrize(
    ("body", "parsed"),
    [
        ("/release-skip waiting on a fix", ("skip", "waiting on a fix")),
        ("  /release-skip   reason here  \nmore", ("skip", "reason here")),
        ("/release-skip", ("skip", "")),
        ("/release-resume", ("resume", "")),
        ("/release-resume now", None),
        ("/release-skipper x", None),
        ("please /release-skip x", None),
        ("@team Releasing today at 12:00 IST. To skip: /release-skip <reason>", None),
    ],
)
def test_parse_command(body: str, parsed: tuple | None) -> None:
    assert rt.parse_command(body) == parsed


def test_honour_commands_checks_role_reason_and_bots() -> None:
    comments = [
        _comment(1, "/release-skip flaky e2e", "writer"),
        _comment(2, "/release-skip flaky e2e", "reader"),
        _comment(3, "/release-skip", "writer"),
        _comment(4, "/release-skip x", "renovate[bot]"),
        _comment(5, "/release-skip x", "app", user={"type": "Bot"}),
        _comment(6, "just a comment", "writer"),
        _comment(7, "/release-resume", "writer"),
    ]
    asked: list[str] = []

    def can_push(login: str) -> bool:
        asked.append(login)
        return login == "writer"

    honoured, refused = rt.honour_commands(comments, seen=[7], can_push=can_push)
    assert [c.comment_id for c in honoured] == [1]
    assert honoured[0].reason == "flaky e2e"
    assert {cid for cid, _ in refused} == {2, 3, 4, 5}
    assert "renovate[bot]" not in asked and "app" not in asked


def test_a_failed_permission_lookup_is_retried_not_honoured() -> None:
    def broken(login: str) -> bool:
        raise RuntimeError("HTTP 502")

    honoured, refused = rt.honour_commands(
        [_comment(1, "/release-skip x")], seen=[], can_push=broken
    )
    assert honoured == [] and refused == []


def test_skip_resume_skip_round_trip() -> None:
    record = rt.Record(week="2026-W44", repo=APP)
    schedule = (("mon", "12:00"),)
    at = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)

    def cmd(kind: str, cid: int) -> rt.Command:
        return rt.Command(kind, cid, "writer", at, "reason" if kind == "skip" else "")

    assert rt.apply_commands(record, [cmd("skip", 1)], schedule) == [1]
    assert rt.pending_skips(record) == {"2026-10-26T12:00+05:30"}
    rt.apply_commands(record, [cmd("resume", 2)], schedule)
    assert rt.pending_skips(record) == set()
    rt.apply_commands(record, [cmd("skip", 3)], schedule)
    assert rt.pending_skips(record) == {"2026-10-26T12:00+05:30"}
    assert record.seen_commands == [1, 2, 3]
    # Nothing scheduled: honoured (seen) but nothing to react to.
    assert rt.apply_commands(record, [cmd("skip", 4)], ()) == []
    assert 4 in record.seen_commands


def test_a_skip_after_thursdays_last_slot_targets_next_week_and_survives() -> None:
    record = rt.Record(week="2026-W44", repo=APP)
    late = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)  # Thu 22:30 IST
    rt.apply_commands(
        record, [rt.Command("skip", 9, "writer", late, "freeze")], (("mon", "12:00"),)
    )
    assert rt.pending_skips(record) == {"2026-11-02T12:00+05:30"}
    rolled = rt.roll_week(record, "2026-W45")
    assert rt.pending_skips(rolled) == {"2026-11-02T12:00+05:30"}
    assert rolled.skip_weeks == []  # the skip is for W45, not W44


# ── Record ───────────────────────────────────────────────────────────────


def test_record_round_trip_and_escaping() -> None:
    record = rt.Record(
        week="2026-W44", repo=APP, schedule=[{"day": "mon", "slot": "12:00"}]
    )
    rt.add_event(
        record,
        "skipped",
        slot="2026-10-26T12:00+05:30",
        sha=None,
        at=MON_0800,
        reason="--> <b>@team</b> & more",
        comment_id=5,
    )
    block = rt.serialize_record(record)
    inner = block.split("\n", 1)[1].rsplit("\n", 1)[0]
    assert "-->" not in inner and "<" not in inner and "&" not in inner
    body = rt.render_status(record, next_at=None, gate=None, head_sha=HEAD)
    parsed = rt.parse_record(body)
    assert parsed is not None and parsed.to_dict() == record.to_dict()
    assert rt.parse_record("no record here") is None
    assert rt.parse_record("<!-- release-train:record v1\n{not json\n-->") is None


def test_events_are_idempotent_by_key() -> None:
    record = rt.Record(week="2026-W44", repo=APP)
    key = dict(slot="2026-10-26T12:00+05:30", sha=HEAD, at=MON_1210)
    assert rt.add_event(record, "gate_requested", **key)
    assert not rt.add_event(record, "gate_requested", **key)
    assert rt.add_event(record, "gate_requested", **{**key, "sha": "b" * 40})
    assert rt.add_event(record, "armed", **key)
    assert rt.event_key(record.events[0])[:4] == (
        "2026-W44",
        "2026-10-26T12:00+05:30",
        "gate_requested",
        HEAD,
    )


def test_only_the_apps_own_marker_comment_is_state() -> None:
    forged = _comment(1, f"{rt.STATUS_MARKER}\nforged", "attacker")
    real = _comment(2, f"{rt.STATUS_MARKER}\nreal", BOT)
    notice = _comment(3, "Releasing today. To skip: /release-skip <reason>", BOT)
    mine, others = rt.status_comment([forged, real, notice], BOT)
    assert mine is not None and mine["id"] == 2
    assert [c["id"] for c in others] == [1]


def test_consecutive_skip_weeks() -> None:
    record = rt.Record(week="2026-W44", repo=APP, skip_weeks=["2026-W43"])
    assert not rt.consecutive_skip_weeks(record)
    rt.add_event(
        record, "skipped", slot="2026-10-26T12:00+05:30", sha=None, at=MON_0800
    )
    assert rt.consecutive_skip_weeks(record)
    rolled = rt.roll_week(record, "2026-W45")
    assert "2026-W44" in rolled.skip_weeks and rolled.regates == 0


# ── Titles ───────────────────────────────────────────────────────────────

NEXT = rt.slot_at(date(2026, 10, 26), "12:00")


@pytest.mark.parametrize(
    ("state", "reason", "title"),
    [
        ("planned", None, "Bump version to 1.4.0 · Next release Mon 26 Oct 12:00 IST"),
        (
            "skipped",
            None,
            "Bump version to 1.4.0 · Skipped, next release Mon 26 Oct 12:00 IST",
        ),
        (
            "blocked",
            "gate-check failed",
            "Bump version to 1.4.0 · Blocked: gate-check failed",
        ),
        ("not_on_train", "set x", "Bump version to 1.4.0 · Not on the train: set x"),
        ("armed", None, "Bump version to 1.4.0"),
        ("gate_requested", None, "Bump version to 1.4.0"),
    ],
)
def test_render_title(state: str, reason: str | None, title: str) -> None:
    assert (
        rt.render_title("Bump version to 1.4.0", state, next_at=NEXT, reason=reason)
        == title
    )


def test_base_title_strips_any_suffix() -> None:
    assert (
        rt.base_title("Bump version to 1.4.0 · Blocked: x · y")
        == "Bump version to 1.4.0"
    )
    assert rt.base_title("Bump version to 1.4.0") == "Bump version to 1.4.0"
    assert rt.render_title("B", "planned", next_at=None, reason=None) == "B"


# ── A fake GitHub ────────────────────────────────────────────────────────


class FakeGitHub:
    """Answers the ``gh`` calls release_train makes, and applies its writes."""

    def __init__(self) -> None:
        self.files = {
            "atlan.yaml": "name: example\nrelease_model: semver\n",
            ".github/scripts/release_gate.py": GATE_SCRIPT,
            ".github/CODEOWNERS": "* @atlanhq/example-owners\n",
            ".github/workflows/release.yaml": "on:\n  pull_request:\n    types: [closed]\n",
        }
        self.rules = copy.deepcopy(RULES)
        self.pull = {
            "number": 7,
            "node_id": "PR_7",
            "title": "Bump version to 1.4.0",
            "head": {"sha": HEAD},
        }
        self.open = True
        self.closed: list[dict] = []
        self.compare = {"ahead_by": 3, "behind_by": 0}
        self.statuses: list[dict] = []
        self.comments: list[dict] = []
        self.push = {"writer": True, "reader": False}
        self.auto_merge_by: str | None = None
        self.writes: list[tuple] = []
        self.calls: list[list[str]] = []
        self._next_id = 1000
        self.fail_mutations: set[str] = set()

    def add_comment(self, body: str, login: str, **extra) -> dict:
        self._next_id += 1
        comment = _comment(self._next_id, body, login, **extra)
        self.comments.append(comment)
        return comment

    def bot_comments(self) -> list[str]:
        return [c["body"] for c in self.comments if c["user"]["login"] == BOT]

    def __call__(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        args = cmd[1:]
        stdin = kwargs.get("input")
        try:
            out = self._route(args, stdin)
        except KeyError as exc:
            return subprocess.CompletedProcess(
                cmd, 1, "", f"gh: Not Found (HTTP 404) {exc}"
            )
        except RuntimeError as exc:
            return subprocess.CompletedProcess(cmd, 1, "", f"gh: Bad Gateway ({exc})")
        text = out if isinstance(out, str) else json.dumps(out)
        return subprocess.CompletedProcess(cmd, 0, text, "")

    def _route(self, args: list[str], stdin: str | None):
        assert args[0] == "api"
        if args[1] == "graphql":
            body = json.loads(stdin or "{}")
            query = body["query"].strip()
            if query.startswith("mutation"):
                name = re.search(r"(\w+)\(\s*input", query)
                assert name is not None
                if name.group(1) in self.fail_mutations:
                    self.fail_mutations.discard(name.group(1))
                    raise RuntimeError("HTTP 502")
                self.writes.append(("graphql", name.group(1), body["variables"]))
                if "enablePullRequestAutoMerge" in query:
                    # GraphQL's Bot.login is the bare slug — no "[bot]".
                    self.auto_merge_by = BOT.removesuffix("[bot]")
                if "disablePullRequestAutoMerge" in query:
                    self.auto_merge_by = None
                return {"data": {}}
            armed = (
                {"enabledBy": {"login": self.auto_merge_by}}
                if self.auto_merge_by
                else None
            )
            return {
                "data": {
                    "repository": {
                        "autoMergeAllowed": True,
                        "squashMergeAllowed": True,
                        "mergeCommitAllowed": False,
                        "rebaseMergeAllowed": False,
                        "pullRequest": {"id": "PR_7", "autoMergeRequest": armed},
                    }
                }
            }
        if args[1] == "-X":
            method, path = args[2], args[3]
            payload = json.loads(stdin) if stdin else None
            self.writes.append((method, path, payload))
            if method == "POST" and path.endswith("/comments"):
                return self.add_comment(payload["body"], BOT)
            if method == "PATCH" and "/issues/comments/" in path:
                cid = int(path.rsplit("/", 1)[1])
                for c in self.comments:
                    if c["id"] == cid:
                        c["body"] = payload["body"]
                return {}
            if method == "PATCH" and "/pulls/" in path:
                self.pull["title"] = payload["title"]
                return {}
            return {}
        if args[1] == "-H":
            path = args[3].split("/contents/", 1)[1].split("?", 1)[0]
            return self.files[path]
        if args[1] == "--paginate":
            return [list(self.comments)]
        path = args[1]
        if "/rules/branches/" in path:
            return self.rules
        if "/pulls?state=open" in path:
            return [copy.deepcopy(self.pull)] if self.open else []
        if "/pulls?state=closed" in path:
            return self.closed
        if "/compare/" in path:
            return self.compare
        if "/statuses" in path:
            return self.statuses
        if "/collaborators/" in path:
            login = path.split("/collaborators/")[1].split("/")[0]
            return {"user": {"permissions": {"push": self.push.get(login, False)}}}
        raise KeyError(path)


def _train(
    fake: FakeGitHub,
    now: datetime,
    *,
    live: bool = True,
    report: bool = False,
    config: rt.Config | None = None,
    phase: str = "slot",
) -> rt.Train:
    mode = "report" if report else ("live" if live else "dry-run")
    return rt.run(
        phase,
        config or _config(),
        [APP],
        rt.Gh(fake),
        mode=mode,
        now=now,
        bot_login=BOT,
    )


def _mutations(fake: FakeGitHub) -> list[list[str]]:
    """Every gh call that writes, judged from the argv alone."""
    found = []
    for cmd in fake.calls:
        args = cmd[1:]
        if "-X" in args and args[args.index("-X") + 1] != "GET":
            found.append(cmd)
        elif any(a in ("-f", "-F", "--field", "--raw-field") for a in args):
            found.append(cmd)
        elif args[:2] == ["api", "graphql"]:
            continue  # bodies checked through fake.writes
    return found


# ── Slot phase ───────────────────────────────────────────────────────────


def test_slot_releases_a_due_app() -> None:
    fake = FakeGitHub()
    train = _train(fake, MON_1210)
    assert not train.errors
    gate = [
        w for w in fake.writes if w[0] == "POST" and w[1].endswith("/issues/7/comments")
    ]
    # FND-4037: pr_test_command honours the fleet App only for the bare command.
    assert gate[0][2] == {"body": "/gate-check"}
    enable = [
        w
        for w in fake.writes
        if w[0] == "graphql" and w[1] == "enablePullRequestAutoMerge"
    ]
    assert enable and enable[0][2] == {"id": "PR_7", "method": "SQUASH", "sha": HEAD}
    assert fake.pull["title"] == "Bump version to 1.4.0"
    status = [b for b in fake.bot_comments() if rt.STATUS_MARKER in b]
    record = rt.parse_record(status[0])
    assert record is not None and record.state == "armed"
    assert [e["action"] for e in record.events] == ["gate_requested", "armed"]


def test_rerunning_a_slot_changes_nothing() -> None:
    fake = FakeGitHub()
    _train(fake, MON_1210)
    first = len(fake.writes)
    _train(fake, MON_1210)
    _train(fake, datetime(2026, 10, 26, 9, 40, tzinfo=UTC))  # the 15:00 run
    assert len(fake.writes) == first
    assert fake.bot_comments().count("/gate-check") == 1


def test_a_missed_slot_is_caught_up_by_the_next_run() -> None:
    fake = FakeGitHub()
    _train(
        fake, datetime(2026, 10, 26, 9, 40, tzinfo=UTC)
    )  # only the 15:00 run happened
    assert fake.bot_comments().count("/gate-check") == 1


def test_no_release_before_the_slot() -> None:
    fake = FakeGitHub()
    _train(fake, datetime(2026, 10, 26, 3, 40, tzinfo=UTC))  # 09:10 IST, due at 12:00
    assert "/gate-check" not in fake.bot_comments()
    assert (
        fake.pull["title"]
        == "Bump version to 1.4.0 · Next release Mon 26 Oct 12:00 IST"
    )


def test_dry_run_and_report_make_no_writes() -> None:
    for kwargs in ({"live": False}, {"report": True}):
        fake = FakeGitHub()
        fake.add_comment("/release-skip waiting", "writer")
        train = _train(fake, MON_1210, **kwargs)
        assert fake.writes == [] and _mutations(fake) == []
        assert not train.errors
    fake = FakeGitHub()
    train = _train(fake, MON_1210, live=False)
    assert any("comment on" in w for w in train.writer.intended)
    assert any("enable auto-merge" in w for w in train.writer.intended)


def test_a_skip_holds_the_slot_and_is_acknowledged() -> None:
    fake = FakeGitHub()
    skip = fake.add_comment("/release-skip waiting on a fix", "writer")
    fake.add_comment("/release-skip not mine", "reader")
    _train(fake, MON_1210)
    assert "/gate-check" not in fake.bot_comments()
    reactions = [w for w in fake.writes if w[1].endswith("/reactions")]
    assert reactions == [
        (
            "POST",
            f"repos/{APP}/issues/comments/{skip['id']}/reactions",
            {"content": "+1"},
        )
    ]
    assert (
        fake.pull["title"] == "Bump version to 1.4.0 · Next release Mon 2 Nov 12:00 IST"
    )


def test_resume_cancels_a_pending_skip() -> None:
    fake = FakeGitHub()
    fake.add_comment("/release-skip x", "writer", created_at="2026-10-26T01:00:00Z")
    fake.add_comment("/release-resume", "writer", created_at="2026-10-26T02:00:00Z")
    _train(fake, MON_1210)
    assert fake.bot_comments().count("/gate-check") == 1


def test_behind_main_defers_without_requesting_the_gate() -> None:
    fake = FakeGitHub()
    fake.compare = {"ahead_by": 3, "behind_by": 2}
    _train(fake, MON_1210)
    assert "/gate-check" not in fake.bot_comments()
    assert not [w for w in fake.writes if "dispatches" in w[1]]
    record = rt.parse_record(
        next(b for b in fake.bot_comments() if rt.STATUS_MARKER in b)
    )
    assert record is not None and record.state == "planned"
    assert "cannot be dispatched" in (record.reason or "")
    deferred = [e for e in record.events if e["action"] == "deferred"]
    assert deferred[0]["deferred_to"] == "2026-10-26T15:00+05:30"


def test_behind_main_dispatches_a_dispatchable_bump_workflow() -> None:
    fake = FakeGitHub()
    fake.compare = {"ahead_by": 3, "behind_by": 2}
    fake.files[".github/workflows/release.yaml"] = "on:\n  workflow_dispatch:\n"
    _train(fake, MON_1210)
    dispatches = [w for w in fake.writes if "dispatches" in w[1]]
    assert dispatches == [
        (
            "POST",
            f"repos/{APP}/actions/workflows/release.yaml/dispatches",
            {"ref": "main"},
        )
    ]


def test_ineligible_app_is_titled_not_on_the_train() -> None:
    fake = FakeGitHub()
    fake.files["atlan.yaml"] = "name: example\nrelease_model: cd\n"
    _train(fake, MON_1210)
    assert "/gate-check" not in fake.bot_comments()
    assert fake.pull["title"].startswith("Bump version to 1.4.0 · Not on the train: ")


def test_an_unscheduled_app_is_tracked_not_released() -> None:
    fake = FakeGitHub()
    # Only reachable via an explicit repos list; roster() never yields it.
    _train(fake, MON_1210, config=_config(schedule={}))
    assert "/gate-check" not in fake.bot_comments()
    assert "Not on the train: not listed in" in fake.pull["title"]


def _armed_world(regates: int, head: str = "b" * 40) -> FakeGitHub:
    fake = FakeGitHub()
    _train(fake, MON_1210)  # armed at HEAD in the 12:00 slot
    status = next(c for c in fake.comments if rt.STATUS_MARKER in c["body"])
    record = rt.parse_record(status["body"])
    assert record is not None
    record.regates = regates
    status["body"] = rt.render_status(record, next_at=None, gate=None, head_sha=HEAD)
    fake.pull["head"]["sha"] = head
    fake.writes.clear()
    return fake


def test_a_moved_head_is_regated_once() -> None:
    fake = _armed_world(regates=0)
    _train(fake, datetime(2026, 10, 26, 9, 40, tzinfo=UTC))
    _train(fake, datetime(2026, 10, 26, 12, 40, tzinfo=UTC))
    gates = [w for w in fake.writes if w[2] == {"body": "/gate-check"}]
    assert len(gates) == 1


def test_regate_is_capped() -> None:
    fake = _armed_world(regates=2)
    _train(fake, datetime(2026, 10, 26, 9, 40, tzinfo=UTC))
    assert not [w for w in fake.writes if w[2] == {"body": "/gate-check"}]
    assert "re-gate cap (2)" in fake.pull["title"]


def test_no_regate_while_the_new_head_passed_or_is_running() -> None:
    for state in ("success", "pending"):
        fake = _armed_world(regates=0)
        fake.statuses = [
            {
                "context": "gate-check",
                "state": state,
                "creator": {"login": rt.GATE_VERDICT_AUTHOR},
            }
        ]
        _train(fake, datetime(2026, 10, 26, 9, 40, tzinfo=UTC))
        assert not [w for w in fake.writes if w[2] == {"body": "/gate-check"}]


def test_gate_status_from_anyone_else_is_ignored() -> None:
    rows = [
        {"context": "gate-check", "state": "success", "creator": {"login": "someone"}},
        {
            "context": "gate-check",
            "state": "failure",
            "creator": {"login": rt.GATE_VERDICT_AUTHOR},
        },
    ]
    assert rt.gate_state(rows) == "failure"
    assert rt.gate_state([]) is None


def test_per_slot_cap_overflows_across_apps() -> None:
    config = _config(per_slot_cap=1)
    apps = []
    for name in ("atlanhq/atlan-a-app", "atlanhq/atlan-b-app"):
        app = rt.App(repo=name, schedule=(("mon", "12:00"),))
        app.pr = rt.PullRequest(1, "PR", "Bump version to 1.0.0", HEAD)
        apps.append((app, rt.Record(week="2026-W44", repo=name)))
    train = rt.Train(
        config,
        rt.Gh(FakeGitHub()),
        rt.Writer(rt.Gh(FakeGitHub()), live=False),
        now=MON_1210,
        bot_login=BOT,
    )
    assert train.plan(apps) == {
        "atlanhq/atlan-a-app": "12:00",
        "atlanhq/atlan-b-app": "15:00",
    }


# ── Sync and close ───────────────────────────────────────────────────────


def test_sync_posts_the_release_day_notice_once() -> None:
    fake = FakeGitHub()
    _train(fake, MON_0800, phase="sync")
    _train(fake, MON_0800, phase="sync")
    notices = [b for b in fake.bot_comments() if b.startswith("@")]
    assert notices == [
        "@atlanhq/example-owners Releasing today at 12:00 IST. To skip: /release-skip <reason>"
    ]
    assert (
        fake.pull["title"]
        == "Bump version to 1.4.0 · Next release Mon 26 Oct 12:00 IST"
    )


def test_sync_notice_without_codeowners_mentions_nobody() -> None:
    fake = FakeGitHub()
    del fake.files[".github/CODEOWNERS"]
    _train(fake, MON_0800, phase="sync")
    assert (
        "Releasing today at 12:00 IST. To skip: /release-skip <reason>"
        in fake.bot_comments()
    )


def test_same_login_bridges_rest_graphql_and_gh_shapes() -> None:
    assert rt.same_login("atlan-app-fleet", BOT)
    assert rt.same_login("app/atlan-app-fleet", BOT)
    assert not rt.same_login("a-human", BOT)
    assert not rt.same_login(None, BOT)


# ── Public logs (this repo is public) ────────────────────────────────────


def test_public_error_keeps_the_status_and_drops_the_message() -> None:
    exc = rt.GhError("gh api repos/o/r/contents/x: HTTP 404: Not Found (private)")
    assert rt.public_error(exc) == "GitHub API error (HTTP 404)"
    assert "private" not in rt.public_error(ValueError("a private path"))


def test_the_public_summary_never_carries_a_reason_or_title(capsys) -> None:
    fake = FakeGitHub()
    fake.files["atlan.yaml"] = "name: example\nrelease_model: cd\n"
    train = _train(fake, MON_1210, live=False)
    text = train.summary("slot", "dry-run") + capsys.readouterr().out
    assert rt.NOT_ON_TRAIN in text and "1 app(s) on the train" in text
    assert APP not in text and "example" not in text  # the list is private
    assert "release_model" not in text  # the reason stays on the bump PR
    assert "Bump version" not in text  # titles stay on the bump PR
    # --verbose (local runs only) shows the per-app table.
    assert APP in train.summary("slot", "dry-run", verbose=True)


def test_public_error_counts_drop_the_repo() -> None:
    errors = [f"{APP}: GitHub API error (HTTP 502)", "a/b: GitHub API error (HTTP 502)"]
    assert rt.public_error_counts(errors) == "GitHub API error (HTTP 502) ×2"


def test_a_failed_write_is_public_as_a_status_and_private_in_full() -> None:
    fake = FakeGitHub()
    fake.fail_mutations.add("enablePullRequestAutoMerge")
    train = _train(fake, MON_1210)
    assert train.errors == [f"{APP}: GitHub API error (HTTP 502)"]
    status = next(b for b in fake.bot_comments() if rt.STATUS_MARKER in b)
    assert "a GitHub call failed, retried next run" in status


def test_a_failed_arm_is_retried_without_a_second_gate_request() -> None:
    fake = FakeGitHub()
    fake.fail_mutations.add("enablePullRequestAutoMerge")
    first = _train(fake, MON_1210)
    assert first.errors
    record = rt.parse_record(
        next(b for b in fake.bot_comments() if rt.STATUS_MARKER in b)
    )
    assert record is not None
    assert [e["action"] for e in record.events] == ["gate_requested"]
    _train(fake, datetime(2026, 10, 26, 9, 40, tzinfo=UTC))  # the 15:00 run
    assert fake.bot_comments().count("/gate-check") == 1
    assert fake.auto_merge_by == "atlan-app-fleet"
    record = rt.parse_record(
        next(b for b in fake.bot_comments() if rt.STATUS_MARKER in b)
    )
    assert record is not None and record.state == "armed"


def test_sync_disarms_what_the_job_armed_on_an_earlier_day() -> None:
    fake = FakeGitHub()
    _train(fake, MON_1210)
    assert fake.auto_merge_by == "atlan-app-fleet"  # GraphQL's shape
    fake.writes.clear()
    _train(fake, TUE_0800, phase="sync")
    assert ("graphql", "disablePullRequestAutoMerge", {"id": "PR_7"}) in fake.writes
    assert (
        fake.pull["title"] == "Bump version to 1.4.0 · Next release Mon 2 Nov 12:00 IST"
    )


def test_sync_never_disarms_a_humans_auto_merge() -> None:
    fake = FakeGitHub()
    _train(fake, MON_1210)
    fake.auto_merge_by = "a-human"
    fake.writes.clear()
    _train(fake, TUE_0800, phase="sync")
    assert not [w for w in fake.writes if w[1] == "disablePullRequestAutoMerge"]


def test_close_disarms_expires_skips_and_summarises(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeGitHub()
    _train(fake, MON_1210)
    fake.add_comment(
        "/release-skip still broken", "writer", created_at="2026-10-26T08:00:00Z"
    )
    fake.writes.clear()
    config = _config(
        schedule={
            APP: [{"day": "mon", "slot": "12:00"}, {"day": "thu", "slot": "21:00"}]
        }
    )
    train = _train(fake, THU_2300, phase="close", config=config)
    assert ("graphql", "disablePullRequestAutoMerge", {"id": "PR_7"}) in fake.writes
    record = rt.parse_record(
        next(b for b in fake.bot_comments() if rt.STATUS_MARKER in b)
    )
    assert record is not None and rt.pending_skips(record) == set()
    assert any(e["action"] == "expired" for e in record.events)
    text = train.summary("close", "live")
    assert "weekly summary" in text and APP not in text
    assert APP in train.summary("close", "live", verbose=True)


def test_close_notes_two_weeks_of_skips() -> None:
    fake = FakeGitHub()
    record = rt.Record(week="2026-W44", repo=APP, skip_weeks=["2026-W43"])
    rt.add_event(
        record, "skipped", slot="2026-10-26T12:00+05:30", sha=None, at=MON_0800
    )
    fake.add_comment(
        rt.render_status(record, next_at=None, gate=None, head_sha=HEAD), BOT
    )
    train = _train(fake, THU_2300, phase="close")
    assert any("two weeks running" in n for n in train.notes)


def test_close_marks_a_bump_pr_merged_this_week() -> None:
    fake = FakeGitHub()
    _train(fake, MON_1210)
    status = next(c for c in fake.comments if rt.STATUS_MARKER in c["body"])
    fake.open = False
    fake.closed = [
        {"number": 7, "merged_at": "2026-10-26T08:00:00Z", "merge_commit_sha": "c" * 40}
    ]
    train = _train(fake, THU_2300, phase="close")
    record = rt.parse_record(status["body"])
    assert record is not None and record.state == "merged"
    assert any(r.state == "merged" for r in train.rows)


# ── Entry point and workflow ─────────────────────────────────────────────


def test_main_dry_run_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub()
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("APP_SLUG", "atlan-app-fleet")
    fake.files[rt.CONFIG_PATH] = PILOT_LIST.replace(
        "atlan-openapi-app", "atlan-example-app"
    )
    code = rt.main(
        [
            "--cron",
            "30 3,6,9,12,15 * * 1-4",
            "--repos",
            json.dumps([APP]),
        ],
        runner=fake,
        now=MON_1210,
    )
    assert code == 0
    assert fake.writes == [] and _mutations(fake) == []
    assert "Would perform" in summary.read_text()
    assert APP not in summary.read_text()
    # With no --config, the list is read from connector-pulse main.
    assert any(
        f"repos/{rt.CONFIG_REPO}/contents/{rt.CONFIG_PATH}?ref=main" in " ".join(c)
        for c in fake.calls
    )


def test_main_fails_closed_and_quietly_on_a_bad_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub()
    fake.files[rt.CONFIG_PATH] = PILOT_LIST.replace("day: mon", "day: fri")
    assert rt.main(["--phase", "sync"], runner=fake) == 2
    err = capsys.readouterr().err
    assert "is invalid" in err and "atlan-openapi-app" not in err
    assert _mutations(fake) == []
    bad = tmp_path / "bad.yaml"
    bad.write_text(PILOT_LIST.replace("day: mon", "day: fri"))
    assert rt.main(["--config", str(bad), "--verbose"], runner=fake) == 2
    assert "atlan-openapi-app" in capsys.readouterr().err  # --verbose: local only


def test_main_rejects_bad_arguments(tmp_path: Path) -> None:
    fake = FakeGitHub()
    pilot = tmp_path / "pilot.yaml"
    pilot.write_text(PILOT_LIST)
    assert rt.main(["--mode", "yolo"], runner=fake) == 2
    assert (
        rt.main(
            ["--config", str(pilot), "--repos", '["atlanhq/elsewhere"]'],
            runner=fake,
        )
        == 2
    )
    assert rt.main(["--config", str(pilot), "--cron", "1 1 * * *"], runner=fake) == 2
    assert fake.calls == []


def test_parse_repos() -> None:
    assert rt.parse_repos("") == []
    assert rt.parse_repos('["a/b", "c/d"]') == ["a/b", "c/d"]
    assert rt.parse_repos("a/b, c/d") == ["a/b", "c/d"]
    with pytest.raises(ValueError):
        rt.parse_repos("[1]")


def _mode_expression() -> str:
    steps = _workflow()["jobs"]["train"]["steps"]
    env = next(s for s in steps if s.get("name") == "Run the release train")["env"]
    expr = env["MODE"].strip()
    assert expr.startswith("${{") and expr.endswith("}}")
    return expr[3:-2].strip()


@pytest.mark.parametrize(
    ("enabled", "live", "event", "dry_run", "mode"),
    [
        ("", "true", "schedule", None, "report"),
        ("false", "true", "workflow_dispatch", "false", "report"),
        ("true", "", "schedule", None, "dry-run"),
        ("true", "false", "workflow_dispatch", "false", "dry-run"),
        ("true", "true", "schedule", None, "live"),
        ("true", "true", "workflow_dispatch", "true", "dry-run"),
        ("true", "true", "workflow_dispatch", "false", "live"),
        # Fail closed if the input ever arrives as a real boolean.
        ("true", "true", "workflow_dispatch", False, "dry-run"),
    ],
)
def test_workflow_mode_expression(enabled, live, event, dry_run, mode) -> None:
    inputs = {} if dry_run is None else {"dry_run": dry_run}
    contexts = {
        "vars": {"RELEASE_TRAIN_ENABLED": enabled, "RELEASE_TRAIN_LIVE": live},
        "github": {"event_name": event, "event": {"inputs": inputs}},
    }
    assert evaluate_operand(_mode_expression(), contexts) == mode


def test_workflow_shape() -> None:
    doc = _workflow()
    assert doc["name"] == "Release Train"
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"] == {"group": "release-train", "cancel-in-progress": False}
    on = doc.get("on", doc.get(True))
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["phase"]["default"] == "auto"
    assert inputs["phase"]["options"] == ["auto", "sync", "slot", "close"]
    assert inputs["dry_run"] == {
        **inputs["dry_run"],
        "type": "boolean",
        "default": True,
    }
    job = doc["jobs"]["train"]
    assert job["timeout-minutes"] <= 60
    for step in job["steps"]:
        uses = step.get("uses", "")
        if uses and not uses.startswith("./"):
            assert "@" in uses and len(uses.split("@")[1].split()[0]) == 40
        run = step.get("run", "")
        for word in (" if ", "then", " for ", "while", "case ", "||", "&&"):
            assert word not in f" {run} "
        assert "${{" not in run
