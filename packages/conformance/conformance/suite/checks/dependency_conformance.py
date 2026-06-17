"""D001/D002 — pyproject.toml conformance against the application-sdk contract.

Two rules in one check module:

* **D001 UnpinnedSdkDependency** — the app's ``[project.dependencies]`` array
  must declare ``atlan-application-sdk`` with a *bounded* version specifier
  (a lower bound *and* an upper bound, or a compatible-release ``~=``).
* **D002 RedeclaredSdkManagedDependency** — packages already pinned by the
  installed ``atlan-application-sdk`` distribution must not be redeclared in
  the app's ``[project.dependencies]`` or any
  ``[project.optional-dependencies.*]`` array.

Self-check exemption: any pyproject whose ``[project].name`` starts with
``atlan-application-sdk`` is skipped entirely (the SDK and its sibling packages
are *publishers* of the contract, not apps subject to it).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from conformance.suite.schema.findings import Finding, findings_to_report

SERIES = "D"
RULE_D001 = "D001"
RULE_D002 = "D002"

SDK_PACKAGE = "atlan-application-sdk"

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
        if clause.startswith("=="):
            # ``==X`` is exact — counts as both bounds.
            return True
        if clause.startswith("~="):
            # ``~=X.Y`` is PEP 440 compatible release: ``>=X.Y, <X+1``.
            return True
        if clause.startswith("==="):
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


def _iter_dep_entries(text: str) -> Iterator[_DepEntry]:
    """Yield every dep entry from ``[project.dependencies]`` and
    ``[project.optional-dependencies.*]`` arrays, preserving source lines.

    ``tomllib`` does not expose source positions, so we re-walk the raw text
    line-by-line using a small state machine that tracks the current array
    context. This is sufficient for the well-formed TOML produced by ``uv``
    and ``hatch`` and is robust against most hand-written variants.
    """
    array_path: str | None = None
    in_array = False
    extras_table: str | None = None  # current [project.optional-dependencies] subtable

    # Pre-scan tomllib for the structure (so we know the dotted key for an
    # ``[<table>] dependencies = [`` form).  But for *line numbers* we still
    # walk the raw text: tomllib loses them.
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
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
                        column=raw_line.index(sm.group(0)) + 2,
                        array_path="project.dependencies",
                    )
                continue
            # ``<extra-name> = [`` inside [project.optional-dependencies]
            if extras_table is not None:
                em = extras_open_re.match(raw_line)
                if em is not None:
                    extra_name = em.group(1)
                    array_path = (
                        f"{extras_table}.{extra_name}"
                        if extras_table == "project.optional-dependencies"
                        else f"{extras_table}.{extra_name}"
                    )
                    in_array = True
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


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def _parse_suppressions(text: str) -> dict[int, tuple[frozenset[str] | None, str]]:
    """Return ``{lineno: (rule_ids_or_None, justification)}`` for every
    ``# conformance: ignore[...]`` directive in *text*.

    ``rule_ids_or_None`` is ``None`` for a rule-id-less directive (matches any
    rule on that line). The directive applies to its own line *and* the line
    immediately below it (matches the E-series convention so users only have
    to learn one form).
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


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------


def _project_name(text: str) -> str | None:
    """Return ``[project].name`` if parseable, else ``None``."""
    try:
        data: Any = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project") if isinstance(data, dict) else None
    if not isinstance(project, dict):
        return None
    name = project.get("name")
    return str(name) if isinstance(name, str) else None


def _is_self_check(name: str | None) -> bool:
    """Return True for the SDK and its sibling packages (exempt from D-series)."""
    if name is None:
        return False
    return _normalise_name(name).startswith(_normalise_name(SDK_PACKAGE))


def scan_text(
    text: str,
    file: str,
    *,
    sdk_managed_packages: Iterable[str] | None = None,
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
        is not importable, D002 is skipped silently.
    """
    name = _project_name(text)
    if _is_self_check(name):
        return []

    findings: list[Finding] = []
    suppressions = _parse_suppressions(text)

    entries = list(_iter_dep_entries(text))

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

    # ── D002 ──────────────────────────────────────────────────────────────
    if sdk_managed_packages is None:
        managed = _sdk_managed_packages()
    else:
        managed = {_normalise_name(p) for p in sdk_managed_packages}

    if managed is None:
        # SDK not installed in this env — cannot determine managed set; skip.
        return findings

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
) -> list[Finding]:
    """Scan a single pyproject.toml on disk."""
    text = path.read_text(encoding="utf-8")
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel), sdk_managed_packages=sdk_managed_packages)


def discover(root: Path) -> list[Path]:
    """Return ``[root/pyproject.toml]`` if it exists, else ``[]``."""
    candidate = root / "pyproject.toml"
    return [candidate] if candidate.is_file() else []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the D-series check.

    Mirrors the C001/E-series CLIs: scans the given pyproject.toml(s),
    emits a SARIF report (stdout by default), exits 1 if any blocking
    finding is present.
    """
    parser = argparse.ArgumentParser(
        description=(
            "D001/D002: scan pyproject.toml against the application-sdk contract."
        ),
    )
    parser.add_argument(
        "scan_paths",
        nargs="*",
        default=["pyproject.toml"],
        metavar="PATH",
        help="pyproject.toml file(s) to scan (default: ./pyproject.toml)",
    )
    parser.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="Repo root for relative URI construction (default: .)",
    )
    parser.add_argument(
        "--sarif-output",
        metavar="FILE",
        help="Write SARIF report to FILE (default: stdout)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate emitted SARIF against the official schema",
    )
    parser.add_argument(
        "--tool-version",
        default="0.4.0",
        metavar="VERSION",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    findings: list[Finding] = []
    for raw in args.scan_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        if not p.is_file():
            continue
        findings.extend(scan_path(p, root))

    report = findings_to_report(findings, tool_version=args.tool_version)

    if args.validate:
        from conformance.suite.schema.validate import validate_sarif

        validate_sarif(report)

    payload = json.dumps(report.model_dump(by_alias=True, exclude_none=True), indent=2)
    if args.sarif_output:
        Path(args.sarif_output).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    return report.runs[0].invocations[0].exit_code  # type: ignore[return-value]


if __name__ == "__main__":
    sys.exit(main())
