import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Set


class ProcessingStrategy(str, Enum):
    FILE_LEVEL = "file_level"
    TYPE_LEVEL = "type_level"


class DiffStatus(str, Enum):
    NEW = "NEW"
    DIFF = "DIFF"
    DELETE = "DELETE"
    NO_DIFF = "NO_DIFF"


@dataclass
class PublishConfig:
    """Configuration for Atlas publish workflow."""

    concurrency_activities: int = 5
    max_activity_concurrency: int = 10
    username: str = ""

    # FIXME(inishchith): Can we figure this out without input? say via record count?
    high_density_asset_types: List[str] = field(default_factory=lambda: ["Column"])
    state_store_config: Dict[str, Any] = field(default_factory=dict)
    lock_config: Dict[str, Any] = field(default_factory=dict)
    atlas_api_config: Dict[str, Any] = field(default_factory=dict)

    # Execute phase specific configurations
    publish_chunk_count: int = 50
    retry_config: Dict[str, Any] = field(
        default_factory=lambda: {
            "max_retries": 3,
            "backoff_factor": 2,
            "retry_status_codes": [429, 500, 502, 503, 504],
            "fail_fast_status_codes": [400, 401, 403],
        }
    )
    continue_on_error: bool = False
    atlas_timeout: int = 60  # in seconds


@dataclass
class FileAssignment:
    """File assignment for high-density asset types."""

    file_path: str
    estimated_records: int

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileAssignment":
        """Create instance from dictionary."""
        return cls(
            file_path=data.get("file_path", ""),
            estimated_records=data.get("estimated_records", 0),
        )


@dataclass
class AssetTypeAssignment:
    """Assignment for asset types."""

    asset_type: str
    directory_prefix: str
    estimated_file_count: int
    estimated_total_records: int
    is_deletion: bool
    processing_strategy: str
    diff_status: str
    files: List[FileAssignment] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        result = asdict(self)
        if self.files:
            result["files"] = [file.to_dict() for file in self.files]
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AssetTypeAssignment":
        """Create instance from dictionary."""
        files_data = data.pop("files", []) if "files" in data else []
        instance = cls(**{k: v for k, v in data.items() if k != "files"})
        instance.files = [
            FileAssignment.from_dict(file_dict)
            for file_dict in files_data
            if isinstance(file_dict, dict)
        ]
        return instance


@dataclass
class PublishPlan:
    """Top-level publish plan contract."""

    publish_order: List[str] = field(default_factory=list)
    dependency_graph: Dict[str, List[str]] = field(default_factory=dict)
    asset_type_assignments: Dict[str, Dict[str, List[AssetTypeAssignment]]] = field(
        default_factory=dict
    )
    self_referential_types: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "publish_order": self.publish_order,
            "dependency_graph": self.dependency_graph,
            "asset_type_assignments": {
                asset_type: {
                    diff_status: [assignment.to_dict() for assignment in assignments]
                    for diff_status, assignments in assignments.items()
                }
                for asset_type, assignments in self.asset_type_assignments.items()
            },
            "self_referential_types": list(self.self_referential_types),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PublishPlan":
        """Create instance from dictionary."""
        return cls(
            publish_order=data.get("publish_order", []),
            dependency_graph=data.get("dependency_graph", {}),
            asset_type_assignments={
                asset_type: {
                    diff_status: [
                        AssetTypeAssignment.from_dict(assignment)
                        for assignment in assignments
                    ]
                    for diff_status, assignments in data.get(
                        "asset_type_assignments", {}
                    )
                    .get(asset_type, {})
                    .items()
                }
                for asset_type in data.get("asset_type_assignments", {})
            },
            self_referential_types=set(data.get("self_referential_types", [])),
        )

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str) -> "PublishPlan":
        """Create instance from JSON string."""
        return cls.from_dict(json.loads(json_str))
