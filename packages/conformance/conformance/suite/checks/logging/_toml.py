"""L021 — MissingLoggingLintRules: pyproject.toml ruff config check."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-reuse-stubs,import-not-found]

from conformance.suite.schema.findings import Finding

# Ruff rules that complement the L-series AST checks.
# Mapping: rule_id -> human label (used in the finding message).
_REQUIRED_RULES: dict[str, str] = {
    "G001": "logging.warn() deprecated",
    "G003": "string concat in log message",
    "G004": "f-string in log message",
    "T201": "print() statement",
    "LOG009": "logging.warn() deprecated (LOG-series)",
}

# Packages exempt from this check (they publish the ruff config, not consume it).
_EXEMPT_NAME_PREFIX = "atlan-application-sdk"


def _is_covered(
    rule_id: str, selected: frozenset[str], ignored: frozenset[str]
) -> bool:
    """Return True if *rule_id* is effectively enabled in the ruff config.

    A rule is covered when:
    - ``"ALL"`` is in *selected* (and the rule is not ignored), or
    - the full rule ID is in *selected* (and not ignored), or
    - any prefix of the rule ID (e.g. ``"G"`` for ``"G004"``) is in *selected*
      and not in *ignored*.
    """
    if rule_id in ignored:
        return False
    if "ALL" in selected:
        return True
    if rule_id in selected:
        return True
    # Check progressively shorter prefixes: "LOG009" → "LOG00", "LOG0", "LOG", "LO", "L"
    for end in range(len(rule_id) - 1, 0, -1):
        prefix = rule_id[:end]
        if prefix in selected and prefix not in ignored:
            return True
    return False


def check_ruff_config(toml_path: Path, root: Path) -> list[Finding]:
    """Return L021 findings for a ``pyproject.toml`` missing required ruff rules.

    Returns an empty list when the file is unreadable, unparseable, is exempt
    (SDK packages), or all required rules are already covered.
    """
    try:
        text = toml_path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, Exception):
        return []

    # Self-check exemption: skip the SDK's own pyproject.toml.
    project_name: str = data.get("project", {}).get("name", "") or ""
    if project_name.startswith(_EXEMPT_NAME_PREFIX):
        return []

    # Read ruff lint config — support both [tool.ruff.lint] (ruff ≥ 0.2) and
    # the legacy [tool.ruff] flat layout (ruff < 0.2).
    ruff_cfg: dict = data.get("tool", {}).get("ruff", {})
    lint_cfg: dict = ruff_cfg.get(
        "lint", ruff_cfg
    )  # fallback to top-level ruff section

    def _to_frozenset(key: str) -> frozenset[str]:
        val = lint_cfg.get(key, [])
        return frozenset(str(v).strip() for v in val if v)

    selected = _to_frozenset("select") | _to_frozenset("extend-select")
    ignored = _to_frozenset("ignore") | _to_frozenset("extend-ignore")

    missing = [
        f"{rid} ({label})"
        for rid, label in _REQUIRED_RULES.items()
        if not _is_covered(rid, selected, ignored)
    ]

    if not missing:
        return []

    try:
        rel = toml_path.relative_to(root)
    except ValueError:
        rel = toml_path

    # Find the line of [tool.ruff.lint] or [tool.ruff] for a precise location.
    line = _find_ruff_section_line(text)

    return [
        Finding(
            rule_id="L021",
            file=str(rel),
            line=line,
            column=1,
            message=(
                "pyproject.toml ruff config is missing logging lint rules. "
                "Add to [tool.ruff.lint] select / extend-select: "
                + ", ".join(missing)
                + ". These complement the L-series AST checks with editor-time "
                "feedback. Selecting the category prefix (e.g. 'G', 'LOG') covers "
                "all rules in that group."
            ),
        )
    ]


def _find_ruff_section_line(text: str) -> int:
    """Return the 1-based line number of the ``[tool.ruff`` section, or 1."""
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("[tool.ruff"):
            return i
    return 1
