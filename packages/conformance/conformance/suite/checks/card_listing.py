"""K022/K023 marketplace-card listing guards — description and icon.

K022 ``CardDescriptionMissing``: the generated ``atlan.yaml`` must supply a
card description, either as a top-level ``short_description`` or on at least
one entrypoint. The toolkit emits ``entrypoints[].description = e.description
?? shortDescription``, so an app that sets neither publishes a card whose text
falls back to the hand-curated Global Marketplace row — and renders blank when
that row is empty too.

K023 ``CardIconBlank``: ``icon_url`` must carry a value, at the app level and
on every entrypoint that declares the key. ``icon`` is a required field on
``App.pkl``, so a blank rendered ``icon_url`` means the value was overridden to
an empty string or the manifest was hand-written; either way the card renders
without a logo.

Both are APP-scoped and gated on the presence of a ``contract/`` directory —
the same "is this a pkl-contract-driven app repo?" signal the sibling K checks
(``release_contract``, ``generated_freshness``) use.

These are cross-artifact checks reading one fixed root-level file
(``atlan.yaml``), so they implement ``scan_all`` and a no-op ``scan_path``,
mirroring K011/K012/K014 (``release_contract``).

The conformance package declares no YAML parser, so the manifest is read with
anchored regexes rather than a structural load — the same approach
``release_contract`` takes for ``app_id`` and ``release_model``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

SERIES = "K"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]

_SHORT_DESCRIPTION_RE = re.compile(
    r"^short_description[ \t]*:[ \t]*(.*)$", re.MULTILINE
)
_ICON_URL_RE = re.compile(r"^icon_url[ \t]*:[ \t]*(.*)$", re.MULTILINE)
_ENTRYPOINTS_RE = re.compile(r"^entrypoints[ \t]*:[ \t]*$", re.MULTILINE)
_NEXT_TOP_LEVEL_RE = re.compile(r"^[A-Za-z_]", re.MULTILINE)
_NESTED_DESCRIPTION_RE = re.compile(
    r"^[ \t]+description[ \t]*:[ \t]*(.*)$", re.MULTILINE
)
_NESTED_ICON_URL_RE = re.compile(r"^[ \t]+icon_url[ \t]*:[ \t]*(.*)$", re.MULTILINE)

# YAML values that carry a non-whitespace token yet still render as no text:
# empty quotes and the null literals.
_EMPTY_VALUES = frozenset({'""', "''", "null", "Null", "NULL", "~"})


def discover(root: Path) -> list[Path]:
    """Return ``[root]`` for a pkl-contract-driven app repo, else ``[]``."""
    return [root] if (root / "contract").is_dir() else []


def _line_of(text: str, pattern: re.Pattern[str], default: int = 1) -> int:
    """1-based line of the first ``pattern`` match in ``text`` (``default`` if none)."""
    match = pattern.search(text)
    if match is None:
        return default
    return text.count("\n", 0, match.start()) + 1


def _is_blank(value: str) -> bool:
    """True when a captured YAML scalar renders as no value."""
    stripped = value.split("#", 1)[0].strip()
    return not stripped or stripped in _EMPTY_VALUES


def _entrypoints_block(text: str) -> str:
    """Return the body of the top-level ``entrypoints:`` block, or ``""``."""
    match = _ENTRYPOINTS_RE.search(text)
    if match is None:
        return ""
    body = text[match.end() :]
    nxt = _NEXT_TOP_LEVEL_RE.search(body)
    return body if nxt is None else body[: nxt.start()]


def _has_card_description(text: str) -> bool:
    """True when the manifest supplies card text at app or entrypoint level."""
    match = _SHORT_DESCRIPTION_RE.search(text)
    if match is not None and not _is_blank(match.group(1)):
        return True
    block = _entrypoints_block(text)
    return any(not _is_blank(value) for value in _NESTED_DESCRIPTION_RE.findall(block))


def _blank_icon_scope(text: str) -> str | None:
    """Describe where ``icon_url`` is blank, or ``None`` when every value is set."""
    match = _ICON_URL_RE.search(text)
    if match is None:
        return "declares no top-level 'icon_url'"
    if _is_blank(match.group(1)):
        return "declares a top-level 'icon_url' with no value"
    block = _entrypoints_block(text)
    if any(_is_blank(value) for value in _NESTED_ICON_URL_RE.findall(block)):
        return "declares an entrypoint 'icon_url' with no value"
    return None


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Emit K022 (card description) and K023 (card icon).

    No-ops when ``discover`` returned nothing (not a contract-driven app repo),
    and when ``atlan.yaml`` is absent — a contract repo with no manifest has a
    missing generated output, which is K004's concern, not a blank card.
    """
    if not paths:
        return []

    atlan = root / "atlan.yaml"
    if not atlan.is_file():
        return []

    text = safe_read_text(atlan) or ""
    suppressions = parse_toml_suppressions(text)
    findings: list[Finding] = []

    if not _has_card_description(text):
        findings.append(
            make_toml_finding(
                rule_id="K022",
                file="atlan.yaml",
                line=_line_of(text, _SHORT_DESCRIPTION_RE),
                column=1,
                message=(
                    "atlan.yaml supplies no marketplace-card description: no "
                    "top-level 'short_description' and no entrypoint description. "
                    "The card falls back to the hand-curated Global Marketplace "
                    "row, and renders blank when that row is empty. Set "
                    "shortDescription in contract/app.pkl and regenerate "
                    "(uv run poe generate)."
                ),
                suppressions=suppressions,
            )
        )

    scope = _blank_icon_scope(text)
    if scope is not None:
        findings.append(
            make_toml_finding(
                rule_id="K023",
                file="atlan.yaml",
                line=_line_of(text, _ICON_URL_RE),
                column=1,
                message=(
                    f"atlan.yaml {scope}. The marketplace card renders without a "
                    "logo. Set icon in contract/app.pkl (iconUrl defaults to it) "
                    "and regenerate (uv run poe generate)."
                ),
                suppressions=suppressions,
            )
        )

    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: K022/K023 are cross-artifact; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=(
        "K022/K023 marketplace-card listing: atlan.yaml description + icon_url."
    ),
)


if __name__ == "__main__":
    sys.exit(main())
