"""Rule ids, shared constants and the pyproject / requirement parsing every D-series rule reads."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from conformance.suite.checks._ast_common import (
    SuppressionsMap,
    _is_suppressed,
    is_sdk_package_name,
)
from conformance.suite.schema.findings import Finding

SERIES = "D"


RULE_D001 = "D001"


RULE_D002 = "D002"


RULE_D003 = "D003"


RULE_D004 = "D004"


RULE_D005 = "D005"


RULE_D006 = "D006"


RULE_D007 = "D007"


RULE_D008 = "D008"


RULE_D009 = "D009"


RULE_D010 = "D010"


RULE_D011 = "D011"


RULE_D012 = "D012"


RULE_D013 = "D013"


RULE_D014 = "D014"


RULE_D015 = "D015"


RULE_D016 = "D016"


RULE_D017 = "D017"


RULE_D018 = "D018"


SDK_PACKAGE = "atlan-application-sdk"


# The conformance suite itself (D011).  Apps declare it in a dev group so
# the remediation loop's ``uv run atlan-application-sdk-conformance``
# invocation resolves; it is never a runtime dependency.
CONFORMANCE_PACKAGE = "atlan-application-sdk-conformance"


# The canonical build backend for Atlan apps (D007).
HATCHLING_BACKEND = "hatchling.build"


# The default package index every repo must pin, and the hosts a committed
# lockfile may name (D012/D013).
#
# ``files.pythonhosted.org`` is where PyPI serves artefacts and is what every
# URL in a cleanly-resolved lock points at.  ``pypi.org`` is allowed because uv
# has used it for some metadata URLs, and excluding it would fail for a reason
# unrelated to this check's purpose.
PYPI_SIMPLE_URL = "https://pypi.org/simple"


PYPI_LOCK_HOSTS = frozenset({"files.pythonhosted.org", "pypi.org"})


# Artifact download URLs in a uv.lock (D013): ``sdist = { url = "..." }`` and
# wheel entries ``{ url = "..." }`` inside a ``wheels`` array.
#
# Matched with a regex rather than a TOML parser because the lockfile schema
# changes between uv versions while the URL spelling is stable.  Deliberately
# *not* line-anchored: uv writes these inside inline tables, so a line-anchored
# pattern matches nothing at all.  The negative lookbehind excludes
# ``source = { url = "..." }`` — that is a direct archive dependency, not an
# index rewrite — and a longer key that ends in ``url`` (e.g. a future
# ``direct_url``) never matches because the pattern requires ``{ url``.
_LOCK_URL_RE = re.compile(r'(?:sdist\s*=\s*)?(?<!source = )\{\s*url = "([^"]+)"')


# Substrings that mean an index credential rode into a committed lockfile
# (D013): Endor's API-key prefix, raw and url-encoded, and userinfo attached to
# the proxy host.  uv strips userinfo from the URLs it records, so these are a
# backstop for a future uv (or a different tool) that does not.
_LOCK_CREDENTIAL_MARKERS: tuple[str, ...] = ("endr%2B", "endr+", "@factory.")


# pyright type-checking modes weaker than the SDK baseline ``standard`` (D008).
PYRIGHT_WEAK_MODES = frozenset({"off", "basic"})


# Matches a poe task fetching Dapr component YAMLs straight from GitHub
# (raw.githubusercontent.com or the contents API) for atlanhq/application-sdk,
# instead of copying them from the installed wheel, which bundles them at
# application_sdk/components/ (D009). Unauthenticated GitHub requests hit rate
# limits under CI concurrency, and a hardcoded ref drifts from whatever SDK
# version is actually locked in the app's uv.lock.
_REMOTE_COMPONENT_FETCH_RE = re.compile(
    r"(?:raw\.githubusercontent\.com|api\.github\.com)[^\s\"'\\]*"
    r"/atlanhq/application-sdk(?![\w-])",
    re.IGNORECASE,
)


# The SDK's own ``[project].requires-python`` lower bound, as ``(major, minor)``.
# Hardcoded (not read from metadata) so D006 stays a pure-text check that works
# under the isolated ``uvx`` CI leg without the SDK being importable. Kept honest
# by ``test_d006_sdk_python_floor_matches_sdk_pyproject``, which re-reads the
# SDK's real pyproject.toml and fails if this constant drifts.
SDK_PYTHON_FLOOR: tuple[int, int] = (3, 11)


# The Python ``app-runtime-base:3`` ships, as ``(major, minor)``. D016 requires
# every app's requires-python to admit it. Hardcoded for the same reason as
# SDK_PYTHON_FLOOR; ``test_d016_runtime_python_matches_sdk_dockerfile`` re-reads
# the golden base tag in the SDK's Dockerfile and fails if this drifts.
RUNTIME_IMAGE_PYTHON: tuple[int, int] = (3, 13)


@dataclass(frozen=True)
class DeprecatedSdkExtra:
    """One deprecated shim extra of ``atlan-application-sdk`` (D017)."""

    removed_in: str
    """SDK version whose release deletes the extra."""

    replacement: str | None
    """Extra to use instead, or ``None`` when the packages are core and the
    extra is simply dropped."""


# The SDK's deprecated shim extras (D017), keyed by PEP 503-normalised name.
# Every one installs nothing core or [sql] does not already provide. Hardcoded
# (an extra has no deprecation metadata to read) so D017 stays pure-text; kept
# honest by ``test_d017_shim_table_matches_sdk_pyproject``, which re-reads the
# SDK's real pyproject.toml and fails if an entry is no longer redundant, or if
# an SDK extra installs nothing distinct without being listed here.
DEPRECATED_SDK_EXTRAS: Mapping[str, DeprecatedSdkExtra] = {
    "workflows": DeprecatedSdkExtra(removed_in="4.0.0", replacement=None),
    "storage": DeprecatedSdkExtra(removed_in="4.0.0", replacement=None),
    "iam-auth": DeprecatedSdkExtra(removed_in="4.0.0", replacement=None),
    "azure": DeprecatedSdkExtra(removed_in="4.0.0", replacement=None),
    "daft": DeprecatedSdkExtra(removed_in="4.0.0", replacement="sql"),
    "sqlalchemy": DeprecatedSdkExtra(removed_in="4.0.0", replacement="sql"),
    "pandas": DeprecatedSdkExtra(removed_in="4.0.0", replacement="sql"),
}


# SDK extras that install test tooling only (D018), keyed by PEP 503-normalised
# name, mapped to what they add beyond the SDK's core dependencies. ``[tests]``
# carries pytest, pytest-asyncio, pandera and httpx2 for
# ``application_sdk.testing``; ``[harness]`` carries the
# kubernetes client the e2e harness imports under the e2e/scenario markers.
# Neither is on a production code path, so neither belongs in the array a
# production sync installs. Kept honest by
# ``test_d018_test_extras_match_sdk_pyproject``, which re-reads the SDK's
# pyproject.toml and fails if an entry is no longer published or the packages
# it names drift from what the extra adds.
TEST_ONLY_SDK_EXTRAS: Mapping[str, str] = {
    "tests": "pytest, pytest-asyncio, pandera and httpx2",
    "harness": "the kubernetes client",
}


# A PEP 508 / requirement-string fragment. Captures the package *name*; the
# rest of the line is treated as the version-specifier blob.
_REQ_RE = re.compile(
    r"""
    ^                                       # start of trimmed value
    (?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)  # PEP 503 normalisable name
    (?:\[(?P<extras>[^\]]*)\])?             # optional [extras]
    \s*
    (?P<rest>.*?)                           # specifier and/or marker
    \s*$
    """,
    re.VERBOSE,
)


# A TOML string in a dependency array: a basic ``"..."`` string or a literal
# ``'...'`` string. TOML allows both; reading only the double-quoted form let a
# requirement spelt with single quotes skip every D-series check on it.
_TOML_STRING_PATTERN = r'"(?P<dq>(?:[^"\\]|\\.)*)"' + r"|'(?P<sq>[^'\n]*)'"


_TOML_STRING_RE = re.compile(_TOML_STRING_PATTERN)


# A string entry on its own line inside an array: ``"requirement-spec",``
# (optional comma, optional inline ``# comment``).
_ARRAY_ENTRY_RE = re.compile(
    r"^(?P<lead>\s*)(?:" + _TOML_STRING_PATTERN + r")\s*,?\s*(?:#.*)?$"
)


def _toml_string_value(match: re.Match[str]) -> str:
    """The contents of a :data:`_TOML_STRING_RE` match, either quote style."""
    dq = match.group("dq")
    return dq if dq is not None else match.group("sq")


@dataclass(frozen=True)
class _DepEntry:
    """A single dependency entry parsed from a TOML array, with line metadata."""

    raw: str
    """Raw requirement string as written, e.g. ``"pydantic>=2,<3"``."""

    name: str
    """PEP 503-normalised package name (lowercased, ``-``/``_``/``.`` collapsed)."""

    line: int
    """1-based line number in the source TOML."""

    column: int
    """1-based column where the requirement string starts on its line."""

    array_path: str
    """Dotted-key path of the array, e.g. ``"project.dependencies"`` or
    ``"project.optional-dependencies.sql"``."""


def _normalise_name(name: str) -> str:
    """PEP 503 normalisation: lowercase + ``-``/``_``/``.`` collapsed to ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _strip_marker(raw: str) -> str:
    """Drop the optional environment marker (``; sys_platform != 'win32'``)."""
    idx = raw.find(";")
    return raw[:idx].rstrip() if idx != -1 else raw


