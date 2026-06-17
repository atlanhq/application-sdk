"""Generate per-series rule catalog documents from the typed Python source.

Usage
-----
Regenerate all docs (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-rule-docs

Check whether committed docs are up-to-date (CI gate):

    uv run atlan-application-sdk-conformance gen-rule-docs --check

Direct invocation:

    python -m conformance.tools.generate_rule_docs
    python -m conformance.tools.generate_rule_docs --check
    python -m conformance.tools.generate_rule_docs --outdir /tmp/rule-docs

Design
------
Each call reads the live Python rule definitions from ``conformance.suite.rules``,
renders one Markdown file per series, and either writes the files or
(with --check) compares them to the committed versions.  The output is
deterministic: same inputs → identical bytes, so ``--check`` is a
reliable staleness gate.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.rules import _ALL_SERIES, assert_registry_consistent
from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import EnforcementTier

# ---------------------------------------------------------------------------
# Per-series metadata (what can't be derived from the RuleDefinition model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesMeta:
    title: str
    """Human-readable title for the series."""
    prefix: str
    """Single-letter series prefix, e.g. ``"E"``."""
    source_module: str
    """Repo-relative path to the Python source, for the header comment."""
    output_filename: str
    """Filename under ``conformance/docs/rules/``."""
    checker: str
    """Short description of the checker that produces findings."""
    suppression_example: str
    """Example suppression directive for the series."""
    stability_note: str | None = None
    """Optional series-level prose rendered above the rule table (e.g. the
    rule-id stability contract).  RST double-backticks are converted to
    Markdown.  ``None`` renders nothing."""


# Rule-id stability contract — kept verbatim in sync with the module docstrings
# of rules/prescriptions.py and rules/optimizations.py so the policy is visible
# to consumers reading the rule catalog, not only to code readers.
_ID_STABILITY_NOTE = (
    "**Rule-id stability (non-migration policy):** P-ids and O-ids are a "
    "permanent public contract — each is exposed in the SARIF ``help_uri`` and "
    "referenced by inline ``# conformance: ignore[...]`` suppressions across the "
    "fleet.  An id therefore **never migrates and never changes**, even if a "
    "future domain series (S/B/T/A/…) later subsumes the same topic.  When a "
    "domain series takes over an area, the rule is retired in place (kept "
    "documented, no longer firing) and the new rule gets a fresh id — the "
    "original id is never reused or reassigned."
)


_SERIES_META: list[SeriesMeta] = [
    SeriesMeta(
        title="Error-Handling Rules (E-series)",
        prefix="E",
        source_module="conformance/suite/rules/error_handling.py",
        output_filename="error-handling.md",
        checker="`suite.checks.error_handling` (AST-based)",
        suppression_example="# conformance: ignore[E012] intentional: stdlib interop",
    ),
    SeriesMeta(
        title="Logging Rules (L-series)",
        prefix="L",
        source_module="conformance/suite/rules/logging.py",
        output_filename="logging.md",
        checker="`suite.checks.logging` (AST-based, not yet fully implemented)",
        suppression_example="# conformance: ignore[L001] intentional: dynamic message",
    ),
    SeriesMeta(
        title="CI/Workflow Supply-Chain Rules (C-series)",
        prefix="C",
        source_module="conformance/suite/rules/ci.py",
        output_filename="ci.md",
        checker=("`suite.checks.actions_pinning` and related workflow checks (static)"),
        suppression_example="# conformance: ignore[C001] intentional: org-internal action",
    ),
    SeriesMeta(
        title="Prescription Rules (P-series)",
        prefix="P",
        source_module="conformance/suite/rules/prescriptions.py",
        output_filename="prescriptions.md",
        checker="`suite.checks.prescriptions` (AST-based)",
        suppression_example="# conformance: ignore[P001] intentional: generic cleanup payload",
        stability_note=_ID_STABILITY_NOTE,
    ),
    SeriesMeta(
        title="Optimisation / Recommendation Rules (O-series)",
        prefix="O",
        source_module="conformance/suite/rules/optimizations.py",
        output_filename="optimizations.md",
        checker="`suite.checks.optimizations` (AST-based)",
        suppression_example="# conformance: ignore[O001] intentional: stdlib json required here",
        stability_note=_ID_STABILITY_NOTE,
    ),
]


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_AUTOGEN_BANNER = """\
<!-- AUTO-GENERATED — do not edit this file directly.
     Source of truth: {source_module}
     To regenerate:  uv run atlan-application-sdk-conformance gen-rule-docs
     To check CI staleness: uv run atlan-application-sdk-conformance gen-rule-docs --check -->
