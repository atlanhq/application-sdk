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

# tests.yaml's `app-name`, `app-image-name` and `enable-e2e` used to be read by
# regexes anchored on one value spelling (`: "([^"]+)"` / a bare `true|false`).
# They are inputs of the reusable job, so they are now read like
# `force-external-runtime`: `extract_field` scoped to that job's `with:` block.
# An anchored regex only ever recognised the spelling the template emits, so a
# repo that hand-wrote a bare or single-quoted value read as *absent* and could
# never be re-synced — the same "a hand-written spelling defeats the read-back"
# class as the services-script line below, which two real repos hit.
# The *commented-out* line that anchoring was there to exclude is excluded more
# strongly by the new path: `reusable_job_with_block` is built from
# `structural_lines`, which blanks comment lines outright.

# `services-script` is read the same way, and for the same reason. Bare matters:
# the two repos that actually run a services script (atlan-mongodbatlas-app,
# atlan-tableau-app) hand-wrote it unquoted, pre-dating this template, and a
# quoted-only read-back deleted their active line on every --resync. The regex
# that replaced accepted bare and double-quoted values only, and its bare arm
# excluded just `"` and `#` — so a single-quoted value matched *with the quotes
# attached* and was re-rendered into the path. The re-render normalises to the
# quoted form the template has always emitted: one-time C002 drift for those two
# files, against losing or corrupting the value.

# tests-reusable.yaml's `unit-coverage-fail-under` input, bare or either quote.
# Anchored and uncommented-only for the same reason as the lines above. Each quote
# pair is matched as a unit (`"(\d+)"`, `'(\d+)'` or `\d+`, never a lone
# leading/trailing quote) so a half-quoted line — which the YAML parser would
# reject anyway — can't read back as a valid declaration. It keeps a regex rather
# than moving to `extract_field` because the `\d+` is load-bearing: the value is
# passed to `int()` below, and a non-numeric one must read as absent (and so reach
# the guard) rather than raise.
_UNIT_COVERAGE_FAIL_UNDER_RE = re.compile(
    r"^\s+unit-coverage-fail-under:\s+(?:\"(\d+)\"|'(\d+)'|(\d+))\s*$", re.MULTILINE
)

# An *explicit* ``secrets:`` mapping — the caller shape that composes
# ``E2E_SOURCE_ENV_JSON`` out of this repo's per-connector source-credential
# secret NAMES. ``secrets: inherit`` can neither compose nor rename, so
# downgrading an explicit mapping to it leaves the reusable's integration and
# e2e legs with no source credentials at all, failing later with what reads as
# a source-system error (FND-604). Matched in mapping form only: nothing but an
# optional trailing comment may follow the colon. The key may be bare or
# single/double-quoted — a quoted ``"secrets":`` is the same declaration.
_SECRETS_MAPPING_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?:secrets|\"secrets\"|'secrets'):[ \t]*(?:#[^\n]*)?$",
    re.MULTILINE,
)

# A ``secrets:`` line carrying its value *inline* rather than as an indented
# block: a flow mapping (``secrets: {A: ${{ secrets.A }}}``), an alias
# (``secrets: *shared``), or an anchored value. Matched separately from
# ``_SECRETS_MAPPING_RE`` because these forms are preserved by neither half of
# the FND-604 fix: ``extract_secrets_block`` splices block form only, so it
# returns ``""`` and the re-render emits ``secrets: inherit`` — yet ``secrets``
# parses as a key on *both* sides of ``unpreserved_declarations``, so the
# generalised guard reads the shared key name as proof of preservation and does
# not refuse. That combination reproduces the exact silent downgrade this module
# exists to stop, through a different spelling of the same declaration, so the
# form is detected and routed to the refusal instead.
_SECRETS_INLINE_RE = re.compile(
    r"^[ \t]+(?:secrets|\"secrets\"|'secrets'):[ \t]+(?P<value>[^#\s][^\n]*?)[ \t]*$",
    re.MULTILINE,
)

