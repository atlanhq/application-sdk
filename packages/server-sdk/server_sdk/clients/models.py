"""DatabaseConfig — connection-string template + defaults for a SQL client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DatabaseConfig:
    """Describes how to build a SQLAlchemy URL for a source.

    Attributes:
        template: SQLAlchemy URL template, e.g.
            ``"redshift+psycopg2://{username}:{password}@{host}:{port}/{database}"``.
        required: credential keys that must be present.
        defaults: values merged into the credential dict before formatting.
        connect_args: kwargs forwarded to ``create_engine(connect_args=...)``.
    """

    template: str
    required: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    connect_args: Optional[dict[str, Any]] = None
