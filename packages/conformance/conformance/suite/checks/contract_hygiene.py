"""K024/K025/K026 contract-hygiene guards on ``contract/app.pkl``.

K024 ``EscapeHatchShadowsTypedField``: ``metadata`` and ``atlanYamlOverrides``
deep-merge *over* the rendered manifest, so a key that also has a typed
``App.pkl`` field makes the contract say one thing and the output another. The
typed field is silently discarded and ``pkl eval`` cannot type-check the
override. Keys with no typed equivalent are the escape hatch working as
intended and are not reported.

K025 ``BlankStringAssignment``: a String field assigned ``""`` is a no-op. The
toolkit omits empty values from the manifest, so the line renders nothing while
reading like a decision.

K026 ``DeprecatedContractField``: ``emitEntrypoints`` is marked ``@Deprecated``
in ``App.pkl`` with removal stated for the next minor toolkit version. B001
covers deprecated SDK *Python* symbols; nothing covers a deprecated *pkl
contract* field, so an app carrying one breaks on the bump with no warning.

All three are APP-scoped and read ``contract/app.pkl`` directly — the contract
source, not its generated output — because each concerns what the author wrote
rather than what was rendered.

The checks are textual: the conformance package has no pkl parser, so blocks
are located by an anchored header match and closed by brace balance. Line
comments are stripped first so a commented-out assignment cannot fire a rule.

Suppression uses the pkl directive form (``// conformance: ignore[K024] why``)
via the parser the legacy-contract check already owns, rather than the ``#``
scanner the TOML and YAML checks share.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import make_cli_main, safe_read_text
from conformance.suite.checks.legacy_contract._directives_pkl import (
    _make_pkl_finding_suppressed,
    _parse_pkl_directives,
)
from conformance.suite.schema.findings import Finding

SERIES = "K"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]

# Rendered manifest keys that App.pkl already models as a typed field. An
# escape-hatch entry for one of these overrides the typed value rather than
# supplementing it. Keys the toolkit does not model (release_model, dockerfile,
# description, categories, source, source_category, package_id) are absent by
# design — routing those through metadata is currently the only way to set them.
_TYPED_MANIFEST_KEYS = frozenset(
    {
        "app_id",
        "argo_package_names",
        "build_tag",
        "creator_org",
        "deploy",
        "display_name",
        "docs_url",
        "entrypoints",
        "icon_url",
        "long_description",
        "name",
        "self_deployed_runtime",
        "short_description",
        "tags",
        "type",
        "visibility",
    }
)

# App.pkl String fields whose default is "", so an explicit "" is always a
# redundant restatement of the default rather than an override of something.
_BLANKABLE_STRING_FIELDS = (
    "docsUrl",
    "helpdeskLink",
    "shortDescription",
    "longDescription",
    "credentialConnectorType",
    "credentialAuthTitle",
)

_DEPRECATED_FIELDS = {
    "emitEntrypoints": (
        "use the entrypoints listing with packageId set on those that should "
        "render as marketplace cards"
    ),
}

_METADATA_HEADER_RE = re.compile(r"^[ \t]*metadata[ \t]*\{", re.MULTILINE)
_OVERRIDES_HEADER_RE = re.compile(r"^[ \t]*atlanYamlOverrides[ \t]*\{", re.MULTILINE)
_MAPPING_KEY_RE = re.compile(r'\["([^"]+)"\]')
_BLANK_ASSIGN_RE = re.compile(
    r"^[ \t]*(" + "|".join(_BLANKABLE_STRING_FIELDS) + r")[ \t]*=[ \t]*\"\"[ \t]*$",
    re.MULTILINE,
)
_DEPRECATED_ASSIGN_RE = re.compile(
    r"^[ \t]*(" + "|".join(_DEPRECATED_FIELDS) + r")[ \t]*=", re.MULTILINE
)


def discover(root: Path) -> list[Path]:
    """Return ``[root]`` when ``contract/app.pkl`` exists, else ``[]``."""
    return [root] if (root / "contract" / "app.pkl").is_file() else []


def _strip_line_comments(text: str) -> str:
    """Blank out whole-line ``//`` comments, preserving line numbering."""
    return "\n".join(
        "" if line.lstrip().startswith("//") else line for line in text.split("\n")
    )