# The ``uses:`` line that identifies the job calling the SDK's reusable test
# workflow. ``force-external-runtime`` is an *input of that job*, so the read for
# it is scoped to that job's ``with:`` block: an unscoped search returns the
# first match anywhere in the file, including one under an unrelated job a repo
# added, and hoists it into the rendered ``jobs.tests.with`` — forcing the
# external runtime on a repo that never asked for it.
#
# Keyed on the reusable's own path rather than on the job being named ``tests``:
# the job name is the branch-protection context string and a repo is free to
# change it, but a job that takes this input is by definition the one calling
# this workflow.
_TESTS_REUSABLE_USES_RE = re.compile(
    r"^(?P<indent>[ \t]+)uses:[ \t]*[^\s#]*tests-reusable\.ya?ml[^\s#]*[ \t]*(?:#[^\n]*)?$",
    re.MULTILINE,
)

# A YAML mapping key as written, with an optional leading sequence dash so a
# list-of-mappings entry counts as a declaration too. The lookahead requires a
# space or end-of-line after the colon, so a bare ``http://x`` inside a value
# cannot read as a key. The key may be bare or single/double-quoted — a quoted
# ``"secrets":`` is the same declaration as ``secrets:``, and a read that only
# knew the bare spelling would be blind to it (the quoted-key class below). The
# quote character is captured so ``_yaml_key`` can strip it; a half-quoted key
# (one the YAML parser would reject) never matches.
_KEY_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:-[ \t]+)?"
    r"(?P<key>(?:(?P<quote>[\"'])[A-Za-z0-9_.-]+(?P=quote)|[A-Za-z0-9_.-]+)):(?=[ \t]|$)"
)


def _yaml_key(raw: str) -> str:
    """Return *raw* (a ``_KEY_LINE_RE`` key capture) with any quotes stripped.

    The one normalisation every key comparison goes through, so the guard, the
    scoped readers, and ``declared_keys`` cannot disagree about whether a quoted
    ``"secrets":`` is the same declaration as a bare ``secrets:``.
    """
    return raw.strip("\"'")


