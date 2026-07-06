"""D-series — pyproject.toml conformance against the application-sdk contract.

Rules in this check module:

* **D001 UnpinnedSdkDependency** — the app's ``[project.dependencies]`` array
  must declare ``atlan-application-sdk`` with a *bounded* version specifier
  (a lower bound *and* an upper bound, or a compatible-release ``~=``).
* **D002 RedeclaredSdkManagedDependency** — packages already pinned by the
  installed ``atlan-application-sdk`` distribution must not be redeclared in
  the app's ``[project.dependencies]`` or any
  ``[project.optional-dependencies.*]`` array.
* **D004 RedeclaredSdkManagedDependencyInGroups** — same redeclaration check,
  extended to PEP 735 ``[dependency-groups]`` (D002's coverage gap).
* **D005 UnknownSdkExtra** — an ``atlan-application-sdk[extra]`` reference must
  name an extra the SDK actually publishes (uv silently drops unknown extras).
* **D006 IncompatibleRequiresPython** — the app's ``[project].requires-python``
  lower bound must not be below the SDK's minimum supported Python.
* **D007 NonStandardBuildBackend** — ``[build-system].build-backend`` must be
  Hatchling.
* **D008 WeakenedTypeChecking** — ``[tool.pyright].typeCheckingMode`` must not
  be weaker than the SDK baseline ``standard``.
* **D009 RemoteDaprComponentFetch** — no ``[tool.poe.tasks.*]`` entry may fetch
  Dapr component YAMLs from ``raw.githubusercontent.com`` or the GitHub
  contents API for ``atlanhq/application-sdk``; the installed SDK wheel
  bundles them at ``application_sdk/components/``.

D004/D005 are metadata-based (need the SDK importable) like D002; D006/D007/D008/D009
are pure-text.

Self-check exemption: any pyproject whose ``[project].name`` starts with
``atlan-application-sdk`` is skipped entirely (the SDK and its sibling packages
are *publishers* of the contract, not apps subject to it).
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from conformance.suite.checks._ast_common import discover as _discover_sources
from conformance.suite.checks._ast_common import is_sdk_package_name, make_cli_main
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

SDK_PACKAGE = "atlan-application-sdk"

# The canonical build backend for Atlan apps (D007).
HATCHLING_BACKEND = "hatchling.build"

# pyright type-checking modes weaker than the SDK baseline ``standard`` (D008).
PYRIGHT_WEAK_MODES = frozenset({"off", "basic"})

# Matches a poe task fetching Dapr component YAMLs straight from GitHub
# (raw.githubusercontent.com or the contents API) for atlanhq/application-sdk,
# instead of copying them from the installed wheel, which bundles them at
# application_sdk/components/ (D009). Unauthenticated GitHub requests hit rate
# limits under CI concurrency, and a hardcoded ref drifts from whatever SDK
# version is actually locked in the app's uv.lock.
_REMOTE_COMPONENT_FETCH_RE = re.compile(
    r"(?:raw\.githubusercontent\.com|api\.github\.com)[^\s\"'\\]*/atlanhq/application-sdk\b",
    re.IGNORECASE,
)

# The SDK's own ``[project].requires-python`` lower bound, as ``(major, minor)``.
# Hardcoded (not read from metadata) so D006 stays a pure-text check that works
# under the isolated ``uvx`` CI leg without the SDK being importable. Kept honest
# by ``test_d006_sdk_python_floor_matches_sdk_pyproject``, which re-reads the
# SDK's real pyproject.toml and fails if this constant drifts.
SDK_PYTHON_FLOOR: tuple[int, int] = (3, 11)

# Inline suppression directive; identical regex to the E-series so the docs
# remain consistent (``# conformance: ignore[D00x] reason``). TOML uses ``#``
# for comments, so this slots in naturally.
_SUPPRESS_RE = re.compile(
    r"^#\s*conformance\s*:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)",
    re.IGNORECASE,
)

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


# ---------------------------------------------------------------------------
# Public dataclass for parsed app dependencies (used by tests)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# TOML parsing with line tracking
# ---------------------------------------------------------------------------


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
    # A string entry inside an array: ``"requirement-spec",`` (optional comma,
    # optional inline ``# comment``).
    entry_re = re.compile(
        r'^(?P<lead>\s*)"(?P<value>(?:[^"\\]|\\.)*)"\s*,?\s*(?:#.*)?$'
    )

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
                for sm in re.finditer(r'"([^"\\]*(?:\\.[^"\\]*)*)"', body):
                    entry = sm.group(1)
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
                    for sm in re.finditer(r'"([^"\\]*(?:\\.[^"\\]*)*)"', body):
                        entry = sm.group(1)
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
        value = em.group("value")
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
    entry_re = re.compile(
        r'^(?P<lead>\s*)"(?P<value>(?:[^"\\]|\\.)*)"\s*,?\s*(?:#.*)?$'
    )

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
                for sm in re.finditer(
                    r'"([^"\\]*(?:\\.[^"\\]*)*)"', inline.group("body")
                ):
                    parsed = _parse_requirement(sm.group(1))
                    if parsed is None:
                        continue
                    yield _DepEntry(
                        raw=sm.group(1),
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
        parsed = _parse_requirement(em.group("value"))
        if parsed is None:
            continue
        yield _DepEntry(
            raw=em.group("value"),
            name=parsed[0],
            line=lineno,
            column=len(em.group("lead")) + 2,
            array_path=f"dependency-groups.{group_name}",
        )


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def _parse_suppressions(text: str) -> dict[int, tuple[frozenset[str] | None, str]]:
    """Return ``{lineno: (rule_ids_or_None, justification)}`` for every
    ``# conformance: ignore[...]`` directive in *text*.

    ``rule_ids_or_None`` is ``None`` for a rule-id-less directive (matches any
    rule on that line). Framed from the violation's side to match
    ``_is_suppressed``: a directive at line N suppresses findings on lines N and
    N+1 (its own line and the one immediately below — the E-series convention,
    so users only have to learn one form).
    """
    out: dict[int, tuple[frozenset[str] | None, str]] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.lstrip()
        # A suppression must be on a comment-only line *or* trail an entry as
        # ``"req-spec",  # conformance: ignore[D002] reason``.
        idx = stripped.find("#")
        if idx == -1:
            continue
        comment = stripped[idx:]
        m = _SUPPRESS_RE.match(comment)
        if m is None:
            continue
        ids_blob = (m.group(1) or "").strip()
        ids: frozenset[str] | None = (
            None
            if not ids_blob
            else frozenset(s.strip() for s in ids_blob.split(",") if s.strip())
        )
        out[lineno] = (ids, m.group(2).strip())
    return out


def _is_suppressed(
    suppressions: dict[int, tuple[frozenset[str] | None, str]],
    rule_id: str,
    line: int,
) -> tuple[bool, str | None]:
    """Return ``(suppressed, justification)`` for a finding at *line*.

    A directive on the same line or on the line immediately above suppresses
    the finding when its rule-id list is empty *or* contains *rule_id*.
    """
    for cand in (line, line - 1):
        if cand not in suppressions:
            continue
        ids, just = suppressions[cand]
        if ids is None or rule_id in ids:
            return True, just
    return False, None


# ---------------------------------------------------------------------------
# SDK-managed-deps lookup
# ---------------------------------------------------------------------------


def _sdk_managed_packages() -> set[str] | None:
    """Return the set of normalised package names the SDK pins as core deps.

    Reads ``importlib.metadata.requires('atlan-application-sdk')`` and filters
    out optional-extra entries (those with ``extra == ...`` markers). Returns
    ``None`` if the SDK is not importable in the current environment, in which
    case D002 is skipped silently.
    """
    try:
        reqs = importlib_metadata.requires(SDK_PACKAGE)
    except importlib_metadata.PackageNotFoundError:
        return None
    if reqs is None:
        return set()

    out: set[str] = set()
    for req in reqs:
        # ``Requires-Dist: foo>=1; extra == "sql"`` — skip optional-extra deps;
        # an app pulls those in via ``atlan-application-sdk[sql]`` and they are
        # not part of the *core* contract enforced here.
        if "extra ==" in req or 'extra=="' in req or 'extra =="' in req:
            continue
        parsed = _parse_requirement(req)
        if parsed is None:
            continue
        name, _spec = parsed
        out.add(name)
    # Defensive: never claim to manage the SDK itself.
    out.discard(_normalise_name(SDK_PACKAGE))
    return out


def _sdk_published_extras() -> set[str] | None:
    """Return the set of normalised extra names the SDK publishes (D005).

    Reads the ``Provides-Extra`` metadata of ``atlan-application-sdk``.  Names
    are PEP 503-normalised so an app's ``[iam_auth]`` matches the published
    ``iam-auth``.  Returns ``None`` if the SDK is not importable, in which case
    D005 is skipped silently (same posture as D002/D004).
    """
    try:
        meta = importlib_metadata.metadata(SDK_PACKAGE)
    except importlib_metadata.PackageNotFoundError:
        return None
    return {_normalise_name(e) for e in (meta.get_all("Provides-Extra") or [])}


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------


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


def scan_text(
    text: str,
    file: str,
    *,
    sdk_managed_packages: Iterable[str] | None = None,
    sdk_published_extras: Iterable[str] | None = None,
) -> list[Finding]:
    """Scan a pyproject.toml text and return D-series findings.

    Parameters
    ----------
    text:
        Raw pyproject.toml content.
    file:
        Repo-root-relative URI for findings.
    sdk_managed_packages:
        Override for the SDK's managed-deps set (test injection). When
        ``None``, looked up via ``importlib.metadata.requires``; when the SDK
        is not importable, D002/D004 are skipped silently.
    sdk_published_extras:
        Override for the SDK's published extras set (test injection). When
        ``None``, looked up via ``importlib.metadata`` ``Provides-Extra``; when
        the SDK is not importable, D005 is skipped silently.
    """
    data = _safe_load(text)
    if data is None:
        return []

    name = _project_name(text, data=data)
    if _is_self_check(name):
        return []

    findings: list[Finding] = []
    suppressions = _parse_suppressions(text)

    entries = list(_iter_dep_entries(text, data=data))

    # ── D001 ──────────────────────────────────────────────────────────────
    sdk_norm = _normalise_name(SDK_PACKAGE)
    project_deps = [e for e in entries if e.array_path == "project.dependencies"]
    sdk_entries = [e for e in project_deps if e.name == sdk_norm]

    if not sdk_entries:
        # Position the finding at the [project] table header (or line 1 if
        # we cannot find it) so reviewers know where the missing dep should go.
        anchor_line = 1
        for ln, line in enumerate(text.splitlines(), start=1):
            if line.strip() == "[project]":
                anchor_line = ln
                break
        findings.append(
            _make_finding(
                rule_id=RULE_D001,
                file=file,
                line=anchor_line,
                column=1,
                message=(
                    f"App pyproject.toml does not declare '{SDK_PACKAGE}' in "
                    f"[project.dependencies]. Every app must depend on the "
                    f"SDK with a bounded version specifier so fleet-wide "
                    f"upgrades remain reviewed and reproducible."
                ),
                suppressions=suppressions,
            )
        )
    else:
        for entry in sdk_entries:
            parsed = _parse_requirement(entry.raw)
            spec = parsed[1] if parsed else ""
            if not _is_bounded_specifier(spec):
                findings.append(
                    _make_finding(
                        rule_id=RULE_D001,
                        file=file,
                        line=entry.line,
                        column=entry.column,
                        message=(
                            f"'{SDK_PACKAGE}' is declared without a bounded "
                            f"version specifier (got '{entry.raw}'). Pin both "
                            f"a lower and an upper bound (e.g. "
                            f"'>=3.17.2,<4.0.0' or '~=3.17') so an automated "
                            f"SDK upgrade cannot pull in a future major "
                            f"version unreviewed."
                        ),
                        suppressions=suppressions,
                    )
                )

    # ── D006 ──────────────────────────────────────────────────────────────
    # Pure-text: runs regardless of whether the SDK metadata is importable, so
    # it must precede the D002 early-return below.
    py_bound = _requires_python_lower_bound(text, data=data)
    if py_bound is not None:
        (app_major, app_minor), app_op, py_line = py_bound
        if (app_major, app_minor) < SDK_PYTHON_FLOOR:
            sdk_major, sdk_minor = SDK_PYTHON_FLOOR
            findings.append(
                _make_finding(
                    rule_id=RULE_D006,
                    file=file,
                    line=py_line,
                    column=1,
                    message=(
                        f"App requires-python lower bound "
                        f"'{app_op}{app_major}.{app_minor}' is below the SDK's "
                        f"minimum supported Python ('>={sdk_major}.{sdk_minor}'). "
                        f"The app claims to support a Python the SDK does not; "
                        f"raise the lower bound to '>={sdk_major}.{sdk_minor}'."
                    ),
                    suppressions=suppressions,
                )
            )

    # ── D007 (pure-text) ────────────────────────────────────────────────────
    backend = _build_backend(text, data=data)
    if backend is not None and backend[0] != HATCHLING_BACKEND:
        findings.append(
            _make_finding(
                rule_id=RULE_D007,
                file=file,
                line=backend[1],
                column=1,
                message=(
                    f"Build backend '{backend[0]}' is non-standard for Atlan "
                    f"apps. Use Hatchling: set build-backend = "
                    f"'{HATCHLING_BACKEND}' and requires = ['hatchling'] in "
                    f"[build-system]."
                ),
                suppressions=suppressions,
            )
        )

    # ── D008 (pure-text) ────────────────────────────────────────────────────
    pyright = _pyright_mode(text, data=data)
    if pyright is not None and pyright[0].strip().lower() in PYRIGHT_WEAK_MODES:
        findings.append(
            _make_finding(
                rule_id=RULE_D008,
                file=file,
                line=pyright[1],
                column=1,
                message=(
                    f"pyright typeCheckingMode '{pyright[0]}' is weaker than "
                    f"the SDK baseline 'standard'. Raise it to at least "
                    f"'standard' so type regressions are caught in app CI."
                ),
                suppressions=suppressions,
            )
        )

    # ── D009 (pure-text) ────────────────────────────────────────────────────
    tasks = _poe_tasks(data)
    if tasks is not None:
        reported_lines: set[int] = set()
        for task_name, task_def in tasks.items():
            if not any(
                _REMOTE_COMPONENT_FETCH_RE.search(s) for s in _iter_strings(task_def)
            ):
                continue
            for ln, line in enumerate(text.splitlines(), start=1):
                if ln in reported_lines or not _REMOTE_COMPONENT_FETCH_RE.search(line):
                    continue
                reported_lines.add(ln)
                findings.append(
                    _make_finding(
                        rule_id=RULE_D009,
                        file=file,
                        line=ln,
                        column=1,
                        message=(
                            f"poe task '{task_name}' fetches Dapr component "
                            f"YAMLs from GitHub over the network instead of "
                            f"reading them from the installed "
                            f"'{SDK_PACKAGE}' wheel, which bundles them at "
                            f"application_sdk/components/. Unauthenticated "
                            f"GitHub requests hit rate limits under CI "
                            f"concurrency, and a hardcoded ref drifts from "
                            f"whatever SDK version is actually locked in "
                            f"uv.lock. Copy from the installed package "
                            f'instead, e.g. `python -c "import '
                            f"application_sdk, pathlib, shutil; "
                            f"shutil.copytree(pathlib.Path("
                            f"application_sdk.__file__).parent / "
                            f"'components', 'components', "
                            f'dirs_exist_ok=True)"`.'
                        ),
                        suppressions=suppressions,
                    )
                )

    # ── D002 / D004: redeclaration of SDK-managed core deps (metadata) ──────
    if sdk_managed_packages is None:
        managed = _sdk_managed_packages()
    else:
        managed = {_normalise_name(p) for p in sdk_managed_packages}

    group_entries = list(_iter_dependency_group_entries(text, data=data))

    if managed is not None:
        # D002 — [project.dependencies] + [project.optional-dependencies.*]
        for entry in entries:
            if entry.name == sdk_norm:
                continue  # the SDK itself is D001's concern
            if entry.name in managed:
                findings.append(
                    _make_finding(
                        rule_id=RULE_D002,
                        file=file,
                        line=entry.line,
                        column=entry.column,
                        message=(
                            f"'{entry.name}' is already pinned by "
                            f"'{SDK_PACKAGE}' and must not be redeclared in "
                            f"the app's [{entry.array_path}]. Remove the line; "
                            f"the SDK's contract will install it transitively."
                        ),
                        suppressions=suppressions,
                    )
                )
        # D004 — [dependency-groups.*] (dev/test groups; D002's coverage gap)
        for entry in group_entries:
            if entry.name == sdk_norm:
                continue
            if entry.name in managed:
                findings.append(
                    _make_finding(
                        rule_id=RULE_D004,
                        file=file,
                        line=entry.line,
                        column=entry.column,
                        message=(
                            f"'{entry.name}' is pinned by '{SDK_PACKAGE}' and "
                            f"redeclared in the app's [{entry.array_path}]. "
                            f"Dev/test groups that re-pin an SDK-managed "
                            f"package drift from the SDK's dev environment and "
                            f"must be touched on every SDK bump. Remove the "
                            f"line, or pull it in via 'atlan-application-sdk"
                            f"[tests]'."
                        ),
                        suppressions=suppressions,
                    )
                )

    # ── D005: invalid SDK extra reference (metadata) ────────────────────────
    if sdk_published_extras is None:
        published = _sdk_published_extras()
    else:
        published = {_normalise_name(e) for e in sdk_published_extras}

    if published is not None:
        for entry in entries + group_entries:
            if entry.name != sdk_norm:
                continue
            for extra in _sdk_extras_in(entry.raw):
                if _normalise_name(extra) not in published:
                    findings.append(
                        _make_finding(
                            rule_id=RULE_D005,
                            file=file,
                            line=entry.line,
                            column=entry.column,
                            message=(
                                f"'{SDK_PACKAGE}[{extra}]' in "
                                f"[{entry.array_path}] references an extra the "
                                f"SDK does not publish. uv silently drops an "
                                f"unknown extra, so its dependencies are never "
                                f"installed. Use a published extra or remove "
                                f"'{extra}'."
                            ),
                            suppressions=suppressions,
                        )
                    )

    return findings


def _make_finding(
    *,
    rule_id: str,
    file: str,
    line: int,
    column: int,
    message: str,
    suppressions: dict[int, tuple[frozenset[str] | None, str]],
) -> Finding:
    """Construct a Finding, marking it suppressed if a directive applies."""
    suppressed, justification = _is_suppressed(suppressions, rule_id, line)
    return Finding(
        rule_id=rule_id,
        file=file,
        line=line,
        column=column,
        message=message,
        snippet=None,
        suppressed=suppressed,
        suppression_justification=justification,
    )


def scan_path(
    path: Path,
    root: Path,
    *,
    sdk_managed_packages: Iterable[str] | None = None,
    sdk_published_extras: Iterable[str] | None = None,
) -> list[Finding]:
    """Scan a single pyproject.toml on disk."""
    text = path.read_text(encoding="utf-8")
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(
        text,
        str(rel),
        sdk_managed_packages=sdk_managed_packages,
        sdk_published_extras=sdk_published_extras,
    )


# ---------------------------------------------------------------------------
# D003 — unused dependency (cross-file: pyproject deps vs. source imports)
# ---------------------------------------------------------------------------


def _collect_top_level_imports(py_files: Iterable[Path]) -> set[str]:
    """Return the set of top-level module names imported across *py_files*.

    Only the first dotted component of each import target is kept (``import
    a.b.c`` and ``from a.b import x`` both contribute ``a``).  Relative imports
    (``from . import x``) are skipped — they target the app's own package, never
    a declared third-party distribution.
    """
    modules: set[str] = set()
    for path in py_files:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        try:
            # Parse from bytes so ``ast`` honours a PEP 263 coding cookie; a
            # legacy non-UTF-8 source (e.g. ``# -*- coding: latin-1 -*-``) is
            # decoded correctly instead of crashing on a UTF-8 decode.
            tree = ast.parse(raw)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    modules.add(node.module.split(".", 1)[0])
    return modules


def _dist_import_names(dist_name: str) -> set[str] | None:
    """Return the top-level import names a distribution provides, or ``None``.

    Resolved from installed package metadata: ``top_level.txt`` when present,
    plus top-level entries derived from the distribution's file list (RECORD).
    Returns ``None`` when the distribution is not importable in the current
    environment — the caller then *skips* the dependency (never flags it) and
    reports it as unanalysed, so a missing env can never produce a false
    "unused" finding.
    """
    try:
        dist = importlib_metadata.distribution(dist_name)
    except importlib_metadata.PackageNotFoundError:
        return None

    names: set[str] = set()
    top_level = dist.read_text("top_level.txt")
    if top_level:
        names.update(line.strip() for line in top_level.splitlines() if line.strip())

    # Derive top-levels from the installed file list as a fallback / supplement
    # (namespace packages and wheels without top_level.txt rely on this).  Each
    # candidate is gated on ``isidentifier`` so non-module RECORD entries — data
    # files (``share/…``), scripts installed via ``..`` / ``bin``, ``LICENSE`` —
    # never leak into the provided-names set.
    for file in dist.files or ():
        parts = file.parts
        if not parts:
            continue
        head = parts[0]
        if head.endswith((".dist-info", ".data", ".egg-info")):
            continue
        if len(parts) == 1:
            # A single top-level module file: ``foo.py`` -> ``foo``.
            if head.endswith(".py"):
                stem = head[:-3]
                if stem.isidentifier():
                    names.add(stem)
        elif head.isidentifier():
            # A package directory: ``foo/__init__.py`` -> ``foo``.
            names.add(head)

    return names


def _scan_unused_dependencies(
    dep_entries: list[_DepEntry],
    imported_modules: set[str],
    suppressions: dict[int, tuple[frozenset[str] | None, str]],
    file: str,
    *,
    dist_import_map: Mapping[str, set[str] | None],
) -> tuple[list[Finding], list[str]]:
    """Return (D003 findings, names of dependencies skipped as unresolvable).

    A dependency is flagged when the import names it provides are all absent
    from *imported_modules*.  A dependency whose ``dist_import_map`` value is
    ``None`` (not importable in this environment) is skipped and returned in the
    second list so the caller can surface it — never silently dropped.
    """
    findings: list[Finding] = []
    unresolved: list[str] = []

    # The caller filters out the SDK self-dependency when building dep_entries,
    # so every entry here is a third-party package eligible for the unused check.
    for entry in dep_entries:
        provided = dist_import_map.get(entry.name)
        if not provided:
            # None (not installed) or empty (no resolvable import names) -> we
            # cannot prove it is unused, so skip and report rather than flag.
            unresolved.append(entry.name)
            continue
        if provided & imported_modules:
            continue  # at least one provided module is imported -> used
        provided_list = ", ".join(sorted(provided))
        findings.append(
            _make_finding(
                rule_id=RULE_D003,
                file=file,
                line=entry.line,
                column=entry.column,
                message=(
                    f"'{entry.name}' is declared in [project.dependencies] but none "
                    f"of the modules it provides ({provided_list}) are imported "
                    f"anywhere in source. If it is unused, remove it. Otherwise it may "
                    f"be loaded dynamically (importlib), pulled in via an entry "
                    f"point/plugin, or required by a framework/server it is not "
                    f"directly imported by — verify before removing, or annotate the "
                    f"line with '# conformance: ignore[D003] <reason>'."
                ),
                suppressions=suppressions,
            )
        )

    return findings, unresolved


def scan_all(
    paths: list[Path],
    root: Path,
    *,
    dist_import_map: Mapping[str, set[str] | None] | None = None,
    imported_modules: set[str] | None = None,
) -> list[Finding]:
    """Run the full D-series over *paths*: per-file D001/D002 + cross-file D003.

    ``paths`` is the post-exclusion discovery list (the root ``pyproject.toml``
    plus the repo's Python sources — see :func:`discover`).

    D001/D002 run per ``pyproject.toml`` via :func:`scan_path` (the SDK and its
    sibling packages remain self-exempt).  D003 is computed here *outside* that
    self-check guard — it is ``scope=both`` and so applies to the SDK itself —
    by mapping each core dependency to the import names it provides and flagging
    any whose modules never appear in the collected source imports.

    Test seams: ``dist_import_map`` injects the dependency -> import-name map
    (default: resolved from installed metadata) and ``imported_modules`` injects
    the set of imported top-levels (default: parsed from the discovered sources).
    """
    pyprojects = [p for p in paths if p.name == "pyproject.toml"]
    py_files = [p for p in paths if p.suffix == ".py"]

    findings: list[Finding] = []
    for pyproject in pyprojects:
        findings.extend(scan_path(pyproject, root))

    # ── D003 ────────────────────────────────────────────────────────────────
    root_pyproject = root / "pyproject.toml"
    if not root_pyproject.is_file():
        return findings
    text = root_pyproject.read_text(encoding="utf-8")
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return findings

    dep_entries = [
        e
        for e in _iter_dep_entries(text)
        if e.array_path == "project.dependencies"
        and e.name != _normalise_name(SDK_PACKAGE)
    ]
    if not dep_entries:
        return findings

    if imported_modules is None:
        imported_modules = _collect_top_level_imports(py_files)
    if dist_import_map is None:
        dist_import_map = {e.name: _dist_import_names(e.name) for e in dep_entries}

    try:
        rel = root_pyproject.relative_to(root)
    except ValueError:
        rel = root_pyproject

    d003_findings, unresolved = _scan_unused_dependencies(
        dep_entries,
        imported_modules,
        _parse_suppressions(text),
        str(rel),
        dist_import_map=dist_import_map,
    )
    findings.extend(d003_findings)

    if unresolved:
        # No silent caps: a dependency we cannot resolve in this environment is
        # not analysed; surface it (to stderr, so SARIF on stdout stays clean)
        # rather than letting "0 D003 findings" imply full coverage.
        noun = "dependency" if len(unresolved) == 1 else "dependencies"
        print(
            f"conformance (D003): skipped {len(unresolved)} {noun} not importable "
            f"in this environment (unused status undetermined): "
            f"{', '.join(sorted(unresolved))}",
            file=sys.stderr,
        )

    return findings


def discover(root: Path) -> list[Path]:
    """Return the root ``pyproject.toml`` (if any) plus the repo's Python sources.

    D001/D002 need the ``pyproject.toml``; D003 additionally needs every source
    file's imports.  Source discovery reuses the shared AST walk, which already
    excludes tests, build dirs, virtualenvs, and dot-directories.
    """
    paths: list[Path] = []
    candidate = root / "pyproject.toml"
    if candidate.is_file():
        paths.append(candidate)
    paths.extend(_discover_sources(root))
    return paths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

main = make_cli_main(
    scan_all=scan_all,
    description="D-series: scan a repo against the application-sdk dependency contract.",
    discover=discover,
    default_scan_paths=(".",),
)
"""CLI entry point for the D-series check."""


if __name__ == "__main__":
    sys.exit(main())