def _parse_requirement(raw: str) -> tuple[str, str] | None:
    """Return (normalised name, specifier-blob) or ``None`` if unparseable.

    The specifier blob is the part after the optional ``[extras]`` and before
    any environment marker; for our purposes we only care whether it bounds
    the version.
    """
    body = _strip_marker(raw).strip()
    if not body:
        return None
    m = _REQ_RE.match(body)
    if m is None:
        return None
    return _normalise_name(m.group("name")), m.group("rest").strip()


def _sdk_extras_in(raw: str) -> list[str]:
    """Return the raw ``[extras]`` names declared on a requirement string.

    ``"atlan-application-sdk[sql,tests]>=3,<4"`` → ``["sql", "tests"]``.
    Returns ``[]`` when there is no extras group or the string is unparseable.
    """
    body = _strip_marker(raw).strip()
    m = _REQ_RE.match(body)
    if m is None:
        return []
    blob = m.group("extras") or ""
    return [e.strip() for e in blob.split(",") if e.strip()]


def _sdk_requirement_proves_pre_removal(spec: str, removed_in: str) -> bool:
    """Return True only when a PEP 440 specifier proves the SDK stays pre-removal.

    D017 matters when resolution can select the SDK release that deletes the
    extra. A finite upper constraint below that release makes the reference
    safe under its current range. Unbounded, malformed, or otherwise uncertain
    ranges are not proof and remain subject to D017.
    """
    if not spec.strip():
        return False
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet  # noqa: PLC0415 — guarded by the deliberate ImportError fallback below
        from packaging.version import InvalidVersion, Version  # noqa: PLC0415 — guarded by the deliberate ImportError fallback below
    except ImportError:
        return False
    try:
        specifiers = SpecifierSet(spec)
        removal = Version(removed_in)
    except (InvalidSpecifier, InvalidVersion):
        return False

    def bump_release(version: Version, keep: int) -> Version | None:
        """Increment the last of the first *keep* release segments.

        The epoch is carried over: ``Version.release`` drops it, and a ceiling
        rebuilt without it would compare as epoch 0 — below every epoch-1
        release the specifier actually admits.
        """
        release = list(version.release[:keep])
        if not release:
            return None
        release[-1] += 1
        return Version(f"{version.epoch}!" + ".".join(str(part) for part in release))

    def wildcard_upper_bound(raw: str) -> Version | None:
        if not raw.endswith(".*"):
            return None
        try:
            prefix = Version(raw[:-2])
        except InvalidVersion:
            return None
        return bump_release(prefix, len(prefix.release))

    for clause in specifiers:
        operator = clause.operator
        raw_bound = clause.version
        if operator in {"<", "<="}:
            try:
                bound = Version(raw_bound)
            except InvalidVersion:
                continue
            if bound < removal or (bound == removal and operator == "<"):
                return True
        elif operator == "~=":
            try:
                floor = Version(raw_bound)
            except InvalidVersion:
                continue
            if len(floor.release) < 2:
                continue
            ceiling = bump_release(floor, len(floor.release) - 1)
            if ceiling is not None and ceiling <= removal:
                return True
        elif operator == "==":
            upper = wildcard_upper_bound(raw_bound)
            if upper is not None:
                if upper <= removal:
                    return True
                continue
            try:
                bound = Version(raw_bound)
            except InvalidVersion:
                continue
            if bound < removal:
                return True
        elif operator == "===":
            try:
                bound = Version(raw_bound)
            except InvalidVersion:
                continue
            if bound < removal:
                return True
    return False


