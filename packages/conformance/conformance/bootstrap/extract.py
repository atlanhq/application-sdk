"""Shared "read a rendered param back off an on-disk managed file" extractors.

Leaf module: no dependency on ``conformance.suite`` or ``conformance.bootstrap.command``.
Both the C002 drift checker (``conformance.suite.checks.bootstrap_drift``) and the
``bootstrap`` command's re-run autodetection (``conformance.bootstrap.command``)
import from here at module level, so a template format change can't leave one
caller silently out of sync with the other — and neither layer has to reach
into the other's module to share this logic (which previously produced a
``bootstrap.command -> suite.checks.bootstrap_drift -> bootstrap.render``
import cycle, dodged only by making the ``command.py`` side's imports
function-local).
"""

from __future__ import annotations

import json
import re

# conformance.yaml's exit-zero mode is rendered as a GitHub Actions expression
# (`exit-zero: ${{ ... || << exit_zero >> }}`), not a plain `key: "value"` pair
# — the boolean is the last token before the closing `}}`.
EXIT_ZERO_RE = re.compile(r"exit-zero:.*\|\|\s*(true|false)\s*\}\}")

# checks.yml's optional system-deps step is rendered as an apt-get command
# inside a `run: |` block, not a `key: value` pair, so its packages are read
# back off the install line itself. Deliberately tolerant of how the step was
# hand-written before this flag existed (any `apt-get install` line in the
# file, with or without `sudo`, with flags in any order) — the repos this
# needs to detect are exactly the ones carrying a pre-existing hand-added
# step, and failing to detect one means bootstrap deletes it.
# The argument list runs to end-of-line, continuing across any `\`-escaped
# newlines so a multi-line `apt-get install -y \` step is read whole.
_APT_INSTALL_RE = re.compile(
    r"apt-get\s+install\b(?P<args>(?:[^\n\\]|\\[ \t]*\n)*)",
)
# Debian package names allow lowercase letters, digits, '+', '-', '.'; the
# apt-get argument list also legitimately carries a version pin ('pkg=1.2-3')
# or an explicit release ('pkg/bookworm'), and uppercase appears in a few real
# archive names. This doubles as the validator for the ``--system-deps``
# flag (see ``bootstrap.args.normalize_system_deps``): the value is interpolated
# into a `run:` block in a generated workflow, so anything outside this set —
# above all shell metacharacters and `$` expansions — is rejected on input and
# dropped on extraction rather than escaped.
APT_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/=:-]*$")

# Ends the package list: whatever follows belongs to another command.
_SHELL_OPERATOR_RE = re.compile(r"[;&|]")

# A commented-out install line describes packages the repo decided NOT to
# install, so it must not be extracted: bootstrap would render them into the
# managed step, and the re-rendered file would then never match the on-disk one
# (the comment stays, the step is added) — permanent C002 drift no re-run can
# clear. Dropped before matching rather than inside the loop so a `\`-continued
# comment block can't leak its later lines in either.
_COMMENT_LINE_RE = re.compile(r"^[ \t]*#.*$", re.MULTILINE)


# Matches a pinned SHA (40 lowercase hex chars) and its optional trailing version
# comment. Example: "@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3" →
# "@<pinned>".
_ACTION_PIN_RE = re.compile(r"@[0-9a-f]{40}(?:[ \t]+#[^\n]*)?")

# tests.yaml's per-repo customised values, read back off a scaffolded file.
# Anchored like _SERVICES_SCRIPT_RE below so a *commented-out* line (the shape
# a renamed app most often leaves behind) can't satisfy the read-back — the
# --resync identity guard in particular must skip rather than re-render from a
# value the file no longer declares.
_APP_NAME_RE = re.compile(r'^\s+app-name:\s+"([^"]+)"\s*$', re.MULTILINE)
_APP_IMAGE_NAME_RE = re.compile(r'^\s+app-image-name:\s+"([^"]+)"\s*$', re.MULTILINE)
_ENABLE_E2E_RE = re.compile(r"enable-e2e:\s+(true|false)")
# Matches an *uncommented* services-script line (quoted value) in the with: block.
_SERVICES_SCRIPT_RE = re.compile(r'^\s+services-script:\s+"([^"]+)"$', re.MULTILINE)
# tests-reusable.yaml's `unit-coverage-fail-under` input, quoted or bare. Anchored
# and uncommented-only for the same reason as the lines above.
_UNIT_COVERAGE_FAIL_UNDER_RE = re.compile(
    r'^\s+unit-coverage-fail-under:\s+"?(\d+)"?\s*$', re.MULTILINE
)

