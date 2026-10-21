#!/usr/bin/env python3
"""App release train (FND-4036, design FND-3529) — deterministic, no AI.

Every app repo keeps one bump-version PR open (head ``bump-version-main``,
title ``Bump version to X``), kept current by ``release-version-bump.yaml``.
Releasing it is two human steps: comment ``/gate-check`` (``pr_test_command.py``
runs the full release gate and posts the ``gate-check`` status) and merge. This
script does those steps on a schedule, from ``.github/workflows/release-train.yml``.

Phases (all times IST, a fixed UTC+05:30)
-----------------------------------------
``sync``  08:00 Mon-Fri. For every listed app: eligibility, schedule, the bump
          PR's title suffix and status comment; disarm auto-merge this job armed
          on an earlier day that has not merged; on a release day post a new
          comment naming the app's CODEOWNERS.
``slot``  09/12/15/18/21 Mon-Thu. Releases the apps due by now (catching up on
          a missed run): pre-flight, ``/gate-check``, plain title, auto-merge.
          Re-gates apps armed this week whose head moved.
``close`` 23:00 Thu. Disarms everything still armed, expires this week's skips,
          writes the weekly summary.

State lives on the bump PR itself
---------------------------------
One status comment per bump PR, found by ``<!-- release-train:status -->`` AND
authored by this job's App (a marker anyone else posts is ignored, so nobody can
forge state), edited in place. It carries a human section and a hidden JSON
record. Every action is an event keyed by ``(week, slot, action, sha)``, so
re-running a phase is a no-op and a missed run catches up on the next one.

Skips are PR comments, never labels: ``/release-skip <reason>`` skips the app's
next slot and ``/release-resume`` cancels a pending skip. Only commenters with
Write/Maintain/Admin (collaborator-permission API, as ``pr_test_command`` does)
count; bots never do.

The schedule is one central list, kept private
----------------------------------------------
``data/release_train.yaml`` in the private ``atlanhq/connector-pulse`` repo holds
the slot grid, the caps and ``schedule``: each ``owner/repo`` and its releases
(``{day, slot}``, at most one a day). Being listed is what puts an app on the
train; an unlisted app is never read or touched. Every run fetches it from
connector-pulse ``main`` with the fleet App token, so a merged edit takes effect
at the next run. ``--config`` reads a local file instead (local runs, tests).

Public logs
-----------
This repo is public, so its run logs are too. They carry counts only: how many
apps are listed, in each state, and how many writes and errors of each kind.
Never a repo name, reason, title, comment text, login or raw API error, and a
config error says only that the list is invalid (connector-pulse's own test
names the problem). Per-app detail goes to each bump PR's status comment and to
Pulse. ``--verbose`` prints the per-app table, for local runs only.

Safety
------
Nothing is mutated unless ``--mode live``: dry-run prints every intended write,
``report`` (the kill switch) computes and summarises only. The workflow decides
the mode in one expression; this script does the branching (docs/standards/ci.md).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import yaml

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import pr_test_command as ptc  # noqa: E402
from react_to_comment import is_transient  # noqa: E402
from renovate_rearm_automerge import merge_method  # noqa: E402
from resync_approval_conditions import flatten  # noqa: E402

# ── Constants ────────────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30), "IST")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
#: Nothing ships into a weekend: fri/sat/sun are rejected by the config check.
TRAIN_DAYS = ("mon", "tue", "wed", "thu")
#: The slots the workflow's slot cron fires for. A configured slot outside this
#: set would never run, so the config check rejects it.
CRON_SLOTS = ("09:00", "12:00", "15:00", "18:00", "21:00")

SYNC, SLOT, CLOSE = "sync", "slot", "close"
PHASES = (SYNC, SLOT, CLOSE)
#: Must equal the workflow's ``schedule`` crons exactly (a test enforces it).
CRON_PHASES = {
    "30 2 * * 1-5": SYNC,  # 08:00 IST Mon-Fri
    "30 3,6,9,12,15 * * 1-4": SLOT,  # 09/12/15/18/21 IST Mon-Thu
    "30 17 * * 4": CLOSE,  # 23:00 IST Thu
}
MODES = ("dry-run", "live", "report")

STATUS_MARKER = "<!-- release-train:status -->"
_RECORD_RE = re.compile(r"<!-- release-train:record v1\n(?P<json>.*?)\n-->", re.S)

PLANNED, GATE_REQUESTED, ARMED, MERGED = "planned", "gate_requested", "armed", "merged"
BLOCKED, SKIPPED, NOT_ON_TRAIN = "blocked", "skipped", "not_on_train"
STATES = (PLANNED, GATE_REQUESTED, ARMED, MERGED, BLOCKED, SKIPPED, NOT_ON_TRAIN)

BUMP_BRANCH = "bump-version-main"
BASE_BRANCH = "main"
TITLE_SEP = " · "
ATLAN_YAML = "atlan.yaml"
RELEASE_GATE_SCRIPT = ".github/scripts/release_gate.py"
CODEOWNERS_PATH = ".github/CODEOWNERS"
#: The one schedule list. Private, so the list never appears in this public repo.
CONFIG_REPO = "atlanhq/connector-pulse"
CONFIG_PATH = "data/release_train.yaml"
SCHEDULE_SOURCE = f"connector-pulse `{CONFIG_PATH}`"
#: The app's bump workflow (bootstrap template ``release.yaml``).
BUMP_WORKFLOW = "release.yaml"
#: Required on main. A ruleset context is the check-run name, so a reusable's
#: job reads ``<caller> / <job>`` (``tests / Tests Gate``); see release-gate.yaml.
REQUIRED_CHECKS = ("Release Gate", "Tests Gate")
GATE_CHECK_CONTEXT = ptc.GATE_CHECK_CONTEXT
#: The only identity whose ``gate-check`` status release_gate.py believes.
GATE_VERDICT_AUTHOR = "github-actions[bot]"
DEFAULT_APP_SLUG = "atlan-app-fleet"
JOB_ACTOR = "release-train"
SKIP_COMMAND = "/release-skip"
RESUME_COMMAND = "/release-resume"
_SKIP_RE = re.compile(r"^/release-skip(?:[ \t]+(?P<reason>\S.*))?$")
_RESUME_RE = re.compile(r"^/release-resume\s*$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_SLOT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_SEEN_COMMANDS = 200
MAX_SKIP_WEEKS = 8
REASON_MAX = 120
GH_TIMEOUT = 60
GH_ATTEMPTS = 3

Runner = Callable[..., subprocess.CompletedProcess]
Entry = tuple[str, str]  # (day, "HH:MM")


class ConfigError(ValueError):
    """The schedule list (connector-pulse ``data/release_train.yaml``) is invalid."""


class GhError(RuntimeError):
    pass


# ── Config ───────────────────────────────────────────────────────────────


CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "timezone",
        "days",
        "slots",
        "per_slot_cap",
        "per_day_cap",
        "max_regate_per_pr",
        "schedule",
    }
)


@dataclass(frozen=True)
class Config:
    days: tuple[str, ...]
    slots: tuple[str, ...]
    per_slot_cap: int
    per_day_cap: int
    max_regate_per_pr: int
    schedule: dict[str, tuple[Entry, ...]] = field(default_factory=dict)


def _int_at_least(data: dict, key: str, floor: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < floor:
        raise ConfigError(f"{key} must be an integer >= {floor}, got {value!r}")
    return value


def parse_releases(
    entries: Any, days: Iterable[str], slots: Iterable[str], where: str
) -> tuple[Entry, ...]:
    """``[{day, slot}, …]`` → sorted ``(day, slot)`` tuples. One release a day."""
    days, slots = tuple(days), tuple(slots)
    if not isinstance(entries, list):
        raise ConfigError(f"{where}: releases must be a list of {{day, slot}}")
    seen_days: set[str] = set()
    out: list[Entry] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: {entry!r} is not {{day, slot}}")
        day, slot = entry.get("day"), entry.get("slot")
        if day not in days:
            raise ConfigError(f"{where}: day {day!r} is not a train day {list(days)}")
        if slot not in slots:
            raise ConfigError(
                f"{where}: slot {slot!r} is not a train slot {list(slots)}"
            )
        if day in seen_days:
            raise ConfigError(f"{where}: more than one release on {day}")
        seen_days.add(day)
        out.append((day, slot))
    return tuple(sorted(out, key=lambda e: (WEEKDAYS.index(e[0]), e[1])))


def parse_config(text: str) -> Config:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("must be a mapping")
    if data.get("schema_version") != 1:
        raise ConfigError("schema_version must be 1")
    if data.get("timezone") != "IST":
        raise ConfigError("timezone must be IST (fixed UTC+05:30)")
    unknown = sorted(set(data) - CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"unknown keys {unknown}; allowed: {sorted(CONFIG_KEYS)}")

    days = data.get("days")
    if not isinstance(days, list) or not days:
        raise ConfigError("days must be a non-empty list")
    for day in days:
        if day not in WEEKDAYS:
            raise ConfigError(f"days: {day!r} is not a weekday (mon..sun)")
        if day not in TRAIN_DAYS:
            raise ConfigError(f"days: {day} is not allowed — the train runs mon..thu")
    if len(set(days)) != len(days):
        raise ConfigError("days lists a day twice")

    slots = data.get("slots")
    if not isinstance(slots, list) or not slots:
        raise ConfigError("slots must be a non-empty list of HH:MM")
    for slot in slots:
        if not isinstance(slot, str) or not _SLOT_RE.match(slot):
            raise ConfigError(f"slots: {slot!r} is not HH:MM (quote it in YAML)")
        if slot not in CRON_SLOTS:
            raise ConfigError(
                f"slots: {slot} has no slot cron — must be one of {list(CRON_SLOTS)}"
            )
    if len(set(slots)) != len(slots):
        raise ConfigError("slots lists a slot twice")

    per_slot = _int_at_least(data, "per_slot_cap", 1)
    per_day = _int_at_least(data, "per_day_cap", 1)
    if per_day < per_slot:
        raise ConfigError("per_day_cap must be >= per_slot_cap")
    regates = _int_at_least(data, "max_regate_per_pr", 0)

    ordered_days = tuple(sorted(days, key=WEEKDAYS.index))
    ordered_slots = tuple(sorted(slots))
    raw = data.get("schedule")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("schedule must map owner/repo to releases")
    schedule: dict[str, tuple[Entry, ...]] = {}
    for repo, entries in raw.items():
        # The key goes into GitHub API paths, so `.`/`..` parts are refused too.
        if (
            not isinstance(repo, str)
            or not _REPO_RE.match(repo)
            or any(part in (".", "..") for part in repo.split("/"))
        ):
            raise ConfigError(f"schedule: {repo!r} is not owner/repo")
        releases = parse_releases(
            entries, ordered_days, ordered_slots, f"schedule[{repo}]"
        )
        if not releases:
            raise ConfigError(f"schedule[{repo}]: list at least one release")
        schedule[repo] = releases
    return Config(
        days=ordered_days,
        slots=ordered_slots,
        per_slot_cap=per_slot,
        per_day_cap=per_day,
        max_regate_per_pr=regates,
        schedule=schedule,
    )


# ── Phase and time ───────────────────────────────────────────────────────


def phase_from_cron(cron: str) -> str:
    phase = CRON_PHASES.get(" ".join(cron.split()))
    if phase is None:
        raise ValueError(f"cron {cron!r} is not one of the release train's crons")
    return phase


def resolve_phase(requested: str, cron: str) -> str:
    """``--phase`` wins; ``auto`` reads the cron; a manual ``auto`` dispatch
    (no cron) only refreshes the plan, which is the phase that ships nothing."""
    if requested and requested != "auto":
        if requested not in PHASES:
            raise ValueError(f"unknown phase {requested!r}")
        return requested
    if cron.strip():
        return phase_from_cron(cron)
    return SYNC


def to_ist(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime: pass an aware one")
    return moment.astimezone(IST)


def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def previous_week(week: str) -> str:
    year, num = week.split("-W")
    return iso_week(date.fromisocalendar(int(year), int(num), 1) - timedelta(days=7))


def weekday(day: date) -> str:
    return WEEKDAYS[day.weekday()]


def slot_at(day: date, slot: str) -> datetime:
    hour, minute = (int(part) for part in slot.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def slot_key(moment: datetime) -> str:
    return to_ist(moment).strftime("%Y-%m-%dT%H:%M+05:30")


def parse_slot_key(key: str) -> datetime:
    return datetime.strptime(key, "%Y-%m-%dT%H:%M%z").astimezone(IST)


def slot_week(key: str) -> str:
    return iso_week(parse_slot_key(key).date())


def format_slot(moment: datetime) -> str:
    """``Mon 26 Oct 12:00 IST``."""
    local = to_ist(moment)
    return f"{local:%a} {local.day} {local:%b %H:%M} IST"


def current_slot(now: datetime, slots: Iterable[str]) -> str | None:
    """The latest slot at or before ``now`` today — derived from the clock, not
    the cron, because a scheduled run can start well after its minute."""
    local = to_ist(now)
    due = [s for s in slots if slot_at(local.date(), s) <= local]
    return max(due) if due else None


def next_slot(
    schedule: Iterable[Entry],
    after: datetime,
    *,
    skipped: Iterable[str] = (),
    inclusive: bool = False,
    horizon_days: int = 21,
) -> datetime | None:
    """The earliest scheduled slot after (or at, when ``inclusive``) ``after``
    that is not in ``skipped`` (slot keys)."""
    local = to_ist(after)
    skip = set(skipped)
    entries = tuple(schedule)
    for offset in range(horizon_days):
        day = local.date() + timedelta(days=offset)
        for entry_day, slot in sorted(entries, key=lambda e: e[1]):
            if entry_day != weekday(day):
                continue
            moment = slot_at(day, slot)
            if (moment > local or (inclusive and moment == local)) and (
                slot_key(moment) not in skip
            ):
                return moment
    return None


def todays_slot(schedule: Iterable[Entry], today: date) -> str | None:
    for day, slot in schedule:
        if day == weekday(today):
            return slot
    return None


# ── Caps and overflow ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Due:
    """An app due today: ``requested`` is the slot it asks for (its schedule,
    or the one after a deferral); ``done`` the slot it was released in already."""

    repo: str
    requested: str
    done: str | None = None


def plan_day(
    dues: Iterable[Due], slots: tuple[str, ...], per_slot_cap: int, per_day_cap: int
) -> dict[str, str | None]:
    """repo → the slot it runs in today, or ``None`` when no slot has room.

    Apps already released hold their slot. The rest are placed in their
    requested slot, overflowing to the next slot with room; past the day cap
    (or the last slot) they wait for their next scheduled release. Ordered by
    (slot, repo) so every run of the day computes the same plan.
    """
    used = dict.fromkeys(slots, 0)
    total = 0
    plan: dict[str, str | None] = {}
    pending: list[Due] = []
    for due in dues:
        if due.done:
            plan[due.repo] = due.done
            used[due.done] = used.get(due.done, 0) + 1
            total += 1
        else:
            pending.append(due)
    rank = {slot: i for i, slot in enumerate(slots)}
    for due in sorted(pending, key=lambda d: (rank.get(d.requested, 99), d.repo)):
        plan[due.repo] = None
        if total >= per_day_cap or due.requested not in rank:
            continue
        for slot in slots[rank[due.requested] :]:
            if used[slot] < per_slot_cap:
                plan[due.repo] = slot
                used[slot] += 1
                total += 1
                break
    return plan


# ── The record ───────────────────────────────────────────────────────────


@dataclass
class Record:
    week: str
    repo: str
    schedule: list[dict] = field(default_factory=list)
    state: str = PLANNED
    reason: str | None = None
    events: list[dict] = field(default_factory=list)
    regates: int = 0
    gm: dict = field(default_factory=lambda: {"stage": "unknown"})
    seen_commands: list[int] = field(default_factory=list)
    skip_weeks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "week": self.week,
            "repo": self.repo,
            "schedule": self.schedule,
            "state": self.state,
            "reason": self.reason,
            "events": self.events,
            "regates": self.regates,
            "gm": self.gm,
            "seen_commands": self.seen_commands,
            "skip_weeks": self.skip_weeks,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Record:
        state = data.get("state")
        return cls(
            week=str(data["week"]),
            repo=str(data["repo"]),
            schedule=[e for e in data.get("schedule") or [] if isinstance(e, dict)],
            state=state if state in STATES else PLANNED,
            reason=data.get("reason"),
            events=[e for e in data.get("events") or [] if isinstance(e, dict)],
            regates=int(data.get("regates") or 0),
            gm=data.get("gm")
            if isinstance(data.get("gm"), dict)
            else {"stage": "unknown"},
            seen_commands=[
                int(i) for i in data.get("seen_commands") or [] if isinstance(i, int)
            ],
            skip_weeks=[str(w) for w in data.get("skip_weeks") or []],
        )


def event_key(event: dict) -> tuple:
    """``(week, slot, action, sha)`` — plus the comment id for a command, so a
    skip, a resume and a second skip of the same slot are three events."""
    slot = event.get("slot") or ""
    week = slot_week(slot) if slot else ""
    return (week, slot, event.get("action"), event.get("sha"), event.get("comment_id"))


def has_event(
    record: Record,
    action: str,
    slot: str | None,
    sha: str | None,
    comment_id: int | None = None,
) -> bool:
    key = event_key(
        {"slot": slot, "action": action, "sha": sha, "comment_id": comment_id}
    )
    return any(event_key(e) == key for e in record.events)


def add_event(
    record: Record,
    action: str,
    *,
    slot: str | None,
    sha: str | None,
    at: datetime,
    by: str = JOB_ACTOR,
    **extra: Any,
) -> bool:
    """Append an event unless one with the same key exists. False = already done."""
    if has_event(record, action, slot, sha, extra.get("comment_id")):
        return False
    event = {
        "at": at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slot": slot,
        "action": action,
        "sha": sha,
        "by": by,
    }
    event.update({k: v for k, v in extra.items() if v is not None})
    record.events.append(event)
    return True


def last_event(record: Record, action: str) -> dict | None:
    for event in reversed(record.events):
        if event.get("action") == action:
            return event
    return None


def serialize_record(record: Record) -> str:
    """The hidden block. ``<``/``>``/``&`` are JSON-escaped so no value — a skip
    reason included — can close the HTML comment around it."""
    text = json.dumps(record.to_dict(), separators=(", ", ": "))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"<!-- release-train:record v1\n{text}\n-->"


def parse_record(body: str) -> Record | None:
    match = _RECORD_RE.search(body or "")
    if not match:
        return None
    try:
        data = json.loads(match.group("json"))
        return Record.from_dict(data) if isinstance(data, dict) else None
    except (ValueError, KeyError, TypeError):
        return None


def roll_week(record: Record, week: str) -> Record:
    """A fresh week. Skips that target this week or later survive (a skip posted
    after Thursday's last slot is for next Monday); so does the skip history."""
    if record.week == week:
        return record
    skipped_last = any(
        e.get("action") == "skipped" and slot_week(e.get("slot") or "") == record.week
        for e in record.events
        if e.get("slot")
    )
    weeks = record.skip_weeks + ([record.week] if skipped_last else [])
    keep = [
        e
        for e in record.events
        if e.get("action") in ("skipped", "resumed", "expired")
        and e.get("slot")
        and slot_week(e["slot"]) >= week
    ]
    return Record(
        week=week,
        repo=record.repo,
        schedule=record.schedule,
        state=PLANNED,
        reason=None,
        events=keep,
        regates=0,
        gm=record.gm,
        seen_commands=record.seen_commands,
        skip_weeks=sorted(set(weeks))[-MAX_SKIP_WEEKS:],
    )


def pending_skips(record: Record) -> set[str]:
    """Slot keys skipped and not since resumed or expired."""
    skips: set[str] = set()
    for event in record.events:
        slot = event.get("slot")
        if not slot:
            continue
        if event.get("action") == "skipped":
            skips.add(slot)
        elif event.get("action") in ("resumed", "expired"):
            skips.discard(slot)
    return skips


def skipped_this_week(record: Record) -> bool:
    return any(
        e.get("action") == "skipped" and slot_week(e["slot"]) == record.week
        for e in record.events
        if e.get("slot")
    )


def consecutive_skip_weeks(record: Record) -> bool:
    return skipped_this_week(record) and previous_week(record.week) in record.skip_weeks


# ── Commands ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Command:
    kind: str  # "skip" | "resume"
    comment_id: int
    login: str
    created_at: datetime
    reason: str = ""


def parse_command(body: str) -> tuple[str, str] | None:
    """``("skip", reason)`` / ``("resume", "")`` from a comment's first line, or
    ``None``. Anchored at the start, so a comment that merely mentions the
    command (the release-day notice does) is not one."""
    lines = (body or "").strip().splitlines()
    first = lines[0].strip() if lines else ""
    skip = _SKIP_RE.match(first)
    if skip:
        return "skip", (skip.group("reason") or "").strip()
    if _RESUME_RE.match(first):
        return "resume", ""
    return None


def is_bot(user: dict) -> bool:
    login = str(user.get("login") or "")
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def honour_commands(
    comments: Iterable[dict], *, seen: Iterable[int], can_push: Callable[[str], bool]
) -> tuple[list[Command], list[tuple[int, str]]]:
    """(honoured, refused) commands not yet seen, oldest first.

    A permission lookup that fails leaves the command unseen so the next run
    asks again; authorisation never fails open.
    """
    seen_ids = set(seen)
    honoured: list[Command] = []
    refused: list[tuple[int, str]] = []
    rows = sorted(comments, key=lambda c: (c.get("created_at") or "", c.get("id") or 0))
    for comment in rows:
        comment_id = comment.get("id")
        if not isinstance(comment_id, int) or comment_id in seen_ids:
            continue
        parsed = parse_command(comment.get("body") or "")
        if parsed is None:
            continue
        kind, reason = parsed
        user = comment.get("user") or {}
        login = str(user.get("login") or "")
        created = _ts(comment.get("created_at"))
        if is_bot(user):
            refused.append((comment_id, "bots cannot steer the release train"))
            continue
        if created is None or not login:
            refused.append((comment_id, "comment has no author or timestamp"))
            continue
        if kind == "skip" and not reason:
            refused.append((comment_id, "a reason is required: /release-skip <reason>"))
            continue
        try:
            allowed = can_push(login)
        except Exception:  # noqa: BLE001 — any failure refuses, retried later
            print("::warning::a commenter permission lookup failed; retried next run")
            continue
        if not allowed:
            refused.append((comment_id, "needs the Write, Maintain or Admin role"))
            continue
        honoured.append(Command(kind, comment_id, login, created, reason))
    return honoured, refused


def apply_commands(
    record: Record, commands: Iterable[Command], schedule: tuple[Entry, ...]
) -> list[int]:
    """Fold honoured commands into the record; returns the ids to react 👍 to."""
    reacted: list[int] = []
    for cmd in commands:
        record.seen_commands = (record.seen_commands + [cmd.comment_id])[
            -MAX_SEEN_COMMANDS:
        ]
        if cmd.kind == "skip":
            target = next_slot(schedule, cmd.created_at)
            if target is None:
                continue  # nothing scheduled to skip: honoured, but a no-op
            add_event(
                record,
                "skipped",
                slot=slot_key(target),
                sha=None,
                at=cmd.created_at,
                by=cmd.login,
                reason=_inert(cmd.reason),
                comment_id=cmd.comment_id,
            )
        else:
            now = to_ist(cmd.created_at)
            for slot in sorted(pending_skips(record)):
                if parse_slot_key(slot) > now:
                    add_event(
                        record,
                        "resumed",
                        slot=slot,
                        sha=None,
                        at=cmd.created_at,
                        by=cmd.login,
                        comment_id=cmd.comment_id,
                    )
        reacted.append(cmd.comment_id)
    return reacted


# ── Eligibility ──────────────────────────────────────────────────────────


def required_contexts(rules: Iterable[dict]) -> set[str]:
    contexts: set[str] = set()
    for rule in rules:
        if (rule or {}).get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        for check in params.get("required_status_checks") or []:
            if isinstance(check, dict) and check.get("context"):
                contexts.add(str(check["context"]))
    return contexts


def _requires(contexts: set[str], name: str) -> bool:
    return any(c == name or c.endswith(f" / {name}") for c in contexts)


def release_model(atlan_yaml_text: str) -> str | None:
    """``release_model`` as parse_atlan_yaml.py reads it (default ``cd``,
    ``versioned`` an alias of ``semver``); ``None`` if unparseable."""
    try:
        data = yaml.safe_load(atlan_yaml_text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    model = data.get("release_model", "cd")
    return "semver" if model == "versioned" else str(model)


def eligibility(
    repo: str,
    *,
    atlan_yaml: str | None,
    release_gate: str | None,
    rules: list[dict] | None,
) -> str | None:
    """``None`` when the app may ride the train, else the next action."""
    if atlan_yaml is None:
        return "no atlan.yaml on main"
    model = release_model(atlan_yaml)
    if model is None:
        return "atlan.yaml on main is not valid YAML"
    if model != "semver":
        return f"set release_model: semver in atlan.yaml (is {model})"
    if release_gate is None or "gate-check" not in release_gate:
        return "Release Gate predates /gate-check — run bootstrap --resync"
    if rules is None:
        return "main ruleset unreadable"
    contexts = required_contexts(rules)
    missing = [name for name in REQUIRED_CHECKS if not _requires(contexts, name)]
    if missing:
        return "main ruleset must require " + " and ".join(missing)
    return None


def gm_canary_stage(repo: str) -> str:
    """GM canary stage for the app's current release.

    TODO(FND-4044): read the real canary stage. Until then always "unknown",
    which pre-flight does not block on.
    """
    del repo
    return "unknown"


def codeowners(text: str | None) -> list[str]:
    """Owners of the last ``*`` rule in CODEOWNERS (the default owners)."""
    owners: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].split()
        if len(line) >= 2 and line[0] in ("*", "/*", "/**"):
            owners = [o for o in line[1:] if re.match(r"^@[A-Za-z0-9_./-]+$", o)]
    return owners


def dispatchable(workflow_text: str | None) -> bool:
    """Whether a workflow file declares ``workflow_dispatch``."""
    if not workflow_text:
        return False
    try:
        doc = yaml.safe_load(workflow_text) or {}
    except yaml.YAMLError:
        return False
    if not isinstance(doc, dict):
        return False
    # PyYAML reads a bare `on:` key as boolean True.
    triggers = doc.get("on", doc.get(True))
    if isinstance(triggers, str):
        return triggers == "workflow_dispatch"
    if isinstance(triggers, (list, dict)):
        return "workflow_dispatch" in triggers
    return False


# ── Rendering ────────────────────────────────────────────────────────────


def _inert(text: str | None, limit: int = REASON_MAX) -> str:
    """One line, no backticks, bounded: safe inside an inline code span."""
    flat = " ".join((text or "").replace("`", "'").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


_HTTP_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


def public_error(exc: BaseException) -> str:
    """What a public run log may say about a failure: the HTTP status, never the
    message (it can quote a private repo's paths, titles or API detail)."""
    match = _HTTP_STATUS_RE.search(str(exc))
    if match:
        return f"GitHub API error (HTTP {match.group(1)})"
    return f"{type(exc).__name__}; detail on the bump PR status comment"


def base_title(title: str) -> str:
    return (title or "").split(TITLE_SEP, 1)[0].rstrip()


def render_title(
    base: str, state: str, *, next_at: datetime | None, reason: str | None
) -> str:
    when = format_slot(next_at) if next_at else None
    if state == PLANNED:
        return f"{base}{TITLE_SEP}Next release {when}" if when else base
    if state == SKIPPED:
        suffix = f"Skipped, next release {when}" if when else "Skipped"
        return f"{base}{TITLE_SEP}{suffix}"
    if state == BLOCKED:
        return f"{base}{TITLE_SEP}Blocked: {_inert(reason, 80)}"
    if state == NOT_ON_TRAIN:
        return f"{base}{TITLE_SEP}Not on the train: {_inert(reason, 80)}"
    return base


def _schedule_text(schedule: Iterable[Entry]) -> str:
    entries = [f"{day.capitalize()} {slot} IST" for day, slot in schedule]
    return ", ".join(entries) if entries else "none"


def render_status(
    record: Record, *, next_at: datetime | None, gate: str | None, head_sha: str | None
) -> str:
    schedule = tuple((e.get("day", ""), e.get("slot", "")) for e in record.schedule)
    rows = [
        ("Schedule", f"{_schedule_text(schedule)} ({SCHEDULE_SOURCE})"),
        ("Next release", format_slot(next_at) if next_at else "—"),
        ("State", f"`{record.state}`"),
    ]
    if record.reason:
        rows.append(("Reason", f"`{_inert(record.reason)}`"))
    sha = (head_sha or "")[:7]
    rows.append(("Gate", f"`gate-check` {gate or 'not requested'} on `{sha or '—'}`"))
    skips = sorted(pending_skips(record))
    if skips:
        rows.append(
            ("Skipped", ", ".join(format_slot(parse_slot_key(s)) for s in skips))
        )
    table = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return (
        f"{STATUS_MARKER}\n"
        "### Release train\n\n"
        "| | |\n|---|---|\n"
        f"{table}\n\n"
        f"`{SKIP_COMMAND} <reason>` skips the next release slot; "
        f"`{RESUME_COMMAND}` cancels a pending skip. Only people with the Write, "
        "Maintain or Admin role count. See application-sdk "
        "`docs/standards/release-flow.md#release-train-scheduler`.\n\n"
        f"{serialize_record(record)}\n"
    )


def render_notice(owners: Iterable[str], slot: str) -> str:
    mention = " ".join(owners)
    lead = f"{mention} " if mention else ""
    return f"{lead}Releasing today at {slot} IST. To skip: {SKIP_COMMAND} <reason>"


# ── GitHub I/O ───────────────────────────────────────────────────────────


class Gh:
    """Every GitHub call, through the ``gh`` CLI (``GH_TOKEN`` is the fleet App)."""

    def __init__(self, runner: Runner = subprocess.run) -> None:
        self._runner = runner

    def run(self, args: list, stdin: str | None = None) -> subprocess.CompletedProcess:
        """One ``gh`` call, bounded; also the ``pr_test_command.GhFn`` seam."""
        try:
            return self._runner(
                ["gh", *args],
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT,
                input=stdin,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(["gh", *args], 124, "", "timed out")

    def _read(self, args: list) -> subprocess.CompletedProcess:
        result = self.run(args)
        for _ in range(GH_ATTEMPTS - 1):
            if result.returncode == 0 or not is_transient(result.stderr or ""):
                break
            result = self.run(args)
        return result

    def get(self, path: str) -> Any:
        """JSON from a GET; ``None`` on 404."""
        result = self._read(["api", path])
        if result.returncode != 0:
            if "HTTP 404" in (result.stderr or ""):
                return None
            raise GhError(f"GET {path}: {(result.stderr or '').strip()[-300:]}")
        try:
            return json.loads(result.stdout or "null")
        except ValueError as exc:
            raise GhError(f"GET {path}: unparseable response") from exc

    def get_paged(self, path: str) -> list:
        result = self._read(["api", "--paginate", "--slurp", path])
        if result.returncode != 0:
            raise GhError(f"GET {path}: {(result.stderr or '').strip()[-300:]}")
        try:
            return flatten(json.loads(result.stdout or "[]"))
        except ValueError as exc:
            raise GhError(f"GET {path}: unparseable response") from exc

    def file(self, repo: str, path: str, ref: str = BASE_BRANCH) -> str | None:
        result = self._read(
            [
                "api",
                "-H",
                "Accept: application/vnd.github.raw",
                f"repos/{repo}/contents/{path}?ref={ref}",
            ]
        )
        if result.returncode != 0:
            if "HTTP 404" in (result.stderr or ""):
                return None
            raise GhError(f"reading {repo}:{path}: {(result.stderr or '')[-300:]}")
        return result.stdout

    def query(self, query: str, variables: dict) -> dict:
        """A read-only GraphQL query (never a mutation — see :class:`Writer`)."""
        if query.lstrip().startswith("mutation"):
            raise GhError("Gh.query is read-only; mutations go through Writer")
        return self._graphql(query, variables, read=True)

    def _graphql(self, query: str, variables: dict, *, read: bool) -> dict:
        body = json.dumps({"query": query, "variables": variables})
        args = ["api", "graphql", "--input", "-"]
        result = self.run(args, stdin=body)
        if read:
            for _ in range(GH_ATTEMPTS - 1):
                if result.returncode == 0 or not is_transient(result.stderr or ""):
                    break
                result = self.run(args, stdin=body)
        if result.returncode != 0:
            raise GhError(f"graphql: {(result.stderr or '').strip()[-300:]}")
        data = json.loads(result.stdout or "{}")
        if data.get("errors"):
            raise GhError(f"graphql: {data['errors']}")
        return data.get("data") or {}

    def write(self, method: str, path: str, payload: dict | None = None) -> Any:
        args = ["api", "-X", method, path]
        stdin = None
        if payload is not None:
            args += ["--input", "-"]
            stdin = json.dumps(payload)
        result = self.run(args, stdin=stdin)
        if result.returncode != 0:
            raise GhError(f"{method} {path}: {(result.stderr or '').strip()[-300:]}")
        return json.loads(result.stdout or "null") if result.stdout else None

    def mutate(self, query: str, variables: dict) -> dict:
        return self._graphql(query, variables, read=False)


_ENABLE_AUTOMERGE = """
mutation($id: ID!, $method: PullRequestMergeMethod!, $sha: GitObjectID!) {
  enablePullRequestAutoMerge(
    input: {pullRequestId: $id, mergeMethod: $method, expectedHeadOid: $sha}
  ) { pullRequest { number } }
}
"""

_DISABLE_AUTOMERGE = """
mutation($id: ID!) {
  disablePullRequestAutoMerge(input: {pullRequestId: $id}) {
    pullRequest { number }
  }
}
"""

_PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    autoMergeAllowed
    squashMergeAllowed
    mergeCommitAllowed
    rebaseMergeAllowed
    pullRequest(number: $number) {
      id
      autoMergeRequest { enabledBy { login } }
    }
  }
}
"""


class Writer:
    """The only path to a mutation. Not live: print the intent, do nothing."""

    def __init__(
        self,
        gh: Gh,
        *,
        live: bool,
        log: Callable[[str], None] = print,
        verbose: bool = False,
    ):
        self.gh = gh
        self.live = live
        self.log = log
        self.verbose = verbose
        self.intended: list[str] = []
        self.kinds: dict[str, int] = {}

    def _do(self, kind: str, what: str, action: Callable[[], Any]) -> Any:
        # `what` names the repo, so it is logged only with --verbose (local runs);
        # the public log gets the per-kind counts in the summary.
        self.intended.append(what)
        self.kinds[kind] = self.kinds.get(kind, 0) + 1
        if self.verbose:
            self.log(f"  {'' if self.live else '[dry-run] would '}{what}")
        if not self.live:
            return None
        return action()

    def comment(self, repo: str, number: int, body: str) -> Any:
        return self._do(
            "comment",
            f"comment on {repo}#{number}",
            lambda: self.gh.write(
                "POST", f"repos/{repo}/issues/{number}/comments", {"body": body}
            ),
        )

    def edit_comment(self, repo: str, comment_id: int, body: str) -> Any:
        return self._do(
            "edit status comment",
            f"edit status comment {comment_id} on {repo}",
            lambda: self.gh.write(
                "PATCH", f"repos/{repo}/issues/comments/{comment_id}", {"body": body}
            ),
        )

    def set_title(self, repo: str, number: int, title: str) -> Any:
        return self._do(
            "retitle",
            f"retitle {repo}#{number}",
            lambda: self.gh.write(
                "PATCH", f"repos/{repo}/pulls/{number}", {"title": title}
            ),
        )

    def react(self, repo: str, comment_id: int) -> Any:
        return self._do(
            "react",
            f"react +1 to comment {comment_id} on {repo}",
            lambda: self.gh.write(
                "POST",
                f"repos/{repo}/issues/comments/{comment_id}/reactions",
                {"content": "+1"},
            ),
        )

    def enable_automerge(
        self, repo: str, number: int, node_id: str, method: str, sha: str
    ) -> Any:
        return self._do(
            "enable auto-merge",
            f"enable auto-merge ({method}) on {repo}#{number}",
            lambda: self.gh.mutate(
                _ENABLE_AUTOMERGE, {"id": node_id, "method": method, "sha": sha}
            ),
        )

    def disable_automerge(self, repo: str, number: int, node_id: str) -> Any:
        return self._do(
            "disable auto-merge",
            f"disable auto-merge on {repo}#{number}",
            lambda: self.gh.mutate(_DISABLE_AUTOMERGE, {"id": node_id}),
        )

    def dispatch(self, repo: str, workflow: str, ref: str) -> Any:
        return self._do(
            "dispatch",
            f"dispatch {workflow} on {repo}@{ref}",
            lambda: self.gh.write(
                "POST",
                f"repos/{repo}/actions/workflows/{workflow}/dispatches",
                {"ref": ref},
            ),
        )


# ── One app ──────────────────────────────────────────────────────────────


@dataclass
class PullRequest:
    number: int
    node_id: str
    title: str
    head_sha: str
    auto_merge_by: str | None = None
    auto_merge_allowed: bool = False
    method: str | None = None


@dataclass
class App:
    repo: str
    not_eligible: str | None = None
    schedule: tuple[Entry, ...] = ()
    pr: PullRequest | None = None
    ahead_by: int | None = None
    behind_by: int | None = None
    gate: str | None = None
    status_comment_id: int | None = None
    status_body: str | None = None
    record: Record | None = None
    comments: list[dict] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    merged: list[MergedPR] = field(default_factory=list)


@dataclass
class MergedPR:
    """A bump PR merged this week, with the job's status comment on it."""

    number: int
    sha: str
    comment_id: int
    record: Record | None


def status_comment(
    comments: Iterable[dict], bot_login: str
) -> tuple[dict | None, list[dict]]:
    """(the job's status comment, everyone else's comments).

    Only a comment the job's own App posted can hold state — a marker anyone
    else writes is ignored — and the App's comments are never commands.
    """
    mine: dict | None = None
    others: list[dict] = []
    for comment in comments:
        user = comment.get("user") or {}
        # Exact REST login, and a Bot account: a user whose login merely
        # resembles the App's slug cannot pass for it.
        if user.get("login") == bot_login and user.get("type") == "Bot":
            if mine is None and STATUS_MARKER in (comment.get("body") or ""):
                mine = comment
            continue
        others.append(comment)
    return mine, others


def gate_state(statuses: Iterable[dict]) -> str | None:
    """Newest ``gate-check`` state posted by the verdict identity, else None."""
    for row in statuses:  # the API lists newest first
        if row.get("context") != GATE_CHECK_CONTEXT:
            continue
        if ((row.get("creator") or {}).get("login")) == GATE_VERDICT_AUTHOR:
            return row.get("state")
    return None


def merged_this_week(repo: str, gh: Gh, bot_login: str, week: str) -> list[MergedPR]:
    owner = repo.split("/", 1)[0]
    found: list[MergedPR] = []
    closed = gh.get(
        f"repos/{repo}/pulls?state=closed&head={owner}:{BUMP_BRANCH}"
        f"&base={BASE_BRANCH}&sort=updated&direction=desc&per_page=5"
    )
    for pull in closed or []:
        merged_at = _ts(pull.get("merged_at"))
        if merged_at is None or iso_week(to_ist(merged_at).date()) != week:
            continue
        mine, _ = status_comment(
            gh.get_paged(f"repos/{repo}/issues/{pull['number']}/comments?per_page=100"),
            bot_login,
        )
        if mine is None or not isinstance(mine.get("id"), int):
            continue
        found.append(
            MergedPR(
                number=int(pull["number"]),
                sha=str(pull.get("merge_commit_sha") or ""),
                comment_id=mine["id"],
                record=parse_record(mine.get("body") or ""),
            )
        )
    return found


def load_app(
    repo: str,
    gh: Gh,
    config: Config,
    bot_login: str,
    *,
    week: str = "",
    with_merged: bool = False,
) -> App:
    """Everything the phases decide on, read once (reads only — no writes)."""
    app = App(repo=repo)
    if with_merged and week:
        app.merged = merged_this_week(repo, gh, bot_login, week)
    rules = gh.get(f"repos/{repo}/rules/branches/{BASE_BRANCH}")
    app.not_eligible = eligibility(
        repo,
        atlan_yaml=gh.file(repo, ATLAN_YAML),
        release_gate=gh.file(repo, RELEASE_GATE_SCRIPT),
        rules=rules if isinstance(rules, list) else None,
    )
    app.schedule = config.schedule.get(repo, ())
    app.owners = codeowners(gh.file(repo, CODEOWNERS_PATH))

    owner, name = repo.split("/", 1)
    pulls = gh.get(
        f"repos/{repo}/pulls?state=open&head={owner}:{BUMP_BRANCH}"
        f"&base={BASE_BRANCH}&per_page=10"
    )
    if not pulls:
        return app
    pull = pulls[0]
    data = gh.query(_PR_QUERY, {"owner": owner, "name": name, "number": pull["number"]})
    repository = data.get("repository") or {}
    node = repository.get("pullRequest") or {}
    armed = node.get("autoMergeRequest") or {}
    app.pr = PullRequest(
        number=int(pull["number"]),
        node_id=str(node.get("id") or pull.get("node_id") or ""),
        title=str(pull.get("title") or ""),
        head_sha=str((pull.get("head") or {}).get("sha") or ""),
        auto_merge_by=((armed.get("enabledBy") or {}).get("login")) if armed else None,
        auto_merge_allowed=bool(repository.get("autoMergeAllowed")),
        method=merge_method(repository),
    )
    compare = gh.get(f"repos/{repo}/compare/{BASE_BRANCH}...{app.pr.head_sha}") or {}
    ahead, behind = compare.get("ahead_by"), compare.get("behind_by")
    app.ahead_by = ahead if isinstance(ahead, int) else None
    app.behind_by = behind if isinstance(behind, int) else None
    app.gate = gate_state(
        gh.get(f"repos/{repo}/commits/{app.pr.head_sha}/statuses?per_page=100") or []
    )
    mine, app.comments = status_comment(
        gh.get_paged(f"repos/{repo}/issues/{app.pr.number}/comments?per_page=100"),
        bot_login,
    )
    if mine is not None:
        app.status_comment_id = mine.get("id")
        app.status_body = mine.get("body") or ""
        app.record = parse_record(app.status_body)
    return app


# ── The train ────────────────────────────────────────────────────────────


@dataclass
class Row:
    repo: str
    state: str
    detail: str = ""


def same_login(a: str | None, b: str | None) -> bool:
    """Compare logins across APIs: REST says ``slug[bot]``, GraphQL's
    ``enabledBy`` may say ``slug``, and ``gh`` renders ``app/slug``."""

    def bare(login: str | None) -> str:
        text = (login or "").strip().lower()
        return text.removeprefix("app/").removesuffix("[bot]")

    return bool(a and b) and bare(a) == bare(b)


def in_flight(record: Record) -> dict | None:
    """The latest ``gate_requested``/``armed`` event not yet disarmed, if any."""
    for event in reversed(record.events):
        action = event.get("action")
        if action == "disarmed":
            return None
        if action in ("armed", "gate_requested"):
            return event
    return None


class Train:
    def __init__(
        self,
        config: Config,
        gh: Gh,
        writer: Writer,
        *,
        now: datetime,
        bot_login: str,
        report_only: bool = False,
    ) -> None:
        self.config = config
        self.gh = gh
        self.writer = writer
        self.now = to_ist(now)
        self.today = self.now.date()
        self.week = iso_week(self.today)
        self.bot_login = bot_login
        self.report_only = report_only
        self.rows: list[Row] = []
        self.notes: list[str] = []
        self.errors: list[str] = []

    # ― shared steps ―

    def record_for(self, app: App) -> Record:
        record = app.record or Record(week=self.week, repo=app.repo)
        record = roll_week(record, self.week)
        record.schedule = [{"day": d, "slot": s} for d, s in app.schedule]
        record.gm = {"stage": gm_canary_stage(app.repo)}
        return record

    def _commands(self, app: App, record: Record) -> None:
        honoured, refused = honour_commands(
            app.comments,
            seen=record.seen_commands,
            can_push=ptc.GitHub(app.repo, run=self.gh.run).can_push,
        )
        for comment_id, why in refused:
            self.notes.append(f"{app.repo}: ignored command {comment_id} ({why})")
            record.seen_commands = (record.seen_commands + [comment_id])[
                -MAX_SEEN_COMMANDS:
            ]
        for comment_id in apply_commands(record, honoured, app.schedule):
            self.writer.react(app.repo, comment_id)

    def _released_today(self, record: Record) -> str | None:
        """The slot (HH:MM) the app was gate-requested in today, if any."""
        for event in record.events:
            if event.get("action") == "gate_requested" and event.get("slot"):
                moment = parse_slot_key(event["slot"])
                if moment.date() == self.today:
                    return f"{moment:%H:%M}"
        return None

    def _deferral(self, record: Record) -> dict | None:
        """Today's latest deferral that still points at a slot today."""
        latest = None
        for event in record.events:
            target = event.get("deferred_to")
            if event.get("action") != "deferred" or not target:
                continue
            if parse_slot_key(target).date() == self.today:
                latest = event
        return latest

    def _requested_slot(self, app: App, record: Record) -> str | None:
        """The slot the app asks for today: its schedule, or after a deferral."""
        slot = todays_slot(app.schedule, self.today)
        if slot is None:
            return None
        if slot_key(slot_at(self.today, slot)) in pending_skips(record):
            return None
        deferral = self._deferral(record)
        if deferral:
            return f"{parse_slot_key(deferral['deferred_to']):%H:%M}"
        return slot

    def _ready(self, app: App) -> bool:
        return app.not_eligible is None and bool(app.schedule) and app.pr is not None

    def plan(self, pairs: Iterable[tuple[App, Record]]) -> dict[str, str | None]:
        dues: list[Due] = []
        for app, record in pairs:
            if not self._ready(app):
                continue
            done = self._released_today(record)
            requested = self._requested_slot(app, record)
            if done or requested:
                dues.append(Due(app.repo, requested or done or "", done))
        return plan_day(
            dues, self.config.slots, self.config.per_slot_cap, self.config.per_day_cap
        )

    def _next_release(
        self, app: App, record: Record, plan: dict[str, str | None]
    ) -> datetime | None:
        """Next slot, honouring today's (possibly overflowed) plan and skips."""
        assigned = plan.get(app.repo)
        if assigned and self._released_today(record) is None:
            return slot_at(self.today, assigned)
        after = self.now
        if app.repo in plan and assigned is None:
            after = slot_at(self.today, "23:59")  # overflowed out of today
        return next_slot(app.schedule, after, skipped=pending_skips(record))

    def _settle(
        self, app: App, record: Record, plan: dict[str, str | None]
    ) -> datetime | None:
        """State, reason and next slot for an app that is not mid-release."""
        if app.not_eligible:
            record.state, record.reason = NOT_ON_TRAIN, app.not_eligible
            return None
        if not app.schedule:
            record.state = NOT_ON_TRAIN
            record.reason = f"not listed in {SCHEDULE_SOURCE}"
            return None
        if app.ahead_by == 0:
            record.state, record.reason = BLOCKED, "bump PR has no changes over main"
            return None
        next_at = self._next_release(app, record, plan)
        upcoming = next_slot(app.schedule, self.now, inclusive=True)
        record.reason = None
        if upcoming is not None and slot_key(upcoming) in pending_skips(record):
            record.state = SKIPPED
            return next_at
        record.state = PLANNED
        deferral = self._deferral(record)
        if app.repo in plan and plan[app.repo] is None:
            record.reason = "no room in today's slots (caps); next scheduled day"
        elif deferral and parse_slot_key(deferral["deferred_to"]) > self.now:
            record.reason = deferral.get("reason")
        return next_at

    def _publish(self, app: App, record: Record, next_at: datetime | None) -> None:
        """Title and status comment — each written only when it changes."""
        if app.pr is None or self.report_only:
            return
        base = base_title(app.pr.title)
        title = render_title(base, record.state, next_at=next_at, reason=record.reason)
        if title != app.pr.title:
            self.writer.set_title(app.repo, app.pr.number, title)
            app.pr.title = title
            add_event(
                record,
                "titled",
                slot=slot_key(next_at) if next_at else None,
                sha=app.pr.head_sha,
                at=self.now,
                title=title,
            )
        body = render_status(
            record, next_at=next_at, gate=app.gate, head_sha=app.pr.head_sha
        )
        if app.status_comment_id is None:
            created = self.writer.comment(app.repo, app.pr.number, body)
            # A dry run creates nothing; -1 stands in so a second publish in
            # the same run edits rather than "creates" a second comment.
            app.status_comment_id = (created or {}).get("id") or -1
        elif body != app.status_body:
            self.writer.edit_comment(app.repo, app.status_comment_id, body)
        app.status_body = body

    def _disarm(self, app: App, record: Record, why: str) -> None:
        """Take back an in-flight release. Auto-merge is only disabled when
        this job's App armed it — never a human's."""
        event = in_flight(record)
        if app.pr is None or event is None:
            return
        if same_login(app.pr.auto_merge_by, self.bot_login) and not self.report_only:
            self.writer.disable_automerge(app.repo, app.pr.number, app.pr.node_id)
        add_event(
            record,
            "disarmed",
            slot=event.get("slot"),
            sha=event.get("sha"),
            at=self.now,
            reason=why,
        )
        record.state, record.reason = PLANNED, why

    def _row(self, app: App, record: Record | None, detail: str = "") -> None:
        if record is None:
            self.rows.append(Row(app.repo, "no bump PR", detail))
            return
        # Never the reason: the public log gets states and slot times only.
        self.rows.append(Row(app.repo, record.state, detail))

    def _each(
        self,
        pairs: list[tuple[App, Record]],
        step: Callable[[App, Record], None],
        *,
        publish: bool = True,
    ) -> None:
        """Run ``step`` per app; one app's failure never stops the fleet.

        A write that fails part-way still leaves the writes before it recorded
        (each event is added right after its write succeeds), so the record is
        published anyway: otherwise the next run would repeat them.
        """
        for app, record in pairs:
            try:
                step(app, record)
            except (GhError, ptc.GitHubError) as exc:
                self.errors.append(f"{app.repo}: {public_error(exc)}")
                if not publish:
                    continue
                record.reason = f"a GitHub call failed, retried next run: {exc}"
                try:
                    self._publish(app, record, None)
                except (GhError, ptc.GitHubError) as again:
                    self.errors.append(f"{app.repo}: {public_error(again)}")

    def _prepare(self, apps: list[App]) -> list[tuple[App, Record]]:
        pairs: list[tuple[App, Record]] = []
        for app in apps:
            if app.pr is None:
                self._row(app, None, "not eligible" if app.not_eligible else "")
                continue
            pairs.append((app, self.record_for(app)))
        self._each(pairs, self._commands, publish=False)
        return pairs

    def mark_merged(self, app: App) -> None:
        """Bump PRs merged this week whose record still says otherwise."""
        for merged in app.merged:
            record = merged.record
            if record is None or record.state == MERGED:
                continue
            record.state, record.reason = MERGED, None
            add_event(record, "merged", slot=None, sha=merged.sha, at=self.now)
            self.rows.append(Row(app.repo, MERGED, f"#{merged.number}"))
            if not self.report_only:
                self.writer.edit_comment(
                    app.repo,
                    merged.comment_id,
                    render_status(record, next_at=None, gate=None, head_sha=merged.sha),
                )

    # ― phases ―

    def sync(self, apps: list[App]) -> None:
        pairs = self._prepare(apps)
        plan = self.plan(pairs)

        def step(app: App, record: Record) -> None:
            assert app.pr is not None
            event = in_flight(record)
            if event and parse_slot_key(event["slot"]).date() < self.today:
                self._disarm(app, record, "armed on an earlier day and not merged")
            next_at = None
            if in_flight(record) is None:
                next_at = self._settle(app, record, plan)
            assigned = plan.get(app.repo)
            if (
                assigned
                and weekday(self.today) in self.config.days
                and record.state == PLANNED
                and self._released_today(record) is None
                and not self.report_only
                and add_event(
                    record,
                    "notified",
                    slot=slot_key(slot_at(self.today, assigned)),
                    sha=None,
                    at=self.now,
                )
            ):
                self.writer.comment(
                    app.repo, app.pr.number, render_notice(app.owners, assigned)
                )
            self._publish(app, record, next_at)
            self._row(app, record, format_slot(next_at) if next_at else "")

        self._each(pairs, step)
        for app in apps:
            self.mark_merged(app)

    def slot(self, apps: list[App]) -> None:
        current = current_slot(self.now, self.config.slots)
        pairs = self._prepare(apps)
        plan = self.plan(pairs)

        def step(app: App, record: Record) -> None:
            assigned = plan.get(app.repo)
            next_at: datetime | None = None
            if (
                current
                and assigned
                and assigned <= current
                and self._released_today(record) is None
            ):
                next_at = self._release(app, record, assigned)
            elif in_flight(record):
                self._regate(app, record, current)
            else:
                next_at = self._settle(app, record, plan)
            self._publish(app, record, next_at)
            self._row(app, record, format_slot(next_at) if next_at else "")

        self._each(pairs, step)

    def _release(self, app: App, record: Record, slot: str) -> datetime | None:
        """Pre-flight, then ``/gate-check`` and auto-merge. Returns the next
        slot to show when the app does not ship now."""
        pr = app.pr
        assert pr is not None
        key = slot_key(slot_at(self.today, slot))
        if not self._ready(app) or app.ahead_by == 0 or key in pending_skips(record):
            return self._settle(app, record, {})
        if app.behind_by is None:
            record.state, record.reason = BLOCKED, "could not compare the bump branch"
            return None
        if app.behind_by > 0:
            return self._defer(app, record, slot)
        if gm_canary_stage(app.repo) not in ("unknown", "ready"):
            record.state, record.reason = BLOCKED, "GM canary is not ready"
            return None
        if self.report_only:
            return None
        # Each event is recorded only after its write succeeded, so a failed
        # write is retried by the next run and a successful one never repeats.
        if not has_event(record, "gate_requested", key, pr.head_sha):
            # Exactly `/gate-check`: pr_test_command honours the fleet App's
            # request only when the body is the bare command (FND-4037).
            self.writer.comment(app.repo, pr.number, ptc.GATE_CHECK)
            add_event(record, "gate_requested", slot=key, sha=pr.head_sha, at=self.now)
        record.state, record.reason = GATE_REQUESTED, None
        self._arm(app, record, key)
        return None

    def _arm(self, app: App, record: Record, key: str) -> None:
        """Enable auto-merge, pinned to the head pre-flight checked."""
        pr = app.pr
        assert pr is not None
        if not pr.auto_merge_allowed or not pr.method:
            record.reason = "auto-merge is off in repo settings; merge by hand"
            return
        if not has_event(record, "armed", key, pr.head_sha):
            if pr.auto_merge_by is None:
                self.writer.enable_automerge(
                    app.repo, pr.number, pr.node_id, pr.method, pr.head_sha
                )
            add_event(record, "armed", slot=key, sha=pr.head_sha, at=self.now)
        record.state, record.reason = ARMED, None

    def _defer(self, app: App, record: Record, slot: str) -> datetime | None:
        """Behind main: /gate-check would refuse it. Refresh if we can, and try
        again in the next slot today."""
        assert app.pr is not None
        key = slot_key(slot_at(self.today, slot))
        later = [s for s in self.config.slots if s > slot]
        target = slot_at(self.today, later[0]) if later else None
        can_dispatch = dispatchable(
            self.gh.file(app.repo, f".github/workflows/{BUMP_WORKFLOW}")
        )
        if can_dispatch:
            reason = f"bump branch is {app.behind_by} behind main; refreshing it"
        else:
            reason = (
                f"bump branch is {app.behind_by} behind main and {BUMP_WORKFLOW} "
                "cannot be dispatched; the next merge to main refreshes it"
            )
        if not has_event(record, "deferred", key, app.pr.head_sha):
            if can_dispatch and not self.report_only:
                self.writer.dispatch(app.repo, BUMP_WORKFLOW, BASE_BRANCH)
            add_event(
                record,
                "deferred",
                slot=key,
                sha=app.pr.head_sha,
                at=self.now,
                deferred_to=slot_key(target) if target else None,
                reason=reason,
            )
        if target is None:
            record.state, record.reason = BLOCKED, reason
            return None
        record.state, record.reason = PLANNED, reason
        return target

    def _regate(self, app: App, record: Record, current: str | None) -> None:
        """An in-flight release whose head moved has no gate verdict: ask again,
        at most ``max_regate_per_pr`` times a week."""
        pr = app.pr
        event = in_flight(record)
        if pr is None or event is None:
            return
        if (
            event.get("action") == "gate_requested"
            and event.get("sha") == pr.head_sha
            and not self.report_only
        ):
            # Gate requested but never armed (the arming call failed): retry.
            self._arm(app, record, str(event.get("slot")))
        if event.get("sha") == pr.head_sha or any(
            e.get("action") == "regated" and e.get("sha") == pr.head_sha
            for e in record.events
        ):
            if app.gate in ("failure", "error"):
                record.state = BLOCKED
                record.reason = f"gate-check failed on {pr.head_sha[:7]}"
            return
        if app.gate in ("success", "pending") or current is None:
            return
        if record.regates >= self.config.max_regate_per_pr:
            record.state = BLOCKED
            record.reason = (
                "head moved after the gate was requested; re-gate cap "
                f"({self.config.max_regate_per_pr}) reached this week"
            )
            return
        if self.report_only:
            return
        key = slot_key(slot_at(self.today, current))
        if not has_event(record, "regated", key, pr.head_sha):
            self.writer.comment(app.repo, pr.number, ptc.GATE_CHECK)
            add_event(record, "regated", slot=key, sha=pr.head_sha, at=self.now)
            record.regates += 1

    def close(self, apps: list[App]) -> None:
        pairs = self._prepare(apps)

        def step(app: App, record: Record) -> None:
            if in_flight(record):
                self._disarm(app, record, "not merged by the weekly close")
            for slot in sorted(pending_skips(record)):
                if slot_week(slot) == self.week:
                    add_event(record, "expired", slot=slot, sha=None, at=self.now)
            if consecutive_skip_weeks(record):
                self.notes.append(
                    f"{app.repo}: skipped two weeks running "
                    f"({previous_week(self.week)}, {self.week})"
                )
            next_at = self._settle(app, record, {})
            self._publish(app, record, next_at)
            self._row(app, record, format_slot(next_at) if next_at else "")

        self._each(pairs, step)
        for app in apps:
            self.mark_merged(app)

    # ― summary ―

    def summary(self, phase: str, mode: str, *, verbose: bool = False) -> str:
        title = "Release train — weekly summary" if phase == CLOSE else "Release train"
        lines = [
            f"## {title} ({self.week}, {phase}, {mode})",
            "",
            f"Run at {self.now:%a %d %b %H:%M} IST.",
            "",
        ]
        if not verbose:
            return "\n".join(lines + self._public_summary()) + "\n"
        lines += ["| App | State | Detail |", "|---|---|---|"]
        lines += [
            f"| `{r.repo}` | {r.state} | {_inert(r.detail, 160)} |" for r in self.rows
        ]
        if self.notes:
            lines += ["", "### Notes", ""] + [f"- {_inert(n, 200)}" for n in self.notes]
        if self.errors:
            lines += ["", "### Errors", ""] + [
                f"- {_inert(e, 300)}" for e in self.errors
            ]
        if self.writer.intended:
            verb = "Performed" if self.writer.live else "Would perform"
            lines += ["", f"### {verb} {len(self.writer.intended)} write(s)", ""]
            lines += [f"- {_inert(w, 200)}" for w in self.writer.intended]
        return "\n".join(lines) + "\n"

    def _public_summary(self) -> list[str]:
        """Counts only: this repo is public and the list is not (see module doc)."""
        states: dict[str, int] = {}
        for row in self.rows:
            states[row.state] = states.get(row.state, 0) + 1
        lines = [f"{len(self.rows)} app(s) on the train.", "", "| State | Apps |"]
        lines += ["|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(states.items())]
        if self.writer.kinds:
            verb = "Performed" if self.writer.live else "Would perform"
            kinds = ", ".join(f"{v} {k}" for k, v in sorted(self.writer.kinds.items()))
            lines += ["", f"{verb} {len(self.writer.intended)} write(s): {kinds}."]
        if self.notes:
            lines += [
                "",
                f"{len(self.notes)} note(s); see the bump PR status comments.",
            ]
        if self.errors:
            lines += [
                "",
                f"{len(self.errors)} error(s): {public_error_counts(self.errors)}.",
            ]
            lines += ["Detail is on each app's bump PR status comment and in Pulse."]
        return lines


def public_error_counts(errors: Iterable[str]) -> str:
    """``repo: kind`` errors → ``kind ×n``, dropping the repo."""
    counts: dict[str, int] = {}
    for error in errors:
        kind = error.split(": ", 1)[-1]
        counts[kind] = counts.get(kind, 0) + 1
    return ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items()))


