"""The storage settings the read and write path reads, without importing ``constants``.

``application_sdk.storage.ops`` and the modules it needs ship in the api
distribution's ``obstore`` extra, whose closure stays the storage path alone,
so these modules read their settings here instead of importing
``application_sdk.constants``. Each reader
returns ``constants``' value whenever that module is loaded, as it always is on
the worker (and as tests patch it), else reads the same variable with the same
default (:func:`application_sdk._env.sdk_constant`).
``tests/unit/test_api_distribution.py`` pins that every reader agrees with
``constants``; the variables are documented there and in
``docs/configuration.md``.
"""

from __future__ import annotations

import os

from application_sdk._env import env_bool, env_float, env_int, sdk_constant

__all__ = [
    "lock_wait_progress_seconds",
    "max_concurrent_storage_transfers",
    "progress_log_interval_seconds",
    "resume_downloads",
    "transfer_memory_budget_bytes",
    "upload_max_concurrency",
    "upload_part_size_bytes",
    "upload_part_size_overridden",
    "verify_transfers",
    "write_sidecars",
]

#: The part-size variable: its presence alone overrides a caller's chunk size.
_PART_SIZE_ENV = "ATLAN_STORAGE_UPLOAD_PART_SIZE_BYTES"


def max_concurrent_storage_transfers() -> int:
    """``ATLAN_MAX_CONCURRENT_STORAGE_TRANSFERS`` (default 4)."""
    return sdk_constant(
        "MAX_CONCURRENT_STORAGE_TRANSFERS",
        lambda: env_int("ATLAN_MAX_CONCURRENT_STORAGE_TRANSFERS", 4),
    )


def progress_log_interval_seconds() -> float:
    """``ATLAN_STORAGE_PROGRESS_LOG_INTERVAL_SECONDS`` (default 30)."""
    return sdk_constant(
        "STORAGE_PROGRESS_LOG_INTERVAL_SECONDS",
        lambda: env_float("ATLAN_STORAGE_PROGRESS_LOG_INTERVAL_SECONDS", 30.0),
    )


def lock_wait_progress_seconds() -> float:
    """``ATLAN_STORAGE_LOCK_WAIT_PROGRESS_SECONDS`` (default 30), capped at a
    quarter of ``ATLAN_MAX_NO_PROGRESS_SECONDS`` and floored at 1s, as
    ``constants`` computes it."""
    return sdk_constant(
        "STORAGE_LOCK_WAIT_PROGRESS_SECONDS",
        lambda: max(
            1.0,
            min(
                env_float("ATLAN_STORAGE_LOCK_WAIT_PROGRESS_SECONDS", 30.0),
                env_float("ATLAN_MAX_NO_PROGRESS_SECONDS", 900.0) / 4.0,
            ),
        ),
    )


def upload_part_size_bytes() -> int:
    """``ATLAN_STORAGE_UPLOAD_PART_SIZE_BYTES`` (default 8 MiB)."""
    return sdk_constant(
        "STORAGE_UPLOAD_PART_SIZE_BYTES",
        lambda: env_int(_PART_SIZE_ENV, 8 * 1024 * 1024),
    )


def upload_part_size_overridden() -> bool:
    """Whether the deployment set ``ATLAN_STORAGE_UPLOAD_PART_SIZE_BYTES``."""
    return sdk_constant(
        "STORAGE_UPLOAD_PART_SIZE_OVERRIDDEN", lambda: _PART_SIZE_ENV in os.environ
    )


def upload_max_concurrency() -> int:
    """``ATLAN_STORAGE_UPLOAD_MAX_CONCURRENCY`` (default 12)."""
    return sdk_constant(
        "STORAGE_UPLOAD_MAX_CONCURRENCY",
        lambda: env_int("ATLAN_STORAGE_UPLOAD_MAX_CONCURRENCY", 12),
    )


def transfer_memory_budget_bytes() -> int:
    """``ATLAN_STORAGE_TRANSFER_MEMORY_BUDGET_BYTES`` (default 0: derived)."""
    return sdk_constant(
        "STORAGE_TRANSFER_MEMORY_BUDGET_BYTES",
        lambda: env_int("ATLAN_STORAGE_TRANSFER_MEMORY_BUDGET_BYTES", 0),
    )


def resume_downloads() -> bool:
    """``ATLAN_STORAGE_RESUME_DOWNLOADS`` (default on)."""
    return sdk_constant(
        "STORAGE_RESUME_DOWNLOADS",
        lambda: env_bool("ATLAN_STORAGE_RESUME_DOWNLOADS", True),
    )


def verify_transfers() -> bool:
    """``ATLAN_STORAGE_VERIFY_TRANSFERS`` (default on)."""
    return sdk_constant(
        "STORAGE_VERIFY_TRANSFERS",
        lambda: env_bool("ATLAN_STORAGE_VERIFY_TRANSFERS", True),
    )


def write_sidecars() -> bool:
    """``ATLAN_STORAGE_WRITE_SIDECARS`` (default on)."""
    return sdk_constant(
        "STORAGE_WRITE_SIDECARS",
        lambda: env_bool("ATLAN_STORAGE_WRITE_SIDECARS", True),
    )