# The floor `tests-reusable.yaml` applies when a caller says nothing — its
# ``unit-coverage-fail-under`` input default. An app may raise its own floor
# above this and keep it (see ``extract_tests_yaml_params``); it may not drop
# below it, because that would use the app's own workflow to opt out of a bar
# the SDK sets for the whole fleet.
#
# Duplicated here rather than read from the workflow because this package ships
# standalone into consumer repos, where application-sdk's workflow files are not
# on disk. ``test_bootstrap`` pins the constant against the real input default in
# the monorepo, so the two cannot drift apart unnoticed.
#
# Note the division of labour: whether the resulting floor is high enough to
# ever fail a run is T014 (CoverageGateDisabled)'s question, not C002's. C002
# only decides whether a per-app value is a preserved choice or drift.
SDK_UNIT_COVERAGE_FLOOR = 0


def strip_action_pins(text: str) -> str:
    """Return *text* with every pinned action SHA normalised to ``@<pinned>``.

    Single source of truth for "compare two renders of a managed file while
    ignoring which SHA an action is pinned at". The C002 checker uses it so
    an automated pin bump doesn't read as drift; ``bootstrap``'s
    ``--resync`` uses it to decide whether a re-render would
    change anything C002 cares about, so the flag rewrites exactly the files
    C002 flags and no others.
    """
    return _ACTION_PIN_RE.sub("@<pinned>", text)


def extract_tests_yaml_params(text: str) -> dict[str, str]:
    """Extract the per-repo customised values from a scaffolded tests.yaml.

    Returns only the keys that were found; callers should pass these as kwargs
    to ``render("tests.yaml", ...)`` so defaults apply for any that are absent.

    Single source of truth for the tests.yaml scaffold's parameters — the C002
    checker extracts them to decide what "structural drift" means for this
    file, and ``bootstrap --resync`` extracts them to re-render it.
    Sharing one implementation is what makes the flag's write byte-identical
    to the canonical the checker compares against; two copies could drift into
    a resync that leaves the finding standing.

    ``unit_coverage_fail_under`` is the one value that is only *conditionally*
    preserved: it is kept when it is at or above ``SDK_UNIT_COVERAGE_FLOOR``
    (an app raising its own coverage bar — a choice C002 must not flag) and
    dropped when it is below (an app using its workflow to undercut the
    fleet-wide floor — which stays drift, and which ``--resync`` then fixes by
    removing the line so the app inherits the SDK floor again). A value equal
    to the floor is kept rather than flagged: it weakens nothing, and deleting
    a redundant-but-honest declaration is churn, not remediation.
    """
    params: dict[str, str] = {}
    m = _APP_NAME_RE.search(text)
    if m:
        params["app_name"] = m.group(1)
    m = _APP_IMAGE_NAME_RE.search(text)
    if m:
        params["app_image_name"] = m.group(1)
    m = _ENABLE_E2E_RE.search(text)
    if m:
        params["enable_e2e"] = m.group(1)
    m = _SERVICES_SCRIPT_RE.search(text)
    if m:
        params["services_script"] = m.group(1).strip()
    declared = extract_declared_unit_coverage_fail_under(text)
    if declared and int(declared) >= SDK_UNIT_COVERAGE_FLOOR:
        params["unit_coverage_fail_under"] = declared
    return params