# ``key: |`` / ``key: >-`` and friends: every more-indented line that follows is
# opaque scalar content, not structure, so it must not be mined for keys. The
# header may carry an optional YAML tag (``!!str``, ``!custom``, ``!<tag:x>``)
# before the indicator, and the chomping (``-``/``+``) and indent (``\d``)
# indicators may appear in either order — ``|2-`` is as valid as ``|-2``. A
# shape this misses leaves the scalar body visible as structure, so a ``uses:``
# or ``with:`` quoted inside it is mined as a real declaration (the
# scalar-header class below).
_BLOCK_SCALAR_RE = re.compile(
    r":[ \t]*(?:(?:!!?[^\s|>]+|!<[^>\s]*>)[ \t]+)?[|>](?:[+-]?\d*|\d+[+-]?)[ \t]*(?:#[^\n]*)?$"
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

    ``force_external_runtime`` and ``secrets_block`` were added by FND-604:
    both are live inputs of ``tests-reusable.yaml`` that apps hand-write, and
    until they were read back here every ``--resync`` deleted them — the first
    making the app's boot raise ``DaprNotDetectedError`` (FND-65), the second
    silently downgrading an explicit ``secrets:`` mapping to ``secrets:
    inherit``, which cannot compose the ``E2E_SOURCE_ENV_JSON`` the integration
    and e2e legs read.

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
    # Inputs of the reusable job, so read from its own `with:` block through the
    # quote-tolerant `extract_field` — see the note above the regexes this
    # replaced, and `extract_force_external_runtime` for the same pairing.
    with_block = reusable_job_with_block(text)
    if with_block:
        app_name = extract_field(with_block, "app-name")
        if app_name:
            params["app_name"] = app_name
        app_image_name = extract_field(with_block, "app-image-name")
        if app_image_name:
            params["app_image_name"] = app_image_name
        # Only the two booleans are meaningful; anything else is unreadable and
        # is left to the round-trip guard to refuse rather than guessed at.
        enable_e2e = extract_field(with_block, "enable-e2e")
        if enable_e2e in ("true", "false"):
            params["enable_e2e"] = enable_e2e
        # Same reader for the same reason. The regex this replaced had a bare arm
        # that excluded only `"` and `#`, so a *single*-quoted value matched it
        # with the quotes still attached and was re-rendered as part of the path
        # — a corrupted value the round-trip guard cannot see, because the key is
        # still present on both sides.
        services_script = extract_field(with_block, "services-script")
        if services_script:
            params["services_script"] = services_script
    declared = extract_declared_unit_coverage_fail_under(text)
    if declared and int(declared) >= SDK_UNIT_COVERAGE_FLOOR:
        params["unit_coverage_fail_under"] = declared
    force_external = extract_force_external_runtime(text)
    if force_external:
        params["force_external_runtime"] = force_external
    secrets_block = extract_secrets_block(text)
    if secrets_block:
        params["secrets_block"] = secrets_block
    return params


def structural_lines(text: str) -> list[str]:
    """Return *text*'s lines with everything that is not YAML structure blanked.

    A comment line, and every line belonging to a block scalar's body
    (``key: |``, ``key: >-``), becomes ``""``. Line *count* is preserved, so an
    index into the result is the same index into ``text.splitlines()`` — callers
    that need the verbatim line can take it from there.

    Single source of truth for "which lines of this file are structure". Every
    scan in this module goes through it, because a raw-text regex cannot tell a
    real declaration from text that merely looks like one inside a ``run: |``
    step — and this file's scans are the ones that decide whether ``--resync``
    rewrites a repo's CI config. A ``uses: …tests-reusable.yaml`` line quoted
    inside a documentation job's shell would otherwise be located as *the*
    reusable call, and a ``force-external-runtime: true`` under it read as that
    job's input and hoisted into the rendered ``jobs.tests.with`` — forcing the
    external runtime on a repo that never asked for it, the FND-65 boot-failure
    class in reverse. ``declared_keys`` already skipped scalar bodies, so the
    key-set guard could not catch that one either: the key it would have refused
    over is invisible to it by design.

    Blanked rather than dropped so the two needs cannot diverge: the boundary
    walks below reason about indentation and adjacency, which a compacted list
    would silently change, while ``extract_secrets_block`` splices the original
    bytes and must index back into them.
    """
    out: list[str] = []
    skip_deeper_than: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        indent = len(line) - len(line.lstrip())
        if skip_deeper_than is not None:
            if indent > skip_deeper_than:
                out.append("")
                continue
            skip_deeper_than = None
        if stripped.startswith("#"):
            out.append("")
            continue
        out.append(line)
        m = _KEY_LINE_RE.match(line)
        if m is not None and _BLOCK_SCALAR_RE.search(line):
            skip_deeper_than = len(m.group("indent"))
    return out


def _outdents(lines: list[str], index: int, limit: int) -> bool:
    """True when *lines*[*index*] starts a mapping shallower than *limit*.

    Blank lines never outdent — they carry no indentation of their own, and a
    blank line inside a job body (or one left behind by a blanked comment) must
    not end it.
    """
    line = lines[index]
    return bool(line.strip()) and len(line) - len(line.lstrip()) < limit


def _reusable_job_scope(lines: list[str]) -> tuple[int, int, int] | None:
    """Locate the job calling ``tests-reusable.yaml`` in *lines*.

    *lines* must be ``structural_lines`` output. Returns ``(start, end,
    key_indent)`` — the half-open line range of the job's body and the
    indentation of its own keys — or ``None`` when no such job is present.

    The scope for every read that is *about that job*: its inputs
    (``force-external-runtime``) and its ``secrets:`` mapping. A repo's
    tests.yaml is free to carry other jobs — an aggregator gate, a hand-kept
    ``tests-passed`` whose name branch protection requires — and any of them may
    declare a key that is also one of the reusable's. Reading file-wide
    attributes that key to the reusable call and re-renders it as one of *its*
    values, which is a value nobody declared.

    Nothing is lost by scoping this narrowly. A `secrets:` or an input outside
    this job necessarily sits under another job, and another job means keys the
    canonical has no place for — so ``unpreserved_declarations`` refuses that
    file anyway. The narrow read cannot silently drop what the guard already
    stops. Same for a file with no reusable job at all: the re-render would add
    one and delete that file's own jobs, which the guard refuses on their keys.

    Located by walking indentation rather than parsing: this package ships
    standalone into consumer repos and takes no YAML dependency (see
    ``declared_keys``). Keyed on the reusable's own path rather than on the job
    being named ``tests``, because the job name is the branch-protection context
    string and a repo is free to change it, while a job that calls this workflow
    is by definition the one whose inputs these are.
    """
    uses_at = next(
        (i for i, line in enumerate(lines) if _TESTS_REUSABLE_USES_RE.match(line)),
        None,
    )
    if uses_at is None:
        return None
    key_indent = len(lines[uses_at]) - len(lines[uses_at].lstrip())
    # Outward from `uses:` until a line shallower than its siblings. Both ways,
    # because `with:` (and `secrets:`) may be written above `uses:`.
    start = uses_at
    while start > 0 and not _outdents(lines, start - 1, key_indent):
        start -= 1
    end = uses_at + 1
    while end < len(lines) and not _outdents(lines, end, key_indent):
        end += 1
    return start, end, key_indent


def _reusable_job_key_line(text: str, key: str) -> tuple[list[str], int, int] | None:
    """Find *key* among the reusable job's own keys in *text* (a tests.yaml).

    Returns ``(structural_lines, index, key_indent)`` for the first match, or
    ``None`` when the file has no reusable job or that job does not declare
    *key* as one of its own (sibling-of-``uses:``) keys. Shared by the
    ``with:``, ``secrets:``-block and inline-``secrets:`` reads so all three
    agree on what counts as "declared by that job".
    """
    lines = structural_lines(text)
    scope = _reusable_job_scope(lines)
    if scope is None:
        return None
    start, end, key_indent = scope
    for i in range(start, end):
        line = lines[i]
        if len(line) - len(line.lstrip()) != key_indent:
            continue
        m = _KEY_LINE_RE.match(line)
        if m is not None and _yaml_key(m.group("key")) == key:
            return lines, i, key_indent
    return None


def reusable_job_with_block(text: str) -> str:
    """Return the body of the ``with:`` mapping of *text*'s reusable-tests job.

    *text* is a tests.yaml. The return value is the block's child lines only
    (the ``with:`` line itself excluded), or ``""`` when the file has no job
    calling ``tests-reusable.yaml``, or that job declares no ``with:``.

    Structure only — see ``structural_lines`` — so an input named inside a
    ``run: |`` step cannot read as one the job actually passes.
    """
    found = _reusable_job_key_line(text, "with")
    if found is None:
        return ""
    lines, with_at, key_indent = found
    body_end = with_at + 1
    while body_end < len(lines) and not _outdents(lines, body_end, key_indent + 1):
        body_end += 1
    return "\n".join(lines[with_at + 1 : body_end])


def extract_force_external_runtime(text: str) -> str:
    """Return ``"true"`` when *text* (a tests.yaml) forces the external runtime.

    Anything else — absent, ``false``, unparseable — returns ``""`` ("say
    nothing, take the SDK default"), for the same reason
    ``extract_use_ghcr_base`` does: rendering an explicit ``false`` would be a
    second spelling of the input default and would read as C002 drift on every
    repo that spells it the other way.

    Read through ``extract_field`` (which accepts bare or quoted values at any
    indentation) rather than an anchored regex like the lines above, because the
    apps that hand-wrote this input put it at two different positions and the
    value appears as both ``true`` and ``"true"``. Failing to read one of those
    spellings means ``--resync`` deletes the line, and the connector's
    ``main.py`` — which still expects external daprd at :3500 / Temporal at
    :7233 — then fails to boot with ``DaprNotDetectedError`` (FND-65).

    That tolerance is confined to the reusable job's own ``with:`` block rather
    than applied file-wide: this is an input of that job, and hoisting a match
    from anywhere else in the file into the rendered ``with:`` would force the
    external runtime on a repo that never asked for it. A declaration this
    cannot see is not silently dropped either — it reaches
    ``unpreserved_declarations`` as a key the re-render would lose, which
    refuses the resync instead.
    """
    scope = reusable_job_with_block(text)
    return (
        "true"
        if scope and extract_field(scope, "force-external-runtime") == "true"
        else ""
    )


def extract_secrets_block(text: str) -> str:
    """Return *text*'s explicit ``secrets:`` mapping verbatim, or ``""``.

    *text* is a tests.yaml. The return value is the block exactly as written —
    the ``secrets:`` line, every more-indented line under it, and any contiguous
    run of comment lines directly above it at the same indentation — with no
    trailing newline, so it drops straight into the template in place of the
    canonical ``secrets: inherit``.

    Spliced verbatim rather than modelled as parameters because the mapping's
    *contents* are per-connector and unknowable here: which source-credential
    secret NAMES exist, how many auth flavours they cover, whether a value
    carries a ``||`` default, and which of them are folded into the
    ``E2E_SOURCE_ENV_JSON`` object the reusable exports before the app server
    and pytest start. The preceding comments come along because they are the
    only record of why the repo dropped ``inherit``, and losing them is the same
    class of silent loss as losing the mapping itself.

    ``secrets: inherit`` returns ``""``: it is the canonical default, and
    re-rendering it from a captured block rather than from the template would
    make every non-customised repo's bytes depend on this extractor.

    Read from the reusable job's own keys only — see ``_reusable_job_scope``.
    Splicing a ``secrets:`` mapping found under a *different* job into the
    rendered ``jobs.tests`` would fabricate credential wiring on a job that
    never declared it, which is worse than dropping it: the guard already
    refuses any file carrying another job.

    The job's first ``secrets:`` line wins, and only in mapping form. A file
    carrying two is a duplicate YAML key that GitHub rejects outright, so
    choosing between them is not this function's job — see FND-604 on the repair
    trap that produces one, and note that the re-render emits a single
    ``secrets:`` either way, so resyncing such a file collapses the duplicate
    rather than propagating it.
    """
    found = _reusable_job_key_line(text, "secrets")
    if found is None:
        return ""
    structural, start, indent = found
    if not _SECRETS_MAPPING_RE.match(structural[start]):
        return ""
    # Boundaries come off the structural view; the returned bytes come off the
    # original, because this value is spliced into the template verbatim and its
    # comments are the only record of why the repo dropped `inherit`.
    lines = text.splitlines()
    # Children: every following line that is blank (interior blank lines are
    # part of the block) or indented deeper than `secrets:` itself.
    # Boundaries come off the structural view, but a comment line is blank
    # there, and `_outdents` never treats a blank line as a boundary — so a
    # comment at `secrets:`'s own indent or shallower would be walked through
    # and spliced into the block (it belongs to whatever follows). Stop on the
    # original line: a same/shallower-indent comment ends the block just as a
    # same/shallower-indent key does.
    end = start + 1
    while end < len(structural) and not _outdents(structural, end, indent + 1):
        if lines[end].strip().startswith("#"):
            comment_indent = len(lines[end]) - len(lines[end].lstrip())
            if comment_indent <= indent:
                break
        end += 1
    # A mapping needs at least one child. Without this, a `secrets:` line with
    # nothing under it — which YAML reads as null, not as a mapping — would be
    # captured and re-rendered, replacing the working `inherit` default with a
    # line that passes no secrets at all.
    if not any(line.strip() for line in structural[start + 1 : end]):
        return ""
    # Trailing blank lines belong to whatever follows the block, not to it.
    # Measured on the original: a comment line the structural view blanked is
    # part of the block's own text and must not be trimmed off its end.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    # Contiguous same-indent comments directly above are part of the block.
    head = start
    while head > 0:
        above = lines[head - 1]
        stripped = above.strip()
        if not stripped.startswith("#") or len(above) - len(above.lstrip()) != indent:
            break
        head -= 1
    return "\n".join(lines[head:end])


def unpreservable_secrets_form(text: str) -> str:
    """Return *text*'s inline ``secrets:`` value if it cannot be carried forward.

    *text* is a tests.yaml. Returns the value as written, or ``""`` when the file
    declares nothing at risk — no ``secrets:`` at all, the canonical
    ``secrets: inherit``, or a block-form mapping (which
    ``extract_secrets_block`` splices verbatim).

    The gap this closes is the one shape that is invisible to *both* halves of
    the FND-604 fix at once. An inline form — a flow mapping
    (``secrets: {E2E_SOURCE_ENV_JSON: ...}``), an alias (``secrets: *shared``) —
    is not block form, so ``extract_secrets_block`` returns ``""`` and the
    re-render emits ``secrets: inherit``. And ``secrets`` is a key on both sides
    of the key-set comparison, so ``unpreserved_declarations`` reads the shared
    name as proof of preservation and the refusal never fires. The mapping is
    replaced by ``inherit``, which can neither compose nor rename, and the
    integration and e2e legs run with no source credentials — the original
    defect, reached through a spelling the first fix did not cover.

    Deliberately reports rather than tries to preserve: routing the form to the
    refusal leaves the file untouched, whereas splicing an inline mapping into a
    template that emits block form would have to rewrite it to do so, and a
    guessed transcription of a repo's credential wiring is the one thing this
    module must not produce. Anything other than the two known-safe shapes
    therefore counts, so an unrecognised spelling stops the resync instead of
    being assumed harmless.

    Scoped to the reusable job's own keys, the same boundary
    ``extract_secrets_block`` reads within — so the pair cannot disagree about
    which ``secrets:`` declaration the re-render is even about. A file-wide scan
    reported an inline ``secrets:`` under an unrelated job as unpreservable and
    appended ``secrets`` to the refusal list, although the re-render only
    rewrites ``jobs.tests``; harmless while any extra job is refused on its own
    keys, but it would block a resync outright the day the canonical grows a
    sibling job of its own.
    """
    found = _reusable_job_key_line(text, "secrets")
    if found is None:
        return ""
    structural, at, _ = found
    m = _SECRETS_INLINE_RE.match(structural[at])
    if m is None:
        return ""
    # A trailing comment is not part of the value. Split on whitespace-then-#
    # so a `#` inside the value itself does not truncate it; the result only
    # ever has to be distinguishable from `inherit`.
    value = re.split(r"[ \t]#", m.group("value"), maxsplit=1)[0].strip()
    return value if value and value != "inherit" else ""


def declared_keys(text: str) -> list[str]:
    """Return the distinct YAML mapping keys *text* declares, in file order.

    Structure only, via ``structural_lines``: comment lines are skipped, and the
    body of a block scalar (``key: |``, ``key: >-``) is skipped as opaque content
    rather than mined for keys that a hand-written ``run:`` step happens to
    contain.

    Key *names* rather than key paths, because tests.yaml legitimately repeats a
    name at several depths (every ``workflow_dispatch`` input has its own
    ``description`` / ``required`` / ``default`` / ``type``) and a set of names
    is the coarsest comparison that still answers the only question
    ``unpreserved_declarations`` asks — "does this file say something the
    canonical has no place for?" — without a YAML parser this package does not
    depend on.
    """
    keys: list[str] = []
    for line in structural_lines(text):
        m = _KEY_LINE_RE.match(line)
        if m is None:
            continue
        key = _yaml_key(m.group("key"))
        if key not in keys:
            keys.append(key)
    return keys


def unpreserved_declarations(existing: str, rerendered: str) -> list[str]:
    """Return the keys *existing* declares that *rerendered* would drop.

    The generalised guard FND-604 asked for: ``--resync`` re-renders a whole
    write-if-absent scaffold from its canonical template, so anything the
    template has no place for is deleted. Naming those keys turns a silent loss
    into a refusal that says what it refused over — and, unlike a line diff,
    generalises to whatever the next unrecognised per-repo value turns out to
    be, without needing to have anticipated it.

    Compares *sets of key names*, deliberately: the audit script that first
    caught this reported a line that merely *moved* as a removal, and
    reapplying on that reading duplicates a YAML key so only the last copy
    survives. A key that moved is present on both sides and reads as preserved,
    which it is.

    Detects *dropped* declarations only. A key present on both sides whose value
    the re-render would change is a different (and far less damaging) class, and
    is left to the ``.bak`` and to C002's own drift finding.
    """
    kept = set(declared_keys(rerendered))
    return [key for key in declared_keys(existing) if key not in kept]


def unpreserved_tests_yaml_declarations(existing: str, rerendered: str) -> list[str]:
    """``unpreserved_declarations`` minus the one drop that is policy, not loss.

    A ``unit-coverage-fail-under`` *below* ``SDK_UNIT_COVERAGE_FLOOR`` is the
    single declaration this module refuses to preserve on purpose — an app may
    raise its coverage floor above the SDK's, not use its own workflow to duck
    under a fleet-wide bar — so ``--resync`` deleting that line is the intended
    remediation, announced ahead of time by C002's own message. Letting it reach
    the generalised guard would invert that: the resync would refuse, and the
    sub-floor line would survive every run.

    The carve-out is conditioned on the value actually reading back as a
    sub-floor number, not on the key name alone. A spelling the extractor cannot
    parse (``unit-coverage-fail-under: ninety``) is not a decision anyone made,
    so it still counts as unpreserved and still stops the resync.

    ``secrets`` is added in the other direction, because for that key alone a
    shared name is *not* evidence of preservation: an inline mapping and the
    canonical ``inherit`` both spell the key ``secrets``, so the key-set
    comparison cannot tell a preserved mapping from one about to be overwritten
    by ``inherit``. ``unpreservable_secrets_form`` makes that distinction on the
    value, and anything it names has to reach the refusal the same way a dropped
    key does.
    """
    dropped = unpreserved_declarations(existing, rerendered)
    if rejected_unit_coverage_fail_under(existing):
        dropped = [key for key in dropped if key != "unit-coverage-fail-under"]
    if unpreservable_secrets_form(existing) and "secrets" not in dropped:
        dropped.append("secrets")
    return dropped


# Enough to name the whole of a realistic per-repo customisation (an explicit
# secrets mapping plus a forced runtime came to two), small enough that the
# message stays readable when a repo carries an extra job — whose every key
# (``needs``, ``runs-on``, ``steps``, ...) counts as a dropped declaration.
_MAX_DROPPED_LISTED = 6


def format_dropped_declarations(keys: list[str]) -> str:
    """Render *keys* as a bounded, quoted list for a one-line message.

    Shared by ``--resync``'s refusal and C002's explanation of it, so the two
    descriptions of the same file cannot list different keys or elide at
    different points. The count is always exact even when the list is elided.
    """
    shown = ", ".join(f"`{key}`" for key in keys[:_MAX_DROPPED_LISTED])
    hidden = len(keys) - _MAX_DROPPED_LISTED
    return f"{shown} (+{hidden} more)" if hidden > 0 else shown


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
    # Quoted and bare forms capture into different groups; exactly one is set.
    return next((g for g in m.groups() if g is not None), "") if m else ""


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

    A *quoted* value is read to its closing quote, so one containing spaces
    survives whole. Stopping at the first whitespace instead would truncate it
    and hand the caller a different value than the file declares — the same
    silent-loss class this function exists to avoid, one dimension over. A bare
    value still ends at the first whitespace, which is what keeps a trailing
    ``# comment`` out of it.
    """
    for line in text.splitlines():
        m = re.match(
            rf"^\s*(?:{re.escape(field)}|\"{re.escape(field)}\"|'{re.escape(field)}'):\s*"
            r"(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>\S+))",
            line,
        )
        if m is None:
            continue
        # An unterminated quote matches no quoted arm and falls to `bare`, where
        # the strip keeps the pre-existing reading of that near-invalid form.
        for group in ("dq", "sq"):
            if m.group(group) is not None:
                return m.group(group)
        return m.group("bare").strip("\"'")
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
