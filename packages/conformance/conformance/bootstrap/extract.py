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
