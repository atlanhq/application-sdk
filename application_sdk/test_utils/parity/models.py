"""Data models for parity comparison results."""

from dataclasses import dataclass, field
from typing import Any, List


@dataclass
class FieldDiff:
    """A single field-level difference between baseline and candidate."""

    field_path: str
    baseline_value: Any
    candidate_value: Any


@dataclass
class AssetDiff:
    """A difference for a single asset (identified by qualifiedName)."""

    qualified_name: str
    type_name: str
    diff_type: str  # ADDED, REMOVED, MODIFIED
    field_diffs: List[FieldDiff] = field(default_factory=list)


@dataclass
class CategoryResult:
    """Comparison result for a single category (e.g., table, column)."""

    category: str
    baseline_count: int
    candidate_count: int
    added: List[AssetDiff] = field(default_factory=list)
    removed: List[AssetDiff] = field(default_factory=list)
    modified: List[AssetDiff] = field(default_factory=list)

    @property
    def has_diffs(self) -> bool:
        return bool(self.added or self.removed or self.modified)