"""


def _rst_to_md(text: str) -> str:
    """Convert RST-style double-backtick literals to Markdown single-backtick code spans."""
    return re.sub(r"``([^`]+)``", r"`\1`", text)


def _tier_badge(tier: EnforcementTier) -> str:
    return f"`{tier.value}`"


def _bool_icon(value: bool) -> str:
    return "yes" if value else "—"


def _rule_anchor(rule: RuleDefinition) -> str:
    return rule.id.lower()


def _render_series(meta: SeriesMeta, rules: list[RuleDefinition]) -> str:
    """Return the full Markdown content for one rule series."""
    lines: list[str] = []

    # Header comment
    lines.append(_AUTOGEN_BANNER.format(source_module=meta.source_module))

    # Title
    lines.append(f"# {meta.title}")
    lines.append("")

    # Summary line
    count = len(rules)
    noun = "rule" if count == 1 else "rules"
    lines.append(f"**{count} {noun}** · Checker: {meta.checker}")
    lines.append("")
    lines.append(
        "Suppress a finding on the violating line or the line directly above it:"
    )
    lines.append("")
    lines.append(f"```python\n{meta.suppression_example}\n```")
    lines.append("")

    # Optional series-level prose (e.g. the rule-id stability contract).
    if meta.stability_note:
        wrapped = textwrap.fill(
            _rst_to_md(meta.stability_note),
            width=88,
            break_long_words=False,
            break_on_hyphens=False,
        )
        lines.append(wrapped)
        lines.append("")

    # Summary table
    lines.append("| ID | Name | Tier | Category | Autofixable | Since |")
    lines.append("|---|---|---|---|---|---|")
    for rule in rules:
        anchor = _rule_anchor(rule)
        since = rule.since or "—"
        lines.append(
            f"| [{rule.id}](#{anchor}) | `{rule.name}` | {_tier_badge(rule.tier)}"
            f" | `{rule.category}` | {_bool_icon(rule.autofixable)} | {since} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")

    # Per-rule sections
    for rule in rules:
        anchor = _rule_anchor(rule)
        since = rule.since or "—"
        autofixable = _bool_icon(rule.autofixable)

        lines.append(f"## {rule.id} — `{rule.name}` {{#{anchor}}}")
        lines.append("")

        # Metadata row
        lines.append(
            f"**Tier:** {_tier_badge(rule.tier)} · "
            f"**Category:** `{rule.category}` · "
            f"**Autofixable:** {autofixable} · "
            f"**Since:** {since}"
        )
        lines.append("")

        # Short description as a blockquote
        if rule.short_description:
            lines.append(f"> {_rst_to_md(rule.short_description)}")
            lines.append("")

        # Full description — convert RST backticks, preserve paragraph breaks
        if rule.full_description:
            desc = _rst_to_md(rule.full_description.strip())
            # Rewrap each paragraph individually to 88 chars for clean diff
            paragraphs = re.split(r"\n{2,}", desc)
            for para in paragraphs:
                wrapped = textwrap.fill(
                    para, width=88, break_long_words=False, break_on_hyphens=False
                )
                lines.append(wrapped)
                lines.append("")

        lines.append("---")
        lines.append("")

    # Remove trailing blank line before EOF
    while lines and lines[-1] == "":
        lines.pop()
    lines.append("")  # single trailing newline

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Build per-series rule lists from the ALL_SERIES tuple
# ---------------------------------------------------------------------------


def _group_by_prefix(
    all_series: tuple[tuple[RuleDefinition, ...], ...],
) -> dict[str, list[RuleDefinition]]:
    """Group all rules by their series prefix letter (first char of ID)."""
    grouped: dict[str, list[RuleDefinition]] = {}
    for series in all_series:
        for rule in series:
            prefix = rule.id[0]
            grouped.setdefault(prefix, []).append(rule)
    # Sort each group by numeric part of the ID
    for prefix in grouped:
        grouped[prefix].sort(key=lambda r: int(r.id[1:]))
    return grouped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate per-series rule catalog Markdown from Python source.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path(__file__).parent.parent / "docs" / "rules",
        help="Directory to write generated Markdown files (default: conformance/docs/rules/)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Verify committed files match generated output. "
            "Exits 1 if any file is stale or missing."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    outdir: Path = args.outdir
    check_mode: bool = args.check

    if not check_mode:
        outdir.mkdir(parents=True, exist_ok=True)

    grouped = _group_by_prefix(_ALL_SERIES)

    # Invariant: the doc-metadata table must describe exactly the rule series in
    # the catalog (orphan SeriesMeta → empty doc; undocumented series → silent
    # gap).  Shared with the runner via assert_registry_consistent.
    assert_registry_consistent(meta_series=frozenset(m.prefix for m in _SERIES_META))

    stale: list[str] = []

    for meta in _SERIES_META:
        rules = grouped.get(meta.prefix, [])
        if not rules:
            continue

        content = _render_series(meta, rules)
        target = outdir / meta.output_filename

        if check_mode:
            if not target.exists():
                print(f"MISSING: {target}", file=sys.stderr)
                stale.append(str(target))
            else:
                on_disk = target.read_text(encoding="utf-8")
                if on_disk != content:
                    print(f"STALE: {target}", file=sys.stderr)
                    stale.append(str(target))
        else:
            target.write_text(content, encoding="utf-8")
            print(f"Wrote {target}")

    if check_mode:
        if stale:
            print(
                f"\n{len(stale)} file(s) are stale or missing. "
                "Run `uv run poe generate-rule-docs` to update.",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            print("All rule catalog docs are up-to-date.")


if __name__ == "__main__":
    main()