# ── Entry point ──────────────────────────────────────────────────────────


def parse_repos(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        value = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(value, list) or not all(
        isinstance(r, str) and _REPO_RE.match(r) for r in value
    ):
        raise ValueError(f"--repos must be a JSON list of owner/repo, got {raw!r}")
    return list(value)


def roster(config: Config, requested: list[str]) -> list[str]:
    """The listed apps. ``--repos`` narrows the list and never widens it; an
    empty list releases nothing (it never falls back to the whole fleet)."""
    outside = sorted(set(requested) - set(config.schedule))
    if outside:
        raise ValueError(f"--repos not in the schedule list: {outside}")
    return sorted(requested or config.schedule)


def run(
    phase: str,
    config: Config,
    repos: list[str],
    gh: Gh,
    *,
    mode: str,
    now: datetime,
    bot_login: str,
    verbose: bool = False,
) -> Train:
    writer = Writer(gh, live=mode == "live", verbose=verbose)
    train = Train(
        config, gh, writer, now=now, bot_login=bot_login, report_only=mode == "report"
    )
    apps: list[App] = []
    for repo in repos:
        try:
            apps.append(
                load_app(
                    repo,
                    gh,
                    config,
                    bot_login,
                    week=train.week,
                    with_merged=phase in (SYNC, CLOSE),
                )
            )
        except (GhError, ptc.GitHubError, ValueError) as exc:
            train.errors.append(f"{repo}: {public_error(exc)}")
    {SYNC: train.sync, SLOT: train.slot, CLOSE: train.close}[phase](apps)
    return train


def load_config(gh: Gh, local: str) -> Config:
    """The local file when given, else the list on connector-pulse ``main``."""
    if local:
        return parse_config(pathlib.Path(local).read_text(encoding="utf-8"))
    text = gh.file(CONFIG_REPO, CONFIG_PATH)
    if text is None:
        raise ConfigError(f"{CONFIG_REPO}:{CONFIG_PATH} not found on {BASE_BRANCH}")
    return parse_config(text)


def config_error_text(exc: BaseException, *, verbose: bool) -> str:
    """A ConfigError can quote a listed repo, so the public log names none."""
    if verbose:
        return str(exc)
    if isinstance(exc, GhError):
        return f"could not read the schedule list: {public_error(exc)}"
    if isinstance(exc, ConfigError) and "not found on" in str(exc):
        return str(exc)
    return (
        f"the schedule list ({SCHEDULE_SOURCE}) is invalid; connector-pulse's "
        "test of that file names the problem (or re-run locally with --verbose)"
    )


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner = subprocess.run,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Release the app fleet on schedule.")
    parser.add_argument(
        "--config",
        default="",
        help=f"a local list instead of {CONFIG_REPO}:{CONFIG_PATH} (local runs)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print per-app detail; never in CI (this repo's logs are public)",
    )
    parser.add_argument("--phase", default="auto", help="auto|sync|slot|close")
    parser.add_argument("--cron", default="", help="github.event.schedule, if any")
    parser.add_argument("--repos", default="", help="JSON list of owner/repo")
    parser.add_argument("--mode", default="dry-run", help="dry-run|live|report")
    parser.add_argument(
        "--live", action="store_const", const="live", dest="mode", help="= --mode live"
    )
    args = parser.parse_args(argv)

    if args.mode not in MODES:
        print(f"::error::--mode must be one of {MODES}", file=sys.stderr)
        return 2
    try:
        phase = resolve_phase(args.phase, args.cron)
        requested = parse_repos(args.repos)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    gh = Gh(runner)
    try:
        config = load_config(gh, args.config)
    except (ConfigError, GhError, OSError) as exc:
        print(
            f"::error::{config_error_text(exc, verbose=args.verbose)}", file=sys.stderr
        )
        return 2
    slug = os.environ.get("APP_SLUG") or DEFAULT_APP_SLUG
    try:
        repos = roster(config, requested)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    print(f"Release train: phase={phase} mode={args.mode} repos={len(repos)}")
    train = run(
        phase,
        config,
        repos,
        gh,
        mode=args.mode,
        now=now or datetime.now(UTC),
        bot_login=f"{slug}[bot]",
        verbose=args.verbose,
    )
    text = train.summary(phase, args.mode, verbose=args.verbose)
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text)
    if train.errors:
        shown = train.errors if args.verbose else [public_error_counts(train.errors)]
        for error in shown:
            print(f"::error::{_inert(error, 300)}", file=sys.stderr)
    return 1 if train.errors else 0


if __name__ == "__main__":
    sys.exit(main())
