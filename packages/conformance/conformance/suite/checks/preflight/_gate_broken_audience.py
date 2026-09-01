"""P041 GateBrokenCategoryUserAudience (CONNECT-812 PF-29).

Flags a typed error leaf whose ``category`` is one the preflight gate treats as
*plumbing* while its ``audience`` blames the ``USER``.

``_GATE_BROKEN_CATEGORIES`` (``preflight_gate.py``) exists so a gate failure in
one of those categories fails **open** — the gate could not form a verdict, so
the run proceeds rather than blaming the source. A ``USER`` audience on the same
leaf says the opposite: that the customer caused it and the customer must fix
it. One of the two is wrong by construction, and the taxonomy already answers
which: eight of the SDK's nine gate-broken leaves resolve to ``PLATFORM`` or
``APP_OWNER``.

The rule compares two constants the SDK already publishes, so it needs no
semantic judgement about whether a *message* matches a *category* — the part
that makes category-correctness rules hard in general.

Only an ``audience`` **declared in the class body** is flagged. An inherited
``USER`` is deliberately not resolved across the SDK boundary: once the leaf
itself is correct, inheriting from it is the right answer, and modelling SDK
internals from an app repo is exactly the false-positive surface the P series
avoids.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry, _class_defs

_P041 = "P041"

# Mirrors application_sdk/execution/_temporal/preflight_gate.py::_GATE_BROKEN_CATEGORIES.
_GATE_BROKEN_CATEGORIES: frozenset[str] = frozenset(
    {
        "DEPENDENCY_UNAVAILABLE",
        "RATE_LIMITED",
        "RESOURCE_EXHAUSTED",
        "CANCELLED",
    }
)

# SDK leaves that carry a gate-broken category, so an app subclass inherits one
# without redeclaring it (P002 requires subclasses to inherit ``category``).
_GATE_BROKEN_LEAVES: frozenset[str] = frozenset(
    {
        "CancelledError",
        "RateLimitedError",
        "DependencyUnavailableError",
        "ResourceExhaustedError",
        "ColdStartRaceError",
        "DaprSidecarUnreachableError",
        "DiskFullError",
        "ObjectStoreDownloadError",
        "ObjectStoreReadError",
    }
)


def _class_body_assignment(cls: ast.ClassDef, field: str) -> ast.stmt | None:
    """Return the class-body statement binding *field*, or ``None``.

    Matches ``field: ClassVar[...] = <value>`` and ``field = <value>``.
    Annotation-only forms bind no value and are not considered.
    """
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign):
            if (
                isinstance(stmt.target, ast.Name)
                and stmt.target.id == field
                and stmt.value is not None
            ):
                return stmt
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == field:
                    return stmt
    return None


def _enum_member(stmt: ast.stmt) -> str | None:
    """Return the member name of an ``Enum.MEMBER`` assignment value."""
    value = stmt.value if isinstance(stmt, (ast.AnnAssign, ast.Assign)) else None
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def _base_names(cls: ast.ClassDef, aliases: dict[str, str]) -> list[str]:
    names: list[str] = []
    for base in cls.bases:
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
        if name:
            names.append(aliases.get(name, name))
    return names


def _carries_gate_broken_category(
    cls: ast.ClassDef, aliases: dict[str, str], derived: frozenset[str]
) -> bool:
    """True if *cls* declares a gate-broken category or inherits one."""
    declared = _class_body_assignment(cls, "category")
    if declared is not None:
        return _enum_member(declared) in _GATE_BROKEN_CATEGORIES
    return any(b in derived for b in _base_names(cls, aliases))


def _gate_broken_derived(tree: ast.Module, aliases: dict[str, str]) -> frozenset[str]:
    """Names transitively derived from a gate-broken leaf, within this module.

    Iterates to a fixed point so a second-generation subclass is reached through
    an in-file intermediate. Mirrors ``_category_override._collect_apperror_subclasses``.
    """
    derived: set[str] = set(_GATE_BROKEN_LEAVES)
    changed = True
    while changed:
        changed = False
        for cls in _class_defs(tree):
            if cls.name in derived:
                continue
            # A subclass that redeclares its own category leaves the gate-broken
            # set — P002 flags the redeclaration; here it simply is not inherited.
            if _class_body_assignment(cls, "category") is not None:
                continue
            if any(b in derived for b in _base_names(cls, aliases)):
                derived.add(cls.name)
                changed = True
    return frozenset(derived)


def scan(reg: Registry) -> list[Finding]:
    findings: list[Finding] = []
    for src in reg.sources:
        derived = _gate_broken_derived(src.tree, src.aliases)
        for cls in _class_defs(src.tree):
            audience = _class_body_assignment(cls, "audience")
            if audience is None or _enum_member(audience) != "USER":
                continue
            if not _carries_gate_broken_category(cls, src.aliases, derived):
                continue
            findings.append(
                make_finding(
                    filename=src.rel,
                    rule_id=_P041,
                    node=audience,
                    message=(
                        f"{cls.name} carries a gate-broken failure category but "
                        "declares audience=USER. The preflight gate treats "
                        "DEPENDENCY_UNAVAILABLE / RATE_LIMITED / "
                        "RESOURCE_EXHAUSTED / CANCELLED as plumbing and fails "
                        "open on them, so the customer is not the locus — a "
                        "USER audience routes it to them anyway and skews every "
                        "ownership metric. Use Audience.APP_OWNER (the connector "
                        "team owns the concurrency and retry posture) or "
                        "Audience.PLATFORM where the dependency is Atlan-run."
                    ),
                    directives=src.directives,
                )
            )
    return findings
