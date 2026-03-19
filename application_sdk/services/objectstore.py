"""Backward-compatible re-export. Use application_sdk.services.storage instead."""

from application_sdk.services.storage import ObjectStore

__all__ = ["ObjectStore"]
