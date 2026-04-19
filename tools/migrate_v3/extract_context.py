"""B1 + F4: Structured context extraction and difficulty scoring for v2→v3 migration.

Produces a machine-readable context summary of a connector directory, intended to
feed the LLM with structured information instead of raw source files.  Also
computes a migration difficulty estimate (F4).

Usage
-----
::

    from tools.migrate_v3.extract_context import extract_context, context_summary
    ctx = extract_context(Path("../atlan-anaplan-app"))
    print(context_summary(ctx))      # human-readable for LLM prompt
    import json
    print(json.dumps(ctx.to_dict())) # machine-readable JSON

CLI::

    uv run python -m tools.migrate_v3.extract_context <path>
    uv run python -m tools.migrate_v3.extract_context --json <path>
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import libcst as cst

from tools.migrate_v3.check_migration import _is_test_path
from tools.migrate_v3.contract_mapping import _CONTRACT_TABLE
from tools.migrate_v3.fingerprint import fingerprint_connector

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# All template method names from the contract table (A3/A4/A5 know these)
_TEMPLATE_METHODS: frozenset[str] = frozenset(m for (_, m) in _CONTRACT_TABLE)

# Handler base class names (exact match)
_HANDLER_BASES: frozenset[str] = frozenset(
    {"BaseHandler", "Handler", "DefaultHandler", "BaseSQLHandler", "HandlerInterface"}
)

# v3 App base classes — connector may already be migrated
_V3_APP_BASES: frozenset[str] = frozenset(
    {
        "App",
        "SqlMetadataExtractor",
        "IncrementalSqlMetadataExtractor",
        "SqlQueryExtractor",
    }
)

# Infrastructure usage regex patterns (text-based scan)
_INFRA_PATTERNS: dict[str, re.Pattern[str]] = {
    "secret_store": re.compile(
        r"\b(?:_secret_store|SecretStore|get_credentials|get_secret)\b"
    ),
    "state_store": re.compile(r"\b(?:_state_store|StateStore|self\._state)\b"),
    "object_store": re.compile(
        r"\b(?:_object_store|ObjectStore|upload_df|upload_file|download_file|list_objects)\b"
    ),
    "event_store": re.compile(r"\b(?:_event_store|EventStore|send_event)\b"),
    "dapr": re.compile(r"\bDaprClient\b"),
    "temporal_dynamic": re.compile(r"\bexecute_activity_method\b"),
}

# v2 Temporal decorator/pattern count (should reach 0 after Phase 1b codemods)
_TEMPORAL_V2_RE = re.compile(
    r"@(?:workflow\.defn|workflow\.run|activity\.defn|auto_heartbeater)\b"
)

# Complex entry point indicators (cause A7 to insert TODO instead of rewriting)
_COMPLEX_ENTRY_RE = re.compile(
    r"\b(?:start_worker|start_server|setup_server|include_router)\b"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MethodInfo:
    name: str
    is_template: bool
    has_args_kwargs: bool  # *args/**kwargs → needs typed-contract rewrite
    body_loc: int  # approximate statement count in method body


@dataclass
class ClassInfo:
    name: str
    bases: list[str]
    role: str  # "workflow" | "activities" | "handler" | "app" | "unknown"
    template_methods: list[MethodInfo] = field(default_factory=list)
    custom_methods: list[MethodInfo] = field(default_factory=list)
    has_load_method: bool = False


@dataclass
class InfraUsage:
    kind: str  # "secret_store" | "state_store" | "object_store" | etc.
    file: str
    count: int  # occurrences in that file


@dataclass
class ConnectorContext:
    connector_type: str
    confidence: float
    already_migrated: bool
    evidence: list[str]
    classes: list[ClassInfo]
    infra_usages: list[InfraUsage]
    has_complex_entry_point: bool
    temporal_v2_count: int  # v2 Temporal decorators still present
    total_loc: int
    difficulty: str  # "simple" | "moderate" | "complex"
    difficulty_score: float  # 0.0–1.0

    def to_dict(self) -> dict:  # type: ignore[return]
        return asdict(self)


# ---------------------------------------------------------------------------
# libcst class/method extraction
# ---------------------------------------------------------------------------


def _extract_base_name(expr: cst.BaseExpression) -> str | None:
    if isinstance(expr, cst.Name):
        return expr.value
    if isinstance(expr, cst.Attribute) and isinstance(expr.attr, cst.Name):
        return expr.attr.value  # module.ClassName → ClassName
    return None


def _determine_role(bases: list[str]) -> str:
    for base in bases:
        if base in _HANDLER_BASES or base.endswith("Handler"):
            return "handler"
        if base in _V3_APP_BASES or base.endswith(("Extractor", "App")):
            return "app"
        if "Workflow" in base:
            return "workflow"
        if "Activities" in base or base.endswith("ActivitiesInterface"):
            return "activities"
    return "unknown"


def _has_args_kwargs(params: cst.Parameters) -> bool:
    has_vararg = params.star_arg is not None and isinstance(params.star_arg, cst.Param)
    return has_vararg or params.star_kwarg is not None


def _count_loc(body: cst.BaseSuite) -> int:
    if isinstance(body, cst.IndentedBlock):
        return len(list(body.body))
    return 1


class _ClassExtractor(cst.CSTTransformer):
    """Read-only transformer that extracts class/method inventory from one file."""

    def __init__(self) -> None:
        super().__init__()
        self.classes: list[ClassInfo] = []
        # Stack of (ClassInfo, fn_depth_at_class_entry)
        self._class_stack: list[tuple[ClassInfo, int]] = []
        self._function_depth: int = 0

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        bases = [
            b for arg in node.bases if (b := _extract_base_name(arg.value)) is not None
        ]
        role = _determine_role(bases)
        cls = ClassInfo(name=node.name.value, bases=bases, role=role)
        self._class_stack.append((cls, self._function_depth))
        return True

    def leave_ClassDef(
        self,
        original_node: cst.ClassDef,
        updated_node: cst.ClassDef,
    ) -> cst.ClassDef:
        cls, _ = self._class_stack.pop()
        self.classes.append(cls)
        return updated_node

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self._function_depth += 1
        if self._class_stack:
            cls, entry_depth = self._class_stack[-1]
            # Only process direct methods of the innermost class (not nested functions)
            if self._function_depth == entry_depth + 1:
                method_name = node.name.value
                method = MethodInfo(
                    name=method_name,
                    is_template=method_name in _TEMPLATE_METHODS,
                    has_args_kwargs=_has_args_kwargs(node.params),
                    body_loc=_count_loc(node.body),
                )
                if method_name == "load":
                    cls.has_load_method = True
                elif method.is_template:
                    cls.template_methods.append(method)
                else:
                    cls.custom_methods.append(method)
        return True

    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef:
        self._function_depth -= 1
        return updated_node


# ---------------------------------------------------------------------------
# Infrastructure scanning
# ---------------------------------------------------------------------------


def _scan_infra(source: str, file_rel: str) -> list[InfraUsage]:
    return [
        InfraUsage(kind=kind, file=file_rel, count=len(matches))
        for kind, pattern in _INFRA_PATTERNS.items()
        if (matches := pattern.findall(source))
    ]


# ---------------------------------------------------------------------------
# Difficulty scoring (F4)
# ---------------------------------------------------------------------------


def _compute_difficulty(
    classes: list[ClassInfo],
    infra_usages: list[InfraUsage],
    has_complex_entry_point: bool,
    temporal_v2_count: int,
) -> tuple[str, float]:
    """Return ``(difficulty_label, score)`` where score ∈ [0, 1]."""
    score = 0.0

    for cls in classes:
        # Each custom method needs LLM attention (capped at 3 per class)
        score += 0.1 * min(len(cls.custom_methods), 3)
        # Handler methods with *args/**kwargs need typed-contract rewrite
        for m in cls.template_methods:
            if m.has_args_kwargs:
                score += 0.1

    if has_complex_entry_point:
        score += 0.2

    # Each distinct infrastructure type (excluding temporal_dynamic, counted separately)
    infra_kinds = {u.kind for u in infra_usages if u.kind != "temporal_dynamic"}
    score += 0.1 * min(len(infra_kinds), 3)

    if temporal_v2_count > 0:
        score += 0.15

    score = min(score, 1.0)
    label = "simple" if score < 0.3 else "moderate" if score < 0.6 else "complex"
    return label, round(score, 2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_context(root: Path) -> ConnectorContext:
    """Scan *root* and return a structured :class:`ConnectorContext`.

    Test files are excluded from the analysis.
    """
    fp = fingerprint_connector(root)
    py_files = sorted(root.rglob("*.py")) if root.is_dir() else [root]
    scan_root = root if root.is_dir() else root.parent

    all_classes: list[ClassInfo] = []
    all_infra: list[InfraUsage] = []
    total_loc = 0
    temporal_v2_count = 0
    has_complex_entry_point = False

    for path in py_files:
        if _is_test_path(path, root=scan_root):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue

        try:
            rel = str(path.relative_to(scan_root))
        except ValueError:
            rel = path.name

        # Non-empty, non-comment lines
        total_loc += sum(
            1
            for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

        # Class/method inventory via libcst
        try:
            extractor = _ClassExtractor()
            cst.parse_module(source).visit(extractor)
            all_classes.extend(extractor.classes)
        except cst.ParserSyntaxError:
            pass

        # Infrastructure patterns (text-based)
        all_infra.extend(_scan_infra(source, rel))

        # v2 Temporal decorators still present
        temporal_v2_count += len(_TEMPORAL_V2_RE.findall(source))

        # Complex entry point check
        if not has_complex_entry_point and "BaseApplication" in source:
            has_complex_entry_point = bool(_COMPLEX_ENTRY_RE.search(source))

    difficulty, score = _compute_difficulty(
        all_classes, all_infra, has_complex_entry_point, temporal_v2_count
    )

    return ConnectorContext(
        connector_type=fp.connector_type,
        confidence=fp.confidence,
        already_migrated=fp.already_migrated,
        evidence=fp.evidence,
        classes=all_classes,
        infra_usages=all_infra,
        has_complex_entry_point=has_complex_entry_point,
        temporal_v2_count=temporal_v2_count,
        total_loc=total_loc,
        difficulty=difficulty,
        difficulty_score=score,
    )


def context_summary(ctx: ConnectorContext) -> str:
    """Return a human-readable summary suitable for inclusion in an LLM prompt."""
    lines: list[str] = []
    lines.append(
        f"Connector Type: {ctx.connector_type} (confidence: {ctx.confidence:.1f})"
    )
    if ctx.already_migrated:
        lines.append(
            "NOTE: Already migrated to v3 — verify before re-running codemods."
        )
    lines.append(
        f"Migration Difficulty: {ctx.difficulty} (score: {ctx.difficulty_score:.2f})"
    )
    lines.append(f"Total LOC (non-comment): {ctx.total_loc}")
    lines.append("")

    if ctx.classes:
        lines.append("Classes to migrate:")
        for cls in ctx.classes:
            base_str = f" (inherits: {', '.join(cls.bases)})" if cls.bases else ""
            lines.append(f"  {cls.name}{base_str}")
            lines.append(f"    Role: {cls.role}")
            if cls.template_methods:
                names = ", ".join(m.name for m in cls.template_methods)
                lines.append(f"    Template methods: {names}")
                awkward = [m.name for m in cls.template_methods if m.has_args_kwargs]
                if awkward:
                    lines.append(
                        f"    WARNING *args/**kwargs (needs typed contract rewrite): {', '.join(awkward)}"
                    )
            if cls.custom_methods:
                names = ", ".join(m.name for m in cls.custom_methods)
                lines.append(f"    Custom methods: {names}")
            if cls.has_load_method:
                lines.append("    Has load(): yes (deleted by A5 codemod)")
        lines.append("")

    if ctx.infra_usages:
        lines.append("Infrastructure usage:")
        by_kind: dict[str, list[InfraUsage]] = {}
        for u in ctx.infra_usages:
            by_kind.setdefault(u.kind, []).append(u)
        for kind, usages in sorted(by_kind.items()):
            total = sum(u.count for u in usages)
            files = ", ".join(u.file for u in usages)
            lines.append(f"  {kind}: {total} occurrence(s) in {files}")
        lines.append("")

    flags: list[str] = []
    if ctx.temporal_v2_count > 0:
        flags.append(
            f"WARNING: {ctx.temporal_v2_count} v2 Temporal pattern(s) still present "
            "(dynamic dispatch or unprocessed decorators — manual rewrite needed)"
        )
    if ctx.has_complex_entry_point:
        flags.append(
            "WARNING: Complex entry point (TODO inserted by A7 — manual rewrite needed)"
        )
    lines.extend(flags)

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m tools.migrate_v3.extract_context",
        description="Extract structured migration context from a connector directory.",
    )
    parser.add_argument("path", help="Path to connector directory")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of human-readable summary",
    )
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.exists():
        print(f"error: path does not exist: {root}", file=sys.stderr)
        return 2

    ctx = extract_context(root)

    if args.json:
        print(json.dumps(ctx.to_dict(), indent=2))
    else:
        print(context_summary(ctx))

    return 0


if __name__ == "__main__":
    sys.exit(main())
