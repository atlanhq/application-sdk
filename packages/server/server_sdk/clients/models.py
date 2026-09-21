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
        defaults: default connection values. A key that names a ``template``
            placeholder fills that placeholder; any other key is appended to the
            URL as a query parameter. Both meanings are load-bearing, and a
            DB_CONFIG lifted verbatim from ``application_sdk`` relies on the
            second -- ``{"connect_timeout": 5, "application_name": "Atlan"}``
            names no placeholder and must still reach the URL.
        parameters: credential keys to append as query parameters when present,
            e.g. ``["ssl_mode"]`` -> ``?ssl_mode=require``.
        connect_args: kwargs forwarded to ``create_engine(connect_args=...)``.
        pool_pre_ping: whether SQLAlchemy tests a pooled connection for liveness
            before checkout. Disable only where the driver's ping path is unsafe.
    """

    template: str
    required: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    parameters: Optional[list[str]] = None
    connect_args: Optional[dict[str, Any]] = None
    pool_pre_ping: bool = True
