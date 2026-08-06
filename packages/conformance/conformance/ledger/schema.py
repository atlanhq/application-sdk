"""Typed model for the Integration Lane Ledger.

The ledger records, per connector product workflow, the best integration lane
that exercises it — see ``README.md`` in this directory for why this is a
hand-maintained inventory rather than runtime instrumentation.

Only ``realism`` and ``depth`` are trusted from the ledger. ``cadence`` is
advisory and re-derived from the GitHub Actions API by :mod:`.compute`, and the
denominator is re-derived by AST-scanning each repo for its product-workflow
declarations.

Stored as TOML rather than YAML: the conformance package ships only pydantic,
jsonschema and jinja2, and ``tomllib`` is stdlib on the supported Python floor.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Realism(str, Enum):
    """How real the source the lane runs against is."""

    LIVE = "L"
    """A live source system."""

    REPLAY = "R"
    """Replay of captured real data."""

    SYNTHETIC = "S"
    """Synthetic fixtures or a mocked client."""

    NONE = "-"


class Depth(str, Enum):
    """How thoroughly the lane validates the transformed output."""

    GOLDEN = "G"
    """Golden / record-level comparison against a committed expectation."""

    VALIDATED = "V"
    """Schema or contract validation (Pandera, schema-file expectations)."""

    COUNTS = "C"
    """Counts and qualifiedName-shaped assertions."""

    ENVELOPE = "E"
    """Envelope only — the call succeeded, output uninspected."""

    NONE = "-"


class Boundary(str, Enum):
    """Where the lane stops — the integration/e2e line.

    The scope of an integration test ends at the app's own handoff artifact.
    A lane that continues past it (running publish / lineage / QI and checking
    whether assets landed in the tenant) is an e2e test and does not count
    toward IRR, however good it is.

    Directory is not the discriminator — several connectors keep qualifying
    integration lanes under ``tests/e2e/``. What the assertion *reads* is.
    """

    TRANSFORMED = "transformed"
    """Asserts on the app's own transformed/ output, then stops."""

    POST_PUBLISH = "post-publish"
    """Continues into system apps and reads back from the Atlan tenant."""


class Cadence(str, Enum):
    """How the lane is triggered. Advisory here; verified in compute."""

    AUTOMATIC = "A"
    """Runs on push, pull_request or schedule."""

    GATED = "g"
    """Exists but requires a label, env flag or manual dispatch."""

    NONE = "-"


#: The only boundary that counts toward IRR.
QUALIFYING_BOUNDARY = Boundary.TRANSFORMED

#: Realism values that count toward IRR.
QUALIFYING_REALISM = frozenset({Realism.LIVE, Realism.REPLAY})

#: Depth values that count toward IRR.
QUALIFYING_DEPTH = frozenset({Depth.GOLDEN, Depth.VALIDATED})


@dataclass(frozen=True)
class Evidence:
    """Citations backing a lane classification. Reviewed like code."""

    test: str | None = None
    ci_workflow: str | None = None
    ci_job: str | None = None
    gate: str | None = None
    notes: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Evidence:
        raw = raw or {}
        return cls(
            test=raw.get("test") or None,
            ci_workflow=raw.get("ci_workflow") or None,
            ci_job=raw.get("ci_job") or None,
            gate=raw.get("gate") or None,
            notes=raw.get("notes") or None,
        )


@dataclass(frozen=True)
class Lane:
    """The best integration lane exercising one workflow."""

    realism: Realism
    depth: Depth
    cadence: Cadence
    boundary: Boundary
    evidence: Evidence

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Lane:
        """Built from the flat workflow table; axes and citations sit together."""
        return cls(
            realism=Realism(raw["realism"]),
            depth=Depth(raw["depth"]),
            cadence=Cadence(raw["cadence"]),
            boundary=Boundary(raw["boundary"]),
            evidence=Evidence.from_dict(raw),
        )

    @property
    def qualifies_on_declared_axes(self) -> bool:
        """Boundary, realism and depth. Cadence is verified elsewhere."""
        return (
            self.boundary is QUALIFYING_BOUNDARY
            and self.realism in QUALIFYING_REALISM
            and self.depth in QUALIFYING_DEPTH
        )


@dataclass(frozen=True)
class Workflow:
    """One product workflow (an ``@entrypoint``) and its lane."""

    id: str
    declared_at: str
    lane: Lane

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Workflow:
        return cls(
            id=raw["id"],
            declared_at=raw.get("declared_at", ""),
            lane=Lane.from_dict(raw),
        )


@dataclass(frozen=True)
class Exclusion:
    """An entrypoint deliberately outside the denominator."""

    id: str
    reason: str
    ticket: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Exclusion:
        return cls(
            id=raw["id"],
            reason=raw["reason"],
            ticket=raw.get("ticket", "TBD"),
        )


@dataclass(frozen=True)
class ConnectorLedger:
    """All ledger rows for one connector repo."""

    name: str
    workflows: tuple[Workflow, ...]
    excluded: tuple[Exclusion, ...] = ()
    notes: str | None = None

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> ConnectorLedger:
        return cls(
            name=name,
            workflows=tuple(Workflow.from_dict(w) for w in raw.get("workflows", [])),
            excluded=tuple(Exclusion.from_dict(e) for e in raw.get("excluded", [])),
            notes=raw.get("notes"),
        )

    @property
    def declared_ids(self) -> frozenset[str]:
        """Every entrypoint id the ledger accounts for, classified or excluded."""
        return frozenset(w.id for w in self.workflows) | frozenset(
            e.id for e in self.excluded
        )


@dataclass
class Ledger:
    """The whole fleet ledger."""

    version: int
    connectors: dict[str, ConnectorLedger] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Ledger:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(
            version=raw["version"],
            connectors={
                entry["name"]: ConnectorLedger.from_dict(entry["name"], entry)
                for entry in raw.get("connectors", [])
            },
        )


DEFAULT_LEDGER_PATH = Path(__file__).parent / "integration-ledger.toml"