def rejected_unit_coverage_fail_under(text: str) -> str:
    """Return the coverage floor *text* declares but this module refuses to
    preserve — i.e. one below ``SDK_UNIT_COVERAGE_FLOOR`` — else ``""``.

    The single reader of the "is this value preservable?" comparison, so the
    C002 checker's explanation of the resulting finding and
    ``extract_tests_yaml_params``' decision to drop the value cannot disagree
    about which values are which.
    """
    declared = extract_declared_unit_coverage_fail_under(text)
    if declared and int(declared) < SDK_UNIT_COVERAGE_FLOOR:
        return declared
    return ""


def extract_declared_unit_coverage_fail_under(text: str) -> str:
    """Return the unit-coverage floor *text* (a tests.yaml) declares, or ``""``.

    Reports the value as written, *without* the at-or-above-the-SDK-floor filter
    ``extract_tests_yaml_params`` applies — so a caller can tell "this file
    declares nothing" apart from "this file declares a floor we refused to
    preserve". The C002 checker uses that distinction to explain the resulting
    finding in terms of the coverage line, instead of leaving an app owner to
    guess which of their edits counted as structural drift and then watching
    ``--resync`` delete it.
    """
    m = _UNIT_COVERAGE_FAIL_UNDER_RE.search(text)
    return m.group(1) if m else ""


def extract_use_ghcr_base(text: str) -> str:
    """Return ``"true"`` when *text* (a ``build-and-publish.yaml``) opts into the
    GHCR base redirect, else ``""``.

    The opt-in is a per-repo choice on an *always-overwrite* managed shim, so it
    needs both halves of the round-trip or it cannot survive: ``bootstrap``'s
    autodetection reads it here so a bare re-run re-renders the line instead of
    deleting it, and the C002 checker reads it here so a repo that opted in is
    not reported as drifted. The default stays ``false`` in the SDK's reusable
    workflow until the whole fleet has soaked, which is exactly why apps have to
    be able to self-select ahead of that flip.

    Anything other than a literal ``true`` returns ``""`` ("say nothing, take the
    SDK default"), including ``false``: rendering an explicit ``use_ghcr_base:
    false`` would be a second spelling of the default and would read as drift on
    every repo that spells it the other way.
    """
    return "true" if extract_field(text, "use_ghcr_base") == "true" else ""


def extract_field(text: str, field: str) -> str:
    """Return the value of ``field: <value>`` in *text*, or ``""`` if absent.

    *value* may be bare or quoted (``field: value`` or ``field: "value"``);
    quotes are stripped. Matches the first ``field:`` line, at any
    indentation level. Single source of truth for "read a rendered param
    back off an on-disk managed file" — both the C002 drift-comparison
    extractors and ``bootstrap``'s re-run autodetection call this, so a
    template format change can't leave one caller silently out of sync with
    the other.
    """
    for line in text.splitlines():
        m = re.match(rf"^\s*{re.escape(field)}:\s*(\S+)", line)
        if m:
            return m.group(1).strip("\"'")
    return ""