def _is_floating_range(spec: str) -> bool:
    """Return True iff *spec* is capped but unfrozen — it can still float (D011).

    Deliberately different from :func:`_is_bounded_specifier`, which accepts
    ``==X`` and ``~=X.Y`` as "bounded".  For the conformance suite an exact or
    compatible-release pin is the defect, not a satisfying answer: the D-series
    CI leg resolves the suite from the app's ``uv.lock``, so a frozen specifier
    freezes the ruleset that grades the repo.  The canonical form is
    ``<=1.0.0`` — the major boundary alone — which lets a lockfile refresh pick
    up every release below it.

    A floor is *optional*, not required: it neither freezes nor unfreezes the
    ruleset (resolution takes the newest version under the cap either way), so
    ``>=<floor>,<1.0.0`` — the form the fleet already carries — floats just as
    well and stays accepted.

    Rejected: bare names, exact (``==`` / ``===``), compatible-release (``~=``),
    and lower-only ranges (``>=0.17.0`` with no cap, which admits an unreviewed
    major).
    """
    spec = spec.strip()
    if not spec:
        return False
    clauses = [c.strip() for c in spec.split(",") if c.strip()]
    if not clauses:
        return False

    has_upper = False
    for clause in clauses:
        if clause.startswith(("===", "==", "~=")):
            # A pin of any flavour freezes the ruleset — never floating.
            return False
        if clause.startswith("<"):
            # covers both ``<`` and ``<=``
            has_upper = True
    return has_upper


