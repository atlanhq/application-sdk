"""Extract customer-facing metadata surfaces from ``atlan.yaml``.

``atlan.yaml`` is generated from ``contract/app.pkl`` with a stable, 2-space
indented shape (see ``contract-toolkit/examples/full/atlan.yaml``).  Following the
conformance package convention (``checks/app_name_alignment/_contract_app_name.py``)
we parse it line-by-line rather than adding a PyYAML dependency — the M-series
only needs a handful of known scalar fields, each with the line number of its key
so findings anchor correctly and inline suppression directives line up.

Surfaces extracted:

* top-level ``name`` and ``display_name`` — naming-convention inputs (M001)
* top-level ``short_description`` / ``long_description`` — copy inputs (M002)
* each entrypoint's ``name`` / ``display_name`` (M001) and ``description`` (M002)

Block scalars (``long_description: |-``) are supported; their content lines are
dedented and joined, and the surface anchors to the key line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Fields whose *values* are customer-facing copy (M002 — jargon/appropriateness).
DESCRIPTION_FIELDS = frozenset({"short_description", "long_description", "description"})
# Fields whose *values* are customer-facing names (M001 — naming convention).
NAME_FIELDS = frozenset({"name", "display_name"})

_KEYS_OF_INTEREST = DESCRIPTION_FIELDS | NAME_FIELDS

_LINE_RE = re.compile(
    r"^(?P<indent>\s*)(?P<dash>-\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>.*)$"
)
_BLOCK_INDICATORS = ("|", ">")


@dataclass(frozen=True)
class Surface:
    """One extracted metadata field.

    ``field`` is the leaf key name (e.g. ``"short_description"``); ``location``
    describes where it came from (``"top-level"`` or ``"entrypoint '<name>'"``)
    for human-readable finding messages.
    """

    field: str
    value: str
    line: int
    location: str

    @property
    def is_description(self) -> bool:
        return self.field in DESCRIPTION_FIELDS

    @property
    def is_name(self) -> bool:
        return self.field in NAME_FIELDS


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] in ('"', "'") and value[0] == value[-1]:
        return value[1:-1]
    return value


def _read_block_scalar(lines: list[str], start: int, key_indent: int) -> str:
    """Join the indented body of a block scalar starting after line ``start``.

    Content lines are those more-indented than ``key_indent`` (blank lines are
    kept as paragraph breaks).  Returns the dedented, stripped text.
    """
    body: list[str] = []
    min_indent: int | None = None
    j = start + 1
    while j < len(lines):
        raw = lines[j]
        if raw.strip() == "":
            body.append("")
            j += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= key_indent:
            break
        body.append(raw)
        min_indent = indent if min_indent is None else min(min_indent, indent)
        j += 1
    dedent = min_indent or 0
    return "\n".join(line[dedent:] if line else "" for line in body).strip()


def iter_surfaces(text: str) -> list[Surface]:
    """Return the customer-facing metadata surfaces declared in *text*.

    Silently ignores anything that is not one of the known scalar fields — the
    goal is to feed a model a few short strings, not to validate YAML structure.
    """
    lines = text.split("\n")
    surfaces: list[Surface] = []
    in_entrypoints = False
    current_ep: str | None = None

    for i, line in enumerate(lines):
        m = _LINE_RE.match(line)
        if m is None:
            continue
        indent = len(m.group("indent"))
        is_list = m.group("dash") is not None
        key = m.group("key")
        rest = m.group("rest").strip()

        top_level = indent == 0 and not is_list
        if top_level and key not in ("entrypoints",) and key not in _KEYS_OF_INTEREST:
            # A different top-level key ends any entrypoints block.
            in_entrypoints = False
            current_ep = None

        if top_level and key == "entrypoints":
            in_entrypoints = True
            current_ep = None
            continue

        # Track which entrypoint we're inside (a new "- name:" starts one).
        if in_entrypoints and is_list and key == "name":
            current_ep = _strip_quotes(rest)

        if key not in _KEYS_OF_INTEREST:
            continue

        # Resolve the value: inline scalar or block scalar.
        if rest[:1] in _BLOCK_INDICATORS:
            key_indent = indent + (len(m.group("dash")) if is_list else 0)
            value = _read_block_scalar(lines, i, key_indent)
        else:
            value = _strip_quotes(rest)

        if not value:
            continue

        if top_level:
            location = "top-level"
        elif in_entrypoints and current_ep:
            location = f"entrypoint '{current_ep}'"
        elif in_entrypoints:
            location = "entrypoint"
        else:
            location = "top-level"

        surfaces.append(Surface(field=key, value=value, line=i + 1, location=location))

    return surfaces
