"""O009 WorkerVolumeWithoutAutoresize — fixed-size worker volumes in ``atlan.yaml``.

An app declares its worker disks in ``atlan.yaml``'s ``deploy:`` block as a list
of ephemeral PVCs, each ``{name, mountPath, size, annotations}``:

* ``deploy.storage`` — mounted on every pod the app runs, workers included;
* ``deploy.storageWorker`` — mounted on worker pods only (split deployment);
* ``deploy.storageServer`` — mounted on server pods only, so it is not graded.

An entry with no ``resize.topolvm.io/storage_limit`` annotation is never grown
by the pvc-autoresizer, so its ``size`` is fixed: it has to cover the largest
run any tenant makes, and every pod that mounts it gets the full size.  Each such
entry is reported on its ``-`` line with its ``name``, ``mountPath`` and ``size``.

Why ``atlan.yaml`` and not ``contract/app.pkl``
------------------------------------------------
``atlan.yaml`` is what the platform deploys, and every app has one, hand-written
or generated.  Where ``contract/app.pkl`` generates it, the storage list rides
the contract's untyped ``deploy`` → ``overrides`` mapping (the toolkit models no
storage field), so reading the Pkl would mean evaluating arbitrary Pkl
expressions; the rendered YAML is the stable shape, and the generated-artifact
freshness rules (K003-K005) keep it in step with the contract.  Grading both
files would report every volume twice.  The finding message says where the edit
goes when the contract emits the file.

The scan is line-based — this package deliberately has no YAML dependency, the
same posture as P040 and the T016 compose-overlay check — and indentation-aware:
only ``storage`` / ``storageWorker`` keys that are direct children of the
top-level ``deploy:`` key count, so a nested ``storage:`` (a PVC template's
``resources.requests.storage``, say) is never mistaken for one.  Both block
sequence layouts are read: items indented under the key (hand-written) and
items flush with it (what the toolkit's YAML renderer emits).

Inline suppression
------------------
``# conformance: ignore[O009] <reason>`` on the entry's ``-`` line or the line
directly above it — YAML ``#`` comments slot into the shared directive grammar
(see ``_ast_common.parse_toml_suppressions``).  Only a hand-written
``atlan.yaml`` can carry one: regeneration drops comments from a generated file.

Known limits (intentional — biased toward zero false positives at WARN):

* A flow-style sequence (``storage: [...]``), a flow-mapping entry
  (``- {name: ..., size: ...}``), YAML anchors / merge keys, per-pool storage
  under ``workerPools``, and raw ``extraVolumes`` PVC templates are not graded.
* Whether ``storage_limit`` is actually larger than ``size`` is not checked.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import (
    make_cli_main,
    make_toml_finding,
    parse_toml_suppressions,
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

SERIES = "O"
RULE_O009 = "O009"

__all__ = [
    "SERIES",
    "STORAGE_LIMIT_ANNOTATION",
    "WORKER_STORAGE_KEYS",
    "StorageEntry",
    "discover",
    "main",
    "scan_path",
    "scan_text",
    "storage_entries",
]

ATLAN_YAML = "atlan.yaml"

#: ``deploy`` keys whose entries mount on worker pods.  ``storageServer`` mounts
#: on server pods only and is deliberately absent.
WORKER_STORAGE_KEYS: tuple[str, ...] = ("storage", "storageWorker")

#: The pvc-autoresizer annotation without which a PVC is never grown.
STORAGE_LIMIT_ANNOTATION = "resize.topolvm.io/storage_limit"

_DEPLOY_RE = re.compile(r"""^(["']?)deploy\1[ \t]*:[ \t]*$""")

#: One ``key: value`` pair of a block mapping, indentation already stripped.  A
#: plain key may hold ``.`` and ``/`` (annotation keys do) but no ``:``.
_PAIR_RE = re.compile(
    r"""^(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<plain>[^\s:#'"{}\[\]][^:#]*?))"""
    r"""[ \t]*:(?:[ \t]+(?P<value>.*))?$"""
)

_ITEM_RE = re.compile(r"^ *-(?: |$)")

_NULL_SCALARS = frozenset({"", "null", "Null", "NULL", "~"})


@dataclass(frozen=True)
class StorageEntry:
    """One worker storage entry of the ``deploy:`` block."""

    key: str
    """The ``deploy`` key the entry sits under (``storage`` or ``storageWorker``)."""
    line: int
    """1-based line of the entry's ``-``."""
    column: int
    """1-based column of the entry's ``-``."""
    name: str | None
    mount_path: str | None
    size: str | None
    has_storage_limit: bool
    """Whether the entry carries a non-empty ``resize.topolvm.io/storage_limit``."""


def _strip_comment(line: str) -> str:
    """*line* without a trailing ``#`` comment, honouring quoted scalars.

    A ``#`` starts a comment at the start of the line or after whitespace.  A
    quote opens a quoted scalar only where a scalar can start (line start,
    whitespace, or after ``:``/``-``/``[``/``{``/``,``), so an apostrophe inside
    a plain scalar does not swallow a later comment.
    """
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote == "'":
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quote = None
        elif ch in ("'", '"') and (i == 0 or line[i - 1] in " \t:-[{,"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
        i += 1
    return line.rstrip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _pair(content: str) -> tuple[str, str | None] | None:
    """``(key, raw_value)`` for a ``key: value`` line, else ``None``."""
    match = _PAIR_RE.match(content)
    if match is None:
        return None
    key = match.group("dq")
    if key is None:
        key = match.group("sq")
    if key is None:
        key = match.group("plain")
    return key, match.group("value")


def _has_value(raw: str | None) -> bool:
    return raw is not None and _unquote(raw) not in _NULL_SCALARS


def _flow_has_limit(raw: str) -> bool:
    """Whether a flow mapping ``{k: v, ...}`` carries a non-empty storage limit."""
    raw = raw.strip()
    if not (raw.startswith("{") and raw.endswith("}")):
        return False
    for part in raw[1:-1].split(","):
        key, sep, value = part.partition(":")
        if sep and _unquote(key) == STORAGE_LIMIT_ANNOTATION and _has_value(value):
            return True
    return False


def _parse_entry(
    lines: list[str], start: int, stop: int, key: str, item_indent: int
) -> StorageEntry | None:
    """Parse the entry whose ``-`` is on ``lines[start]``; ``None`` if unreadable."""
    after_dash = lines[start][item_indent + 1 :]
    content = after_dash.lstrip(" ")
    rest = [
        (_indent(lines[i]), lines[i].strip())
        for i in range(start + 1, stop)
        if lines[i].strip()
    ]
    if content:
        if content.startswith("{"):
            return None  # flow-mapping entry: not graded
        key_indent = item_indent + 1 + len(after_dash) - len(content)
        body = [(key_indent, content), *rest]
    elif rest:
        key_indent = rest[0][0]
        body = rest
    else:
        return None

    fields: dict[str, str] = {}
    has_limit = False
    in_annotations = False
    for indent, text in body:
        if indent <= key_indent:
            in_annotations = False
        if indent < key_indent:
            continue
        pair = _pair(text)
        if pair is None:
            continue
        name, raw = pair
        if indent == key_indent:
            if name == "annotations":
                if raw is not None and raw.strip():
                    has_limit = has_limit or _flow_has_limit(raw)
                else:
                    in_annotations = True
            elif raw is not None:
                fields.setdefault(name, _unquote(raw))
        elif in_annotations and name == STORAGE_LIMIT_ANNOTATION and _has_value(raw):
            has_limit = True
    return StorageEntry(
        key=key,
        line=start + 1,
        column=item_indent + 1,
        name=fields.get("name") or None,
        mount_path=fields.get("mountPath") or None,
        size=fields.get("size") or None,
        has_storage_limit=has_limit,
    )


def _block_end(lines: list[str], key_line: int, end: int, key_indent: int) -> int:
    """Index of the first line after the value of the key on ``lines[key_line]``.

    The value runs until a line indented left of the key, or level with it and
    not a ``-`` item — items flush with their key are how the toolkit's YAML
    renderer lays out a sequence.
    """
    for idx in range(key_line + 1, end):
        line = lines[idx]
        if not line.strip():
            continue
        indent = _indent(line)
        if indent < key_indent or (
            indent == key_indent and _ITEM_RE.match(line) is None
        ):
            return idx
    return end


def _sequence_entries(
    lines: list[str], key_line: int, stop: int, key: str
) -> list[StorageEntry]:
    """Entries of the block sequence between ``lines[key_line]`` and *stop*."""
    item_indent: int | None = None
    starts: list[int] = []
    for idx in range(key_line + 1, stop):
        line = lines[idx]
        if not line.strip():
            continue
        is_item = _ITEM_RE.match(line) is not None
        if item_indent is None:
            if not is_item:
                return []  # a mapping, not a sequence: not graded
            item_indent = _indent(line)
        if is_item and _indent(line) == item_indent:
            starts.append(idx)
    if item_indent is None:
        return []
    entries: list[StorageEntry] = []
    for n, first in enumerate(starts):
        last = starts[n + 1] if n + 1 < len(starts) else stop
        entry = _parse_entry(lines, first, last, key, item_indent)
        if entry is not None:
            entries.append(entry)
    return entries


def storage_entries(text: str) -> list[StorageEntry]:
    """Every ``deploy.storage`` / ``deploy.storageWorker`` entry in *text*."""
    lines = [_strip_comment(raw) for raw in text.splitlines()]
    deploy = next((i for i, line in enumerate(lines) if _DEPLOY_RE.match(line)), None)
    if deploy is None:
        return []
    end = next(
        (
            i
            for i in range(deploy + 1, len(lines))
            if lines[i].strip() and _indent(lines[i]) == 0
        ),
        len(lines),
    )
    child_indent = next(
        (_indent(lines[i]) for i in range(deploy + 1, end) if lines[i].strip()),
        None,
    )
    if child_indent is None:
        return []
    entries: list[StorageEntry] = []
    idx = deploy + 1
    while idx < end:
        line = lines[idx]
        if line.strip() and _indent(line) == child_indent:
            pair = _pair(line.strip())
            if (
                pair is not None
                and pair[0] in WORKER_STORAGE_KEYS
                and not (pair[1] or "").strip()
            ):
                stop = _block_end(lines, idx, end, child_indent)
                entries.extend(_sequence_entries(lines, idx, stop, pair[0]))
                idx = stop
                continue
        idx += 1
    return entries


def _message(entry: StorageEntry) -> str:
    label = f"'{entry.name}'" if entry.name else "(unnamed)"
    mount = f" (mountPath {entry.mount_path})" if entry.mount_path else ""
    size = f"a fixed {entry.size} volume" if entry.size else "a fixed volume (no size)"
    pods = "worker pod" if entry.key == "storageWorker" else "pod"
    return (
        f"deploy.{entry.key} entry {label}{mount} declares {size} with no "
        f"{STORAGE_LIMIT_ANNOTATION} annotation, so the volume never grows: every "
        f"{pods} that mounts it gets the full size, sized up front for the largest "
        "run any tenant makes. Use a small base size plus the "
        f"{STORAGE_LIMIT_ANNOTATION}, resize.topolvm.io/threshold and "
        "resize.topolvm.io/increase annotations, sizing the base so about 1% of "
        "pods need to grow. If contract/app.pkl emits atlan.yaml, make the change "
        "in its deploy overrides and regenerate; otherwise edit atlan.yaml."
    )


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan ``atlan.yaml`` *text* for O009 findings."""
    flagged = [e for e in storage_entries(text) if not e.has_storage_limit]
    if not flagged:
        return []
    suppressions = parse_toml_suppressions(text)
    return [
        make_toml_finding(
            rule_id=RULE_O009,
            file=file,
            line=entry.line,
            column=entry.column,
            message=_message(entry),
            suppressions=suppressions,
        )
        for entry in flagged
    ]


def discover(root: Path) -> list[Path]:
    """Return ``[<root>/atlan.yaml]`` when the repo has one, else ``[]``."""
    candidate = root / ATLAN_YAML
    return [candidate] if candidate.is_file() else []


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan one ``atlan.yaml``, producing a repo-root-relative URI."""
    text = safe_read_text(path)
    if text is None:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


main = make_cli_main(
    scan_text,
    discover=discover,
    description=(
        "O009: worker storage entries in atlan.yaml's deploy block with no "
        "resize.topolvm.io/storage_limit annotation (fixed-size volumes)."
    ),
)
"""CLI entry point for the O009 worker-volume check."""


if __name__ == "__main__":
    sys.exit(main())