def _is_bounded_specifier(spec: str) -> bool:
    """Return True iff *spec* has both a lower and an upper bound.

    Recognised bounds:

    * ``==X``                           — exact (lower == upper)
    * ``~=X.Y[.Z]``                     — PEP 440 compatible-release (implicit upper)
    * ``>=A`` *and* ``<B`` (any order)  — explicit two-sided range
    * ``>=A,<=B`` / ``>A,<B`` etc.      — any combination of strict/non-strict

    Bare ``>=A`` (no upper) and bare names are not bounded.
    """
    spec = spec.strip()
    if not spec:
        return False

    # Split the comma-joined clauses, ignoring whitespace.
    clauses = [c.strip() for c in spec.split(",") if c.strip()]
    if not clauses:
        return False

    has_lower = False
    has_upper = False
    for clause in clauses:
        if clause.startswith("==="):
            # ``===X`` is arbitrary-equality — counts as exact.
            return True
        if clause.startswith("=="):
            # ``==X`` is exact — counts as both bounds.
            return True
        if clause.startswith("~="):
            # ``~=X.Y`` is PEP 440 compatible release: ``>=X.Y, <X+1``.
            return True
        if clause.startswith(">="):
            has_lower = True
        elif clause.startswith(">"):
            has_lower = True
        elif clause.startswith("<="):
            has_upper = True
        elif clause.startswith("<"):
            has_upper = True
        elif clause.startswith("!="):
            # Exclusion alone never bounds.
            continue
    return has_lower and has_upper


