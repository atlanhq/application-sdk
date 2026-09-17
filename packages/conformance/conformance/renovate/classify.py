"""
Pure classification logic for Renovate PRs.

Branch-first, title as fallback. This module used to read self-managed labels
the fleet preset applied, with branch/title parsing only as a fallback for PRs
that predated the label rollout. The labels are gone (FND-2201) and the fallback
is now the only path, because the labels cost far more than they were worth:

GitHub emits one ``pull_request: labeled`` event PER label, the fleet's
tests.yaml subscribes to ``labeled`` (the only way the human ``e2e`` label can
start a run — GitHub has no trigger-level filter for WHICH label), and the unit
job's concurrency group then evicts all but one of the resulting runs. Each
evicted run still publishes a FAILURE on the required ``tests / Tests Gate``
context. That reds Renovate's own automerge fallback on every bot PR
("PR is not ready for merge (branch status is red)" — Renovate aggregates ALL
check runs, not just required ones), and blocks the merge outright whenever an
evicted run is the newest check SUITE, which is how GitHub resolves a required
check. Three labels per PR meant four Tests runs, three of them evicted.

Branch names carry the same information for free: they are set by the preset's
own groupName/branchName, so they are as authoritative as a label Renovate
applies from the same config, and they arrive with the PR instead of as three
extra webhook events.

Auto-merge policy mirrors renovate-config/default.json:
  - lock-maintenance:    automerge=true  (uv.lock-only, in-range)
  - github-actions:      automerge=true  (all update types, incl. major)
  - contract-toolkit:    automerge=true  (majors are `enabled: false` in the
                         preset, so a contract-toolkit major PR never exists)
  - conformance-package: automerge=true  for EVERY update type, majors included
                         (FND-378 — the suite is its own gate, so a major it
                         cannot pass simply never merges; dedicated uv.lock-only
                         PR via update-lockfile, auto-merged even under the
                         soft-mode template's '*' automerge=false rule)
  - python-dep:          automerge=false (edits pyproject.toml constraint → human)

Blocking-reason mirrors renovate-auto-approve-reusable.yml conditions:
  file allowlist: .github/, uv.lock, package-lock.json, requirements.txt, pyproject.toml
  (at any depth — (.*/)?<name> pattern from the workflow's grep -vxE filter)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from conformance.renovate.models import (
    BlockingReason,
    Category,
    ChecksState,
    PRDep,
    RenovatePR,
    UpdateType,
)

# The runtime SDK package — matched in branch slugs (renovate/atlan-application-sdk-3.x)
# and titles ("update dependency atlan-application-sdk to v3.27.0"). The negative
# lookahead keeps atlan-application-sdk-conformance from matching.
_SDK_PACKAGE_RE = re.compile(r"atlan-application-sdk(?!-conformance)")

# Allowed file patterns for the auto-approve gate (mirrors renovate-auto-approve-reusable.yml).
_DEP_FILE_RE = re.compile(
    r"^(\.github/.*"
    r"|(.*/)?uv\.lock"
    r"|(.*/)?package-lock\.json"
    r"|(.*/)?requirements\.txt"
    r"|(.*/)?pyproject\.toml)$"
)


# Age backstop for the "genuinely wedged" signal. An auto-merge-eligible, green
# PR still open this many days after creation is treated as stuck regardless of
# the specific mechanism — the auto-approve → auto-merge → queue pipeline runs on
# a cadence of hours, so a full day open means something is broken. Deliberately
# conservative to avoid flagging PRs the pipeline is still legitimately carrying.
STALE_AFTER_DAYS = 1


# ISO 8601 duration subset accepted for the tripwire window, mirroring
# renovate_uv_lock_bounded.parse_window — that is the only writer, so anything it
# cannot emit is not a window we should be interpreting. Calendar units are
# rejected rather than approximated.
_WINDOW_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


# The refusal reasons ``withhold()`` stamps onto the tripwire line, split by
# whether waiting fixes them. The writer's copy of this vocabulary lives in
# ``.github/scripts/renovate_uv_lock_bounded.py``; the two are pinned equal by a
# test in ``.github/scripts/tests/`` rather than shared by import, because the
# driver runs as a bare ``python3`` script on the fleet runner with no venv and
# so cannot import this package.
#
# Only ``window-empty`` heals on its own: the bound admitted nothing on the pass
# that wrote it, which the next slice of window routinely fixes. The rest are
# standing faults — a broken interpreter, an unsatisfiable floor, a floor that
# was admitted and still failed, a yanked-pin rollback — where waiting changes
# nothing and a human owns the branch.
SELF_HEALING_REFUSALS = frozenset({"window-empty"})

# How long a self-healing refusal may sit before its survival is itself the
# finding. The reaper step in renovate.yaml deletes one on sight and the fleet
# cron is four-hourly, so anything past two passes means the reaper did not run.
# Deliberately not the refusal's own window: that clock answers "would the bound
# admit something now?", which stops being the question once something is
# supposed to have deleted the branch regardless of what the bound would admit.
REAPER_GRACE = timedelta(hours=9)


def _non_dep_files(files: list[str]) -> list[str]:
    return [f for f in files if not _DEP_FILE_RE.match(f)]


def parse_window(window: str) -> Optional[timedelta]:
    """``P3D`` / ``PT12H`` -> timedelta; ``None`` for anything unrecognised."""
    match = _WINDOW_RE.match(window.strip())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes)


def parse_refusal_options(lock_text: str) -> tuple[str, str]:
    """The bounded-lock refusal tripwire's ``(window, reason)``, or ``("", "")``.

    One parser behind two accessors — :func:`lock_refusal_window` and
    :func:`lock_refusal_reason` — rather than two line loops that could disagree
    about what counts as a tripwire.

    ``withhold()`` in ``.github/scripts/renovate_uv_lock_bounded.py`` refuses a
    lock refresh by writing the baseline versions plus a bare ``[options]`` table,
    stamped since FND-909 with why it refused::

        [options]
        exclude-newer-span = "P3D"  # refusal: window-empty

    The reason is empty for a tripwire written before that stamp existed. "No
    reason given" must never read as self-healing, so callers treat an unstamped
    tripwire as the pre-FND-909 shape and fall back to the window clock.

    That table is the refusal's only durable trace — nothing else on the PR
    records it — and it is precisely what ``uv sync --locked`` rejects, which is
    how a required check ends up red by design.

    Distinguishing it from an ``[options]`` table uv wrote itself matters, because
    four repos declare their own ``[tool.uv] exclude-newer`` and legitimately carry
    one on every branch *and* on base. uv always records the absolute
    ``exclude-newer`` key alongside the span (and an
    ``[options.exclude-newer-package]`` subtable when per-package ceilings apply);
    the tripwire writes the span alone. So a lone span is the driver's signature,
    and reading only the head lock is enough — no second fetch of base to prove the
    table was *added*.

    Line-based rather than a ``tomllib`` round-trip for the same reason
    ``strip_options`` is: the caller holds several hundred KB of lockfile per PR
    and needs six lines of it.
    """
    span = ""
    reason = ""
    in_options = False
    for raw in lock_text.splitlines():
        line = raw.strip()
        if line.startswith("[options."):
            # uv's own per-package ceiling subtable. Never written by withhold().
            return "", ""
        if line.startswith("[options]"):
            in_options = True
            continue
        if not in_options:
            continue
        if line.startswith("["):
            in_options = False
            continue
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip() == "exclude-newer":
            # uv wrote this table, not the driver.
            return "", ""
        if key.strip() == "exclude-newer-span":
            head, _, stamp = value.partition("#")
            span = head.strip().strip("\"'")
            stamp = stamp.strip()
            if stamp.startswith("refusal:"):
                reason = stamp[len("refusal:") :].strip()
    return span, reason


def lock_refusal_window(lock_text: str) -> str:
    """The release-age window a bounded-lock refusal tripwire names, or "".

    See :func:`parse_refusal_options` for the shape being read.
    """
    return parse_refusal_options(lock_text)[0]


def lock_refusal_reason(lock_text: str) -> str:
    """Why the driver refused, per the tripwire's stamp, or "" if unstamped.

    A reason outside :data:`SELF_HEALING_REFUSALS` is a standing fault: waiting
    does not clear it and the reaper deliberately leaves it alone.
    """
    return parse_refusal_options(lock_text)[1]


def _is_uv_lock_only(files: list[str]) -> bool:
    """Exactly one changed file, and it is a ``uv.lock`` (at any depth)."""
    return len(files) == 1 and files[0].rsplit("/", 1)[-1] == "uv.lock"


def bounded_lock_refusal_state(
    pr: RenovatePR, category: Category, now: datetime
) -> Optional[BlockingReason]:
    """Which bounded-lock refusal this red PR is, or ``None`` if it is not one.

    Three conditions identify a refusal at all, machine-checkable and none
    heuristic: it is a lock-maintenance PR, the diff is a ``uv.lock`` and nothing
    else, and that lock carries the driver's tripwire (see
    :func:`parse_refusal_options`).

    What separates the two findings is the tripwire's stamp, because they have
    different owners and different clocks:

    ``BOUNDED_LOCK_REFUSAL_STANDING``
        A reason outside :data:`SELF_HEALING_REFUSALS` — a broken interpreter, an
        unsatisfiable floor, a yanked-pin rollback. Waiting fixes none of them and
        the reaper leaves them alone by design, so this is reported the moment it
        is seen. No clock: an age gate here would only delay a human looking at a
        branch that was never going to recover on its own.

    ``BOUNDED_LOCK_REFUSAL_EXPIRED``
        A refusal that should already be gone. Two clocks reach it, and which one
        applies depends on whether the tripwire is stamped:

        *Stamped self-healing* — the reaper deletes these on sight, so the finding
        is not "would the bound admit something now?" but "why is this still
        here?". :data:`REAPER_GRACE` (two fleet passes) is the answer, and the
        refusal's own window stops being relevant once something is supposed to
        have deleted the branch regardless of what the bound would admit.

        *Unstamped* — written before FND-909, so the reason is unknowable and the
        reaper correctly ignores it. Falls back to the original window clock: a
        refusal written at ``T`` was caused by content published inside
        ``(T - W, T]``, so by ``T + W`` every one of those releases is at least
        ``W`` old and the same resolve would now be admitted. "No reason given"
        must never read as self-healing, so this path never claims the shorter
        grace.

    ``head_committed_at`` is the right clock for both and ``created_at`` is not:
    Renovate rewrites a lock branch in place, so a PR opened a week ago may carry
    a refusal written an hour ago.
    """
    if category is not Category.LOCK_MAINTENANCE:
        return None
    if not _is_uv_lock_only(pr.files):
        return None
    window = parse_window(pr.lock_refusal_window)
    if window is None:
        return None

    reason = pr.lock_refusal_reason
    if reason and reason not in SELF_HEALING_REFUSALS:
        return BlockingReason.BOUNDED_LOCK_REFUSAL_STANDING

    head = pr.head_committed_at
    if head is None:
        # No clock for the branch head — the input predates the field. Report the
        # ordinary red-build reason rather than guessing from created_at, which
        # Renovate's in-place branch rewrites make unrelated to the refusal.
        return None
    # Both sides get the same naive->UTC coercion classify() applies to
    # created_at. Normalising only one of them makes a naive clock a TypeError on
    # subtraction rather than a wrong-by-an-offset answer, which is a worse
    # failure for a caller passing its own `now` in a test.
    if head.tzinfo is None:
        head = head.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    grace = REAPER_GRACE if reason in SELF_HEALING_REFUSALS else window
    if now - head >= grace:
        return BlockingReason.BOUNDED_LOCK_REFUSAL_EXPIRED
    return None


def categorize(pr: RenovatePR) -> Category:
    """Derive category from the branch slug, falling back to the title.

    Branch-only since FND-2201 — see the module docstring for why the label path
    was removed. Every lane below is named by the preset's own groupName (or, for
    the SDK lane, by the package name Renovate slugifies into the branch), so
    this reads the same config the labels were generated from, one indirection
    earlier and without the webhook cost.

    The title arm stays because two shapes reach here without a usable branch:
    a PR retargeted or renamed by hand, and the grouped lanes whose slug is the
    groupName rather than the package ("non-critical python dependencies").
    """
    branch = pr.branch.lower()
    title = pr.title.lower()

    if "lock-file-maintenance" in branch or pr.title.startswith(
        "Lock file maintenance"
    ):
        return Category.LOCK_MAINTENANCE
    if "github-actions" in branch:
        return Category.GITHUB_ACTIONS
    if "app-contract-toolkit" in branch or "app-contract-toolkit" in title:
        return Category.CONTRACT_TOOLKIT
    # groupName "conformance package" → branch slug renovate/conformance-package;
    # ungrouped fallback branch carries the full package name.
    if (
        "conformance-package" in branch
        or "application-sdk-conformance" in branch
        or "conformance package" in title
        or "application-sdk-conformance" in title
    ):
        return Category.CONFORMANCE_PACKAGE
    # SDK check must come AFTER conformance: "atlan-application-sdk" is a
    # prefix of "atlan-application-sdk-conformance", so the negative lookahead
    # alone isn't enough for grouped-branch slugs that drop the suffix.
    if _SDK_PACKAGE_RE.search(branch) or _SDK_PACKAGE_RE.search(title):
        return Category.SDK_PACKAGE

    return Category.PYTHON_DEP


def update_type_from_body(body: str) -> UpdateType:
    """
    Parse the semver update type from the Renovate PR body table.

    Renovate embeds a 'from -> to' version table like:
        | dep | 1.2.3 | -> | 2.0.0 |
    We extract the numeric majors and classify accordingly.
    """
    # Match patterns like "1.2.3 -> 2.0.0" or "v1.2 -> v2.0"
    pattern = re.compile(r"v?(\d+)\.\d+[^\s]*\s*->\s*v?(\d+)\.\d+", re.MULTILINE)
    for m in pattern.finditer(body):
        from_major, to_major = int(m.group(1)), int(m.group(2))
        if to_major > from_major:
            return UpdateType.MAJOR
        return UpdateType.MINOR  # minor/patch — can't distinguish without 3-part semver
    return UpdateType.UNKNOWN


def derive_update_type(pr: RenovatePR) -> UpdateType:
    """Body-table only, since FND-2201 removed the update:<type> labels.

    REPORTING ONLY. Nothing branches on this — :func:`auto_merge_expected`
    deliberately does not take it (see that function's docstring), so a coarser
    answer here cannot mis-route a PR, only under-describe one on the dashboard.

    That is what makes dropping the labels affordable: the body table renders
    the version move but not enough of it to separate a minor from a patch
    without assuming three-part semver, so PATCH, DIGEST and PIN are no longer
    distinguishable and surface as MINOR or UNKNOWN. Do not reintroduce a label
    to recover them — the fidelity is worth a fraction of one wasted CI run,
    let alone three per PR across the fleet. If a consumer ever genuinely needs
    the exact update type, read it from the deps this PR delivers
    (:func:`extract_deps` gives from/to versions) rather than from a label.
    """
    return update_type_from_body(pr.body)


# Renovate PR body version-table row: first cell is the package (usually a
# markdown link, sometimes bare text), and the Change cell renders the version
# move as `old` -> `new`. Cell-internal parentheses (changelog links, "(source)")
# and extra columns vary by manager, so match loosely: name from the first cell,
# then the first backtick-quoted arrow pair anywhere on the same row.
_BODY_TABLE_NAME_RE = re.compile(
    r"^\|\s*(?:\[(?P<linked>[^\]]+)\]|(?P<bare>[^|\[\]]+?))\s*(?:\(|\|)"
)
_BODY_TABLE_CHANGE_RE = re.compile(r"`(?P<from>[^`]+)`\s*(?:->|→)\s*`(?P<to>[^`]+)`")

# Title fallback: "chore(deps): update dependency atlan-application-sdk to v3.27.0",
# "chore(deps): update app-contract-toolkit to v0.18.1",
# "chore(deps): update anthropics/claude-code-action action to v1.0.190". Grouped
# titles without a trailing version ("update non-critical python dependencies")
# deliberately don't match — there is no version to report.
_TITLE_DEP_RE = re.compile(
    r"update (?:dependency )?(?P<name>[A-Za-z0-9._/@-]+)(?: action)? to v?(?P<to>\d[\w.+-]*)",
    re.IGNORECASE,
)


def extract_deps(pr: RenovatePR) -> tuple[PRDep, ...]:
    """Which packages this PR delivers, as (name, from, to) triples.

    Body version-table first (authoritative — grouped PRs list every package
    there and their titles carry no version), title parse as fallback for
    bodies that are empty or unrecognisable. Lock-file-maintenance PRs have no
    table and yield () — they deliver in-range bumps invisibly, which is
    exactly why downstream freshness tooling needs the explicit deps on every
    other category.
    """
    deps: list[PRDep] = []
    seen: set[tuple[str, str]] = set()
    for line in pr.body.splitlines():
        name_m = _BODY_TABLE_NAME_RE.match(line.strip())
        if not name_m:
            continue
        name = (name_m.group("linked") or name_m.group("bare") or "").strip()
        # Skip the header row and separator rows.
        if (
            not name
            or name.lower() in {"package", "dependency", "update"}
            or set(name) <= {"-", ":"}
        ):
            continue
        change_m = _BODY_TABLE_CHANGE_RE.search(line)
        if not change_m:
            continue
        key = (name, change_m.group("to").strip())
        if key in seen:
            continue
        seen.add(key)
        deps.append(
            PRDep(
                name=name,
                from_version=change_m.group("from").strip(),
                to_version=change_m.group("to").strip(),
            )
        )
    if deps:
        return tuple(deps)

    title_m = _TITLE_DEP_RE.search(pr.title)
    if title_m:
        return (
            PRDep(
                name=title_m.group("name"),
                from_version="",
                to_version=title_m.group("to"),
            ),
        )
    return ()


def auto_merge_expected(
    category: Category,
    repo_automerge_mode: str = "unknown",
) -> bool:
    """
    Mirror renovate-config/default.json auto-merge policy.

    ``repo_automerge_mode`` is the REPO's own posture (see
    discover_org_consumers.automerge_mode): a "soft" repo appends a blanket
    ``automerge: false`` rule after the preset's, so no lane auto-merges there
    however this function reads the shared policy. "auto"/"unknown" defer to the
    lane rules below. This gates first because a soft repo's green unarmed PR is
    the design — reporting it as stuck buried the one real instance among 27 on
    2026-09-17.

    lock-maintenance → always auto (uv.lock refresh, in-range)
    github-actions   → always auto (incl. major; validated by CI gate)
    contract-toolkit → always auto (see below)
    conformance-pkg  → always auto (see below)
    sdk-package      → always human (runtime SDK bumps are a deliberate merge)
    python-dep       → always human (edits pyproject.toml constraint → out-of-range)

    NO UPDATE TYPE. This used to take one and gate both toolkit and conformance
    on ``update_type not in (MAJOR, UNKNOWN)``. Both arms were wrong against the
    preset they claim to mirror, in opposite directions:

    * contract-toolkit majors are ``enabled: false`` in the preset, so a
      contract-toolkit major PR cannot exist. The arm was unreachable.
    * conformance majors ARE auto-merged, explicitly and by design (FND-378):
      the lane ships a checker whose worst case is a red check on the very PR
      that introduced it, so the suite is its own gate. Treating them as
      human-review reported every conformance major as AWAITING_HUMAN_REVIEW —
      the one classification that suppresses the stuck-PR signal — for a PR the
      fleet was in fact auto-merging.

    The ``UNKNOWN`` half was worse than either: it made the verdict depend on
    whether a label happened to be present, so a PR whose labels had not landed
    yet was silently reclassified as needing a human. Removing the parameter is
    what lets the update:<type> labels go (FND-2201); it is also simply correct.
    """
    if repo_automerge_mode == "soft":
        return False
    if category == Category.LOCK_MAINTENANCE:
        return True
    if category == Category.GITHUB_ACTIONS:
        return True
    if category == Category.CONTRACT_TOOLKIT:
        return True
    if category == Category.CONFORMANCE_PACKAGE:
        return True
    # sdk-package, python-dep, unknown
    return False


def checks_state(pr: RenovatePR) -> ChecksState:
    """Map the GitHub statusCheckRollup to a simplified state."""
    # The 'checks_state' field on RenovatePR is already parsed in scan.py from
    # statusCheckRollup; this helper is for tests / callers that have raw values.
    return pr.checks_state


def blocking_reason(
    pr: RenovatePR,
    category: Category,
    age_days: int = 0,
    now: Optional[datetime] = None,
) -> BlockingReason:
    """
    Why has this open PR not merged?

    Mirrors the conditions in renovate-auto-approve-reusable.yml, then adds two
    signals for the silent-stuck case a green/approved/mergeable PR can fall into
    when GitHub-native auto-merge is never armed (see PR #2828 postmortem), plus
    one sub-case of a red build that no human owns (FND-782).

    ``age_days`` and ``now`` are threaded in explicitly rather than read off ``pr``
    because classify() computes them after the model is first constructed.
    """
    now = now or datetime.now(timezone.utc)
    if not auto_merge_expected(category, pr.repo_automerge_mode):
        return BlockingReason.AWAITING_HUMAN_REVIEW

    # For auto-merge-eligible PRs, check gate conditions in priority order.
    if pr.mergeable == "CONFLICTING":
        return BlockingReason.MERGE_CONFLICT

    if _non_dep_files(pr.files):
        return BlockingReason.NON_DEP_FILES

    if pr.checks_state == ChecksState.FAILING:
        # Red is ordinarily a human's problem and age adds nothing to it. One
        # shape is different: a bounded-lock refusal is red by design, so it is
        # either frozen past when the reaper should have cleared it, or a standing
        # fault waiting on a human. Separate both out so the dashboard can say
        # which, and so an alarm can fire on the first without the second.
        refusal = bounded_lock_refusal_state(pr, category, now)
        if refusal is not None:
            return refusal
        return BlockingReason.CHECKS_FAILING
    if pr.checks_state == ChecksState.PENDING:
        return BlockingReason.CHECKS_PENDING

    # Eligible, non-conflicting, dep-only. Both auto-merge signals below describe
    # a PR whose every gate is *green* yet nothing is driving it to merge, so they
    # only fire on GREEN. FAILING/PENDING are already handled above; UNKNOWN
    # (checks rollup couldn't be determined) must not masquerade as green-but-
    # parked — it falls through to AWAITING_APPROVAL as it did before these
    # signals existed. Is anything actually driving a green PR to merge?
    if pr.checks_state == ChecksState.GREEN:
        # Precise signal: every gate is green, yet auto-merge was never armed.
        # Nothing will ever merge it — the dangerous "looks healthy, parked
        # forever" case. Detected immediately (no age threshold) because there is
        # nothing left to wait for: Renovate arms at PR CREATION, long before any
        # check could have gone green, so green-and-unarmed is already anomalous.
        #
        # This used to also require review_decision == "APPROVED", which made it
        # unreachable across most of the fleet: GitHub returns an EMPTY
        # reviewDecision when the repo's ruleset requires 0 approving reviews,
        # even with an atlan-ci approval posted. 52 of 60 open lane PRs sampled
        # on 2026-09-17 read empty, so the real instances all fell through to the
        # age backstop and reported as merely stale. The approval was never part
        # of the claim anyway — arming does not wait on it.
        if not pr.auto_merge_enabled:
            return BlockingReason.AUTOMERGE_NOT_ARMED

        # Age backstop: green + eligible but still open past the staleness
        # threshold. Not gated on approval or armed-state so it also catches a
        # down approval workflow and wedged merge queues — any stuck mode,
        # including ones not modelled above.
        if age_days >= STALE_AFTER_DAYS:
            return BlockingReason.AUTOMERGE_STALE

    # Recently eligible (or checks state not yet determinable); expected to merge
    # imminently (approval pending or freshly armed and awaiting the queue).
    return BlockingReason.AWAITING_APPROVAL


def classify(pr: RenovatePR) -> RenovatePR:
    """
    Return a new RenovatePR with category, update_type, auto_merge_expected,
    blocking_reason, and age_days populated.
    """
    cat = categorize(pr)
    # Reporting only — deliberately not an input to either verdict below.
    ut = derive_update_type(pr)
    ame = auto_merge_expected(cat, pr.repo_automerge_mode)

    now = datetime.now(timezone.utc)
    # pr.created_at may be tz-aware or naive; normalise.
    created = pr.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = max(0, (now - created).days)

    # age feeds the staleness backstop, so it must be computed before classifying.
    br = blocking_reason(pr, cat, age, now)

    # dataclass is frozen=True; use replace pattern.
    import dataclasses

    return dataclasses.replace(
        pr,
        category=cat,
        update_type=ut,
        auto_merge_expected=ame,
        blocking_reason=br,
        age_days=age,
        deps=extract_deps(pr),
    )