def _line_at(text: str, index: int) -> int:
    """1-based line number of ``index`` within ``text``."""
    return text.count("\n", 0, index) + 1


def _balanced_block(text: str, header: re.Match[str]) -> str:
    """Return the brace-balanced body that ``header`` opens."""
    start = text.index("{", header.start())
    depth = 0
    for offset in range(start, len(text)):
        if text[offset] == "{":
            depth += 1
        elif text[offset] == "}":
            depth -= 1
            if depth == 0:
                return text[start : offset + 1]
    return text[start:]


def _shadowed_keys(text: str) -> list[tuple[str, str, int]]:
    """Return ``(hatch, key, line)`` for each escape-hatch key with a typed field."""
    hits: list[tuple[str, str, int]] = []
    for hatch, pattern in (
        ("metadata", _METADATA_HEADER_RE),
        ("atlanYamlOverrides", _OVERRIDES_HEADER_RE),
    ):
        header = pattern.search(text)
        if header is None:
            continue
        block = _balanced_block(text, header)
        block_start = text.index("{", header.start())
        for key_match in _MAPPING_KEY_RE.finditer(block):
            key = key_match.group(1)
            if key in _TYPED_MANIFEST_KEYS:
                hits.append(
                    (hatch, key, _line_at(text, block_start + key_match.start()))
                )
    return hits


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Emit K024 (shadowed typed field), K025 (blank string), K026 (deprecated)."""
    if not paths:
        return []

    contract = root / "contract" / "app.pkl"
    raw = safe_read_text(contract) or ""
    text = _strip_line_comments(raw)
    directives = _parse_pkl_directives(raw)
    findings: list[Finding] = []

    def _emit(rule_id: str, line: int, message: str) -> None:
        suppressed, justification = _make_pkl_finding_suppressed(
            rule_id=rule_id, line=line, directives=directives
        )
        findings.append(
            Finding(
                rule_id=rule_id,
                file="contract/app.pkl",
                line=line,
                column=1,
                message=f"{message}  Suppress with: // conformance: ignore[{rule_id}] <reason>",
                suppressed=suppressed,
                suppression_justification=justification,
            )
        )

    for hatch, key, line in _shadowed_keys(text):
        _emit(
            "K024",
            line,
            f"{hatch} sets '{key}', which App.pkl already models as a typed "
            "field. The escape hatch deep-merges over the rendered manifest, so "
            "the typed value is discarded and pkl eval cannot type-check the "
            "override. Move the value onto the typed field and drop the "
            "override.",
        )

    for match in _BLANK_ASSIGN_RE.finditer(text):
        field = match.group(1)
        _emit(
            "K025",
            _line_at(text, match.start()),
            f"{field} is assigned an empty string, which is already its default. "
            "The toolkit omits empty values from the generated manifest, so the "
            "line renders nothing while reading like a decision. Give it a value "
            "or delete the assignment.",
        )

    for match in _DEPRECATED_ASSIGN_RE.finditer(text):
        field = match.group(1)
        _emit(
            "K026",
            _line_at(text, match.start()),
            f"{field} is deprecated in App.pkl and its removal is stated for the "
            f"next minor toolkit version - {_DEPRECATED_FIELDS[field]}. The "
            "contract stops evaluating when the field is dropped.",
        )

    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: K024/K025/K026 are contract-file checks; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    discover=discover,
    description=(
        "K024/K025/K026 contract hygiene: escape-hatch shadowing, blank string "
        "assignments, deprecated contract fields."
    ),
)


if __name__ == "__main__":
    sys.exit(main())