def _safe_load(text: str) -> dict[str, Any] | None:
    """Parse *text* as TOML once; return the dict or ``None`` if unparseable.

    Helpers accept the result via their ``data`` keyword so a single
    ``scan_text`` parse is reused instead of re-parsing per helper.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _iter_dep_entries(
    text: str, *, data: Mapping[str, Any] | None = None
) -> Iterator[_DepEntry]:
    """Yield every dep entry from ``[project.dependencies]`` and
    ``[project.optional-dependencies.*]`` arrays, preserving source lines.

    ``tomllib`` does not expose source positions, so we re-walk the raw text
    line-by-line using a small state machine that tracks the current array
    context. This is sufficient for the well-formed TOML produced by ``uv``
    and ``hatch`` and is robust against most hand-written variants.

    ``data`` lets the caller pass the already-parsed document so the
    parseability check below does not re-parse.
    """
    array_path: str | None = None
    in_array = False
    extras_table: str | None = None  # current [project.optional-dependencies] subtable

    # Validate parseability once (line numbers still come from the raw-text walk
    # below — tomllib loses them).
    if data is None and _safe_load(text) is None:
        return  # unparseable input — emit no findings (caller decides)

    table_re = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")

    # Heuristic: ``dependencies = [`` opens an array under the current table.
    deps_open_re = re.compile(r"^\s*dependencies\s*=\s*\[\s*(?:#.*)?$")
    # In-line array on the same line: ``dependencies = ["a", "b"]`` — rare in
    # generated lockfiles but we still handle the open-and-close-on-one-line
    # variant by treating the bracket region as the array.
    deps_inline_re = re.compile(
        r"^\s*dependencies\s*=\s*\[\s*(?P<body>.*?)\s*\]\s*(?:#.*)?$"
    )
    extras_open_re = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*\[\s*(?:#.*)?$")
    extras_inline_re = re.compile(
        r"^\s*([A-Za-z0-9_-]+)\s*=\s*\[\s*(?P<body>.*?)\s*\]\s*(?:#.*)?$"
    )
    array_close_re = re.compile(r"^\s*\]\s*(?:#.*)?$")
    entry_re = _ARRAY_ENTRY_RE

    current_table = ""
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        # Table header line — updates the active table for the next array.
        m = table_re.match(raw_line)
        if m and not in_array:
            current_table = m.group(1).strip()
            extras_table = (
                current_table
                if current_table.startswith("project.optional-dependencies.")
                or current_table == "project.optional-dependencies"
                else None
            )
            continue

        if not in_array:
            # ``dependencies = [`` opening (multi-line)
            if deps_open_re.match(raw_line) and current_table == "project":
                array_path = "project.dependencies"
                in_array = True
                continue
            # ``dependencies = ["x", "y"]`` (inline, single-line)
            inline = deps_inline_re.match(raw_line)
            if inline and current_table == "project":
                body = inline.group("body")
                # Match each "string" entry within the inline body.
                for sm in _TOML_STRING_RE.finditer(body):
                    entry = _toml_string_value(sm)
                    parsed = _parse_requirement(entry)
                    if parsed is None:
                        continue
                    name, _spec = parsed
                    yield _DepEntry(
                        raw=entry,
                        name=name,
                        line=lineno,
                        # Offset within the body (not raw_line.index, which
                        # would alias duplicate strings to the first match).
                        column=inline.start("body") + sm.start() + 2,
                        array_path="project.dependencies",
                    )
                continue
            # ``<extra-name> = [`` inside [project.optional-dependencies]
            if extras_table is not None:
                em = extras_open_re.match(raw_line)
                if em is not None:
                    extra_name = em.group(1)
                    array_path = f"{extras_table}.{extra_name}"
                    in_array = True
                    continue
                # ``<extra-name> = ["req"]`` (inline single-line form)
                ei = extras_inline_re.match(raw_line)
                if ei is not None:
                    extra_name = ei.group(1)
                    inline_path = f"{extras_table}.{extra_name}"
                    body = ei.group("body")
                    for sm in _TOML_STRING_RE.finditer(body):
                        entry = _toml_string_value(sm)
                        parsed = _parse_requirement(entry)
                        if parsed is None:
                            continue
                        name, _spec = parsed
                        yield _DepEntry(
                            raw=entry,
                            name=name,
                            line=lineno,
                            column=ei.start("body") + sm.start() + 2,
                            array_path=inline_path,
                        )
                    continue
            # [project.optional-dependencies] table-of-arrays form:
            # the dotted-key style ``[project.optional-dependencies.sql]``
            # is handled by table_re above; the array opens on a subsequent
            # ``<key> = [`` line — but actually it opens with ``<extra> = [``
            # under the ``[project.optional-dependencies]`` parent. Either
            # subform reaches us via the branches above.
            continue

        # We are inside an array (``in_array`` is True). Read entries until
        # we see the closing ``]``.
        if array_close_re.match(raw_line):
            in_array = False
            array_path = None
            continue

        em = entry_re.match(raw_line)
        if em is None:
            continue
        value = _toml_string_value(em)
        parsed = _parse_requirement(value)
        if parsed is None:
            continue
        name, _spec = parsed
        # Column points at the first char of the requirement string (after
        # the opening quote).
        col = len(em.group("lead")) + 2
        yield _DepEntry(
            raw=value,
            name=name,
            line=lineno,
            column=col,
            array_path=array_path or "project.dependencies",
        )


def _iter_dependency_group_entries(
    text: str, *, data: Mapping[str, Any] | None = None
) -> Iterator[_DepEntry]:
    """Yield string entries from PEP 735 ``[dependency-groups]`` arrays,
    preserving source lines.

    ``[dependency-groups]`` is *not* covered by ``_iter_dep_entries`` (which
    handles only ``[project.dependencies]`` and the optional-dependencies
    arrays), so D002's scope is unchanged.  ``{include-group = "..."}`` table
    entries are not requirement strings and are skipped naturally — only quoted
    string entries match ``entry_re``.  ``array_path`` is
    ``"dependency-groups.<group>"``.  ``data`` lets the caller pass the
    already-parsed document.
    """
    if data is None:
        data = _safe_load(text)
    if data is None or "dependency-groups" not in data:
        return

    table_re = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
    group_open_re = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*\[\s*(?:#.*)?$")
    group_inline_re = re.compile(
        r"^\s*([A-Za-z0-9_-]+)\s*=\s*\[\s*(?P<body>.*?)\s*\]\s*(?:#.*)?$"
    )
    array_close_re = re.compile(r"^\s*\]\s*(?:#.*)?$")
    entry_re = _ARRAY_ENTRY_RE

    in_table = False
    in_array = False
    group_name: str | None = None
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if not in_array:
            m = table_re.match(raw_line)
            if m is not None:
                in_table = m.group(1).strip() == "dependency-groups"
                continue
        if not in_table:
            continue

        if not in_array:
            inline = group_inline_re.match(raw_line)
            if inline is not None:
                grp = inline.group(1)
                for sm in _TOML_STRING_RE.finditer(inline.group("body")):
                    value = _toml_string_value(sm)
                    parsed = _parse_requirement(value)
                    if parsed is None:
                        continue
                    yield _DepEntry(
                        raw=value,
                        name=parsed[0],
                        line=lineno,
                        column=inline.start("body") + sm.start() + 2,
                        array_path=f"dependency-groups.{grp}",
                    )
                continue
            opened = group_open_re.match(raw_line)
            if opened is not None:
                group_name = opened.group(1)
                in_array = True
            continue

        # inside an array
        if array_close_re.match(raw_line):
            in_array = False
            group_name = None
            continue
        em = entry_re.match(raw_line)
        if em is None:
            continue  # e.g. ``{include-group = "dev"}`` — not a requirement
        value = _toml_string_value(em)
        parsed = _parse_requirement(value)
        if parsed is None:
            continue
        yield _DepEntry(
            raw=value,
            name=parsed[0],
            line=lineno,
            column=len(em.group("lead")) + 2,
            array_path=f"dependency-groups.{group_name}",
        )


def _project_name(text: str, *, data: Mapping[str, Any] | None = None) -> str | None:
    """Return ``[project].name`` if parseable, else ``None``."""
    if data is None:
        data = _safe_load(text)
    if data is None:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    name = project.get("name")
    return str(name) if isinstance(name, str) else None


# Only ``>=`` / ``>`` lower-bound clauses are considered. Known gap: an exact
# ``==3.10`` or compatible-release ``~=3.10`` floor below the SDK is not flagged
# (returns None). Real incidence across the fleet is zero and the rule is WARN,
# so this is left as a documented limitation rather than handled with a
# specifier whose message form would misrepresent the operator.
_PY_LOWER_RE = re.compile(r"(>=|>)\s*(\d+)(?:\.(\d+))?")


def _requires_python_lower_bound(
    text: str, *, data: Mapping[str, Any] | None = None
) -> tuple[tuple[int, int], str, int] | None:
    """Return ``((major, minor), operator, lineno)`` for the app's
    ``requires-python`` lower bound, or ``None`` when it is absent or has no
    lower bound. ``operator`` is the matched ``>=`` or ``>`` as written.

    A strict ``>X.Y`` is treated the same as ``>=X.Y`` for floor purposes: it
    still admits ``X.Y.z`` patch releases, which are below the next minor, so
    comparing the declared ``X.Y`` against the SDK floor is the right test.
    """
    if data is None:
        data = _safe_load(text)
    if data is None:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    spec = project.get("requires-python")
    if not isinstance(spec, str):
        return None
    m = _PY_LOWER_RE.search(spec)
    if m is None:
        return None
    operator = m.group(1)
    major = int(m.group(2))
    minor = int(m.group(3)) if m.group(3) is not None else 0

    lineno = 1
    for ln, line in enumerate(text.splitlines(), start=1):
        if re.match(r"^\s*requires-python\s*=", line):
            lineno = ln
            break
    return (major, minor), operator, lineno


def _requires_python_excludes_runtime(data: Mapping[str, Any]) -> str | None:
    """Return the app's ``requires-python`` spec when it shuts out the runtime
    image's Python, else ``None``.

    The runtime image's base tag pins only ``X.Y``; its patch floats as the
    mutable tag is rebuilt. So the spec must admit *every* ``X.Y.N`` patch
    (checked for N in 0..99), not just one: ``<3.13``, ``<=3.12``,
    ``==3.12.*``, ``~=3.12.0``, ``!=3.13.*``, ``==3.13.0`` and ``!=3.13.4`` are
    all caught while ``<3.14``, ``<4.0`` and ``==3.13.*`` pass. Absent or
    unparseable specs return ``None``: D006 owns the absent case, and a spec
    ``packaging`` cannot parse is not graded here.
    """
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    spec = project.get("requires-python")
    if not isinstance(spec, str) or not spec.strip():
        return None
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet  # noqa: PLC0415 — guarded by the ImportError fallback below
    except ImportError:  # pragma: no cover - packaging is a declared dependency
        return None
    try:
        specifiers = SpecifierSet(spec)
    except InvalidSpecifier:
        return None
    major, minor = RUNTIME_IMAGE_PYTHON
    if all(specifiers.contains(f"{major}.{minor}.{patch}") for patch in range(100)):
        return None
    return spec.strip()


def _line_of(text: str, key: str, *, section: str | None = None) -> int:
    """Return the 1-based line of the ``<key> =`` assignment, else 1.

    When *section* is given, only lines inside that ``[section]`` table are
    considered, so a key that also appears in an unrelated table does not
    misanchor the finding. With no *section*, the first match anywhere wins.
    """
    key_pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    table_pat = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
    in_section = section is None
    for ln, line in enumerate(text.splitlines(), start=1):
        table = table_pat.match(line)
        if table is not None:
            in_section = section is not None and table.group(1).strip() == section
            continue
        if in_section and key_pat.match(line):
            return ln
    return 1


def _build_backend(
    text: str, *, data: Mapping[str, Any] | None = None
) -> tuple[str, int] | None:
    """Return ``(build-backend, lineno)`` from ``[build-system]``, or ``None``
    when the key is absent (a missing build backend is not D007's concern)."""
    if data is None:
        data = _safe_load(text)
    if data is None:
        return None
    build_system = data.get("build-system")
    if not isinstance(build_system, dict):
        return None
    backend = build_system.get("build-backend")
    if not isinstance(backend, str):
        return None
    return backend, _line_of(text, "build-backend", section="build-system")


def _pyright_mode(
    text: str, *, data: Mapping[str, Any] | None = None
) -> tuple[str, int] | None:
    """Return ``(typeCheckingMode, lineno)`` from ``[tool.pyright]``, or
    ``None`` when the key is absent (D008 only flags an explicit weak mode)."""
    if data is None:
        data = _safe_load(text)
    if data is None:
        return None
    tool = data.get("tool")
    pyright = tool.get("pyright") if isinstance(tool, dict) else None
    if not isinstance(pyright, dict):
        return None
    mode = pyright.get("typeCheckingMode")
    if not isinstance(mode, str):
        return None
    return mode, _line_of(text, "typeCheckingMode", section="tool.pyright")


def _poe_tasks(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return ``[tool.poe.tasks]`` as a mapping, or ``None`` when absent."""
    tool = data.get("tool")
    poe = tool.get("poe") if isinstance(tool, dict) else None
    tasks = poe.get("tasks") if isinstance(poe, dict) else None
    return tasks if isinstance(tasks, dict) else None


def _iter_strings(value: Any) -> Iterator[str]:
    """Recursively yield every string leaf under a poe task definition.

    A task may be a bare string, a ``{shell = "..."}``/``{cmd = "..."}``/
    ``{interpreter = "python", shell = "..."}`` table, or a sequence-task list
    of steps — this walks all of those shapes uniformly.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)


def _is_self_check(name: str | None) -> bool:
    """Return True for the SDK and its sibling packages (exempt from D-series).

    Delegates to the shared ``is_sdk_package_name`` so this self-exemption and the
    runner-side ``detect_scope`` answer "is this the SDK?" with identical
    (hyphen-anchored) semantics — see ``_ast_common._scope``.
    """
    return name is not None and is_sdk_package_name(name)


def _make_finding(
    *,
    rule_id: str,
    file: str,
    line: int,
    column: int,
    message: str,
    suppressions: SuppressionsMap,
    discriminator: str | None = None,
) -> Finding:
    """Construct a Finding, marking it suppressed if a directive applies.

    *discriminator* names the varying subject when one rule reports several
    findings at one anchor — several packages fenced on a single inline-table
    line, say.  Fingerprints hash ``(rule, uri, line)``, so without it those
    siblings collapse to one SARIF identity and a line-level
    ``# conformance: ignore[...]`` cannot single one of them out.  ``None``
    keeps the pre-discriminator fingerprint and directive behaviour, so every
    existing caller is unchanged.
    """
    suppressed, justification = _is_suppressed(
        suppressions, rule_id, line, discriminator
    )
    return Finding(
        rule_id=rule_id,
        file=file,
        line=line,
        column=column,
        message=message,
        snippet=None,
        discriminator=discriminator,
        suppressed=suppressed,
        suppression_justification=justification,
    )


_SQLALCHEMY_DIALECTS_GROUP = "sqlalchemy.dialects"