def extract_apt_packages(text: str) -> str:
    """Return the apt packages installed by *text*'s ``apt-get install`` step.

    *text* is a rendered (or hand-written) ``checks.yml``. Returns a single
    space-separated package list, or ``""`` when the file installs nothing.
    Anything that isn't a plausible package name per ``APT_PACKAGE_RE`` is
    dropped — flags (``-y``, ``--no-install-recommends``, ...), line
    continuations, and any shell construct a hand-written step may carry
    (``$VAR``, ``&&``, a pipe). Dropping rather than raising matters: this runs
    during ``bootstrap``'s autodetection over whatever a consumer repo happens
    to have on disk, and aborting a whole re-sync over one odd token would be
    worse than rendering the packages it could read.
    Single source of truth for reading this step back off disk —
    both ``bootstrap``'s re-run autodetection (which must preserve an existing
    step across an always-overwrite re-sync) and the C002 drift checker (which
    must not report a preserved step as drift) call it, so the two cannot
    diverge on what counts as "this repo installs these packages".

    Package order is preserved as written rather than sorted: the value round-
    trips through ``normalize_system_deps`` into the same rendered line, and
    re-ordering it would make every already-bootstrapped repo report C002
    drift once.

    Every *uncommented* ``apt-get install`` occurrence contributes
    (deduplicated, first occurrence wins), so a repo that hand-wrote two
    separate install steps has both preserved — bootstrap then consolidates
    them into the one managed step, and C002 flags the pre-consolidation file
    as drift until it does. Commented-out install lines are excluded: they name
    packages the repo chose not to install, and extracting them would render a
    step the on-disk file doesn't have, leaving C002 drift that no re-run clears.
    """
    # Dropping a comment line leaves its trailing newline behind. If the comment
    # sat *between* two `\`-continuation lines, that stray blank line is a bare
    # newline `_APT_INSTALL_RE`'s continuation arm cannot cross, so every package
    # after it would be lost. Collapse runs of blank lines back to one so the
    # continuation stays whole; the canonical render never uses `\`-continuation,
    # so this only rescues a near-invalid hand-written form and leaves the
    # C002 round-trip untouched.
    cleaned = re.sub(r"\n{2,}", "\n", _COMMENT_LINE_RE.sub("", text))
    packages: list[str] = []
    for m in _APT_INSTALL_RE.finditer(cleaned):
        for token in sanitize_package_list(m.group("args")):
            if token not in packages:
                packages.append(token)
    return " ".join(packages)


def sanitize_package_list(text: str) -> list[str]:
    """Return the plausible apt package names in *text*, in order.

    Truncates at the first shell operator — everything after ``&&``, ``||``,
    ``|`` or ``;`` belongs to another command, and no package name may contain
    those characters, so the split can never cut a real one short — and at the
    first ``#`` — an inline trailing comment (``libkrb5-dev  # build deps``)
    describes the line, not more packages, and its words are otherwise valid
    ``APT_PACKAGE_RE`` tokens that would leak into the list. ``APT_PACKAGE_RE``
    forbids ``#`` too, so this can't cut a real name short either. Then keeps
    only tokens matching ``APT_PACKAGE_RE`` (dropping flags and anything else).

    Shared by the ``checks.yml`` extraction above and the
    ``.github/ci-system-deps.txt`` reader in ``bootstrap.autodetect``, so a
    hand-edited value cannot reach a generated workflow's ``run:`` block by
    whichever of the two paths happens to read it.
    """
    args = _SHELL_OPERATOR_RE.split(text)[0].split("#", 1)[0]
    return [token for token in args.split() if APT_PACKAGE_RE.match(token)]


def extract_renovate_automerge(text: str) -> str:
    """Return renovate.json's automerge mode (``"true"``/``"false"``) from *text*.

    renovate.json's soft-mode block (rendered only when ``automerge ==
    "false"``) is a Jinja ``<% if %>`` block, not a substitutable value —
    detected structurally via the ``lockFileMaintenance`` key that block's
    canonical content always adds, not by matching the human-readable
    ``description`` prose inside it, so wording edits to that prose can't
    silently break mode detection.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return "true"
    return (
        "false" if isinstance(data, dict) and "lockFileMaintenance" in data else "true"
    )


def resolve_renovate_fallback_exit_zero(renovate_text: str) -> str:
    """Return the raw ``exit-zero`` fallback signal (``"true"``/``"false"``)
    implied by *renovate_text* (an already-read ``renovate.json``'s contents).

    Single source of truth for "derive exit-zero from renovate.json's
    automerge signal when the primary ``conformance.yaml`` exit-zero line
    can't be read" — both ``bootstrap.autodetect``'s ``--enforce``
    autodetection and the C002 checker's exit-zero drift extraction call
    this (each converting the result to its own required polarity), so the
    two can't silently diverge on how the fallback is derived. Callers own
    the "renovate.json is absent/unreadable" case themselves, since each has
    a different sentinel for "no signal at all" (``""`` vs ``"false"``).
    """
    automerge = extract_renovate_automerge(renovate_text)
    return "true" if automerge == "false" else "false"
