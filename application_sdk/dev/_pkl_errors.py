"""Typed error leaves for the pinned-pkl dev toolchain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InternalError


@dataclass(kw_only=True)
class UnsupportedPklPlatformError(InternalError):
    """No published pkl asset for this os/arch pair.

    pkl ships one native binary per platform; the combination reported by
    ``platform`` has none, so there is nothing to download. Separate from
    Dapr's ``UnsupportedArchitectureError`` because the *supported sets differ*
    — pkl publishes no arm64 Windows build, for one — and a shared error would
    have to describe both.
    """

    code: ClassVar[str] = "INTERNAL_PKL_UNSUPPORTED_PLATFORM"
    message: str = "No published pkl release asset for this platform"
    component: str | None = "pinned_pkl"
    os_name: str | None = None
    architecture: str | None = None


@dataclass(kw_only=True)
class PklDownloadError(InternalError):
    """The pinned pkl binary could not be fetched.

    pkl is a GitHub release asset, so this is a live dependency on the release
    CDN — which returns 503s in bursts. Raised only after the retries in
    ``application_sdk.dev.pkl`` are exhausted.
    """

    code: ClassVar[str] = "INTERNAL_PKL_DOWNLOAD_FAILED"
    message: str = "Could not download the pinned pkl binary"
    component: str | None = "pinned_pkl"
    asset_url: str | None = None
    attempts: int | None = None
