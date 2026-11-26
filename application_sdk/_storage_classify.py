"""The obstore-free half of the storage seam the worker and the API host share.

The worker's store is :mod:`application_sdk.storage`; the consolidated API host's
is :class:`~application_sdk.handler.object_routes.HostObjectStore`. They are two
stacks because the ``atlan-application-sdk-api`` wheel ships neither
``application_sdk.storage`` nor ``obstore`` in its core install, but they talk to
the same buckets through the same gateway, so they must read a failure the same
way and leave the same objects behind. This module is what they share:

* :func:`classify_storage_failure`, the one table that names a failed store
  request: a gateway that has not loaded its routes, a gateway that could not
  verify the request, a missing Azure container, a missing object, or anything
  else. The worker turns the verdict into a ``StorageError`` leaf
  (``storage.ops._storage_error_for``); the host into a 503 or a 404.
* :func:`past_gateway_no_route`, the retry of the gateway's transient
  ``404 "no Route matched"``, which obstore's own retry layer never retries.
* the ``{key}.sha256`` sidecar convention every upload records.

It imports nothing from ``obstore`` or ``application_sdk.storage``: obstore's
exception classes are matched by name, and everything else by the text obstore
formats every backend failure into. Keep it that way; the api wheel's surface
check fails the build otherwise.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

from application_sdk._logging import get_logger
from application_sdk.common.backoff import Backoff, Jitter, retry_async

logger = get_logger(__name__)

_T = TypeVar("_T")

# --- the sidecar convention ---------------------------------------------------

#: Suffix of the SHA-256 sidecar written alongside every uploaded object.
SIDECAR_SUFFIX = ".sha256"

#: Env var that turns sidecar emission off (``ATLAN_STORAGE_WRITE_SIDECARS``).
#: The worker reads it once into ``constants.STORAGE_WRITE_SIDECARS``; the host,
#: which cannot import ``constants``, reads it through ``_env``.
WRITE_SIDECARS_ENV = "ATLAN_STORAGE_WRITE_SIDECARS"


def sidecar_key(key: str) -> str:
    """Object-store key of the SHA-256 sidecar written alongside *key*."""
    return key + SIDECAR_SUFFIX


def is_sidecar_key(key: str) -> bool:
    """Return ``True`` when *key* is a SHA-256 sidecar rather than a data object."""
    return key.endswith(SIDECAR_SUFFIX)


# --- evidence parsed from a failure -------------------------------------------

# ``object_store`` formats every backend HTTP failure as
#   "Generic {store} error: Error performing {METHOD} {url} in {elapsed} -
#    Server returned non-2xx status code: {status} {reason}: {body}"
# and GCS, S3 and Azure Blob all put an XML
# ``<Error><Code>...</Code><Message>...</Message></Error>`` in ``{body}``.
# Two shapes reach here: the backend-response form above ("non-2xx status
# code: 403 ...") and the retry layer's "Client error with status 403
# Forbidden: ...", which is how GCS client errors arrive.
_HTTP_STATUS_RE = re.compile(r"status(?: code:)?\s*(\d{3})\b")
_PROVIDER_CODE_RE = re.compile(r"<Code>([^<>]{1,64})</Code>")
# Atlan's ``/api/blobstorage`` proxy is a Kong plugin that rejects before the
# request reaches a bucket, in its own JSON shape
# ``{"code": 1005, "error": "...", "message": "..."}``. Parsed into the same
# ``provider_code`` so a consumer branches on one field whichever hop failed.
_JSON_PROVIDER_CODE_RE = re.compile(r'"code"\s*:\s*"?(\d{1,10})"?')

#: The blobstorage proxy's "I could not look your signing key up in Keycloak"
#: verdict: the gateway is unavailable, the credential is not rejected.
GATEWAY_INVALID_CLIENT_CODE = "1005"

# A Kong gateway instance that joins the load balancer before its route table
# has synced answers every request with ``404 {"message":"no Route matched with
# those values"}`` for a few seconds.
_GATEWAY_NO_ROUTE_TEXT = "no route matched"


def http_evidence(exc: BaseException) -> tuple[int | None, str | None]:
    """Return ``(http_status, provider_code)`` parsed from *exc*, best-effort.

    Both are ``None`` when *exc* is not a backend HTTP failure: a local store,
    a request timeout, a credential decode error. Never raises: a parse miss
    degrades the evidence attached to the failure, it must never replace the
    failure itself.
    """
    text = str(exc)
    status = _HTTP_STATUS_RE.search(text)
    provider = _PROVIDER_CODE_RE.search(text) or _JSON_PROVIDER_CODE_RE.search(text)
    return (
        int(status.group(1)) if status else None,
        provider.group(1) if provider else None,
    )


# --- the predicates -------------------------------------------------------------


def is_gateway_no_route(exc: BaseException) -> bool:
    """True for the gateway's transient "no Route matched" 404, and only that.

    Matched on the 404 *and* the gateway's own text, so a real missing bucket,
    container or key never matches.
    """
    if not isinstance(exc, Exception):
        return False
    http_status, _ = http_evidence(exc)
    return http_status == 404 and _GATEWAY_NO_ROUTE_TEXT in str(exc).lower()


def is_gateway_auth_unavailable(exc: BaseException) -> bool:
    """True when the blobstorage gateway could not verify the request.

    Routed on the ``(401, 1005)`` pair, never on either alone: any other 401
    from any other store is a real credential rejection.
    """
    http_status, provider_code = http_evidence(exc)
    return http_status == 401 and provider_code == GATEWAY_INVALID_CLIENT_CODE


def is_azure_container_missing(exc: BaseException) -> bool:
    """True when Azure reports the container, not the blob, as absent.

    Azure answers it with a plain 404, otherwise indistinguishable from a
    missing blob; v3 does not auto-create containers (v2's Dapr did).
    """
    msg = str(exc).lower()
    return "containernotfound" in msg or "the specified container does not exist" in msg


def _is_obstore_not_found_class(exc: BaseException) -> bool:
    """``obstore.exceptions.NotFoundError``, matched without importing obstore."""
    cls = type(exc)
    return cls.__name__ == "NotFoundError" and cls.__module__.startswith("obstore")


def is_not_found(exc: BaseException) -> bool:
    """True when *exc* says the requested object is missing.

    Recognises the built-in :class:`FileNotFoundError` (what current obstore
    raises), ``obstore.exceptions.NotFoundError`` (older obstore), and a
    substring fallback for a generic error that carries only the status in its
    text. The gateway's "no Route matched" 404 is excluded first: the key is
    not missing, the serving gateway instance has not loaded its routes.

    A missing bucket or container also answers 404, which this cannot tell
    apart from a missing object. A caller that must (the host's config read)
    confirms with a listing under the key: it succeeds, empty, when only the
    object is missing.
    """
    if is_gateway_no_route(exc):
        return False
    if isinstance(exc, FileNotFoundError) or _is_obstore_not_found_class(exc):
        return True
    msg = str(exc).lower()
    return (
        "not found" in msg
        or "no such file" in msg
        or "does not exist" in msg
        or "404" in msg
        or "key not found" in msg
    )


# --- the table --------------------------------------------------------------------


class StorageFailureKind(Enum):
    """What a failed store request means, as both stacks route it."""

    #: Azure answered 404 for the container: a deployment config problem.
    AZURE_CONTAINER_MISSING = "azure_container_missing"
    #: The blobstorage gateway's own Keycloak lookup failed (401 + ``1005``).
    GATEWAY_AUTH_UNAVAILABLE = "gateway_auth_unavailable"
    #: A gateway instance that has not loaded its routes (404 + "no Route matched").
    GATEWAY_NO_ROUTE = "gateway_no_route"
    #: The object is missing, or its bucket or container (see :func:`is_not_found`).
    NOT_FOUND = "not_found"
    #: Anything else: auth, network, throttling, a 5xx. The store could not answer.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StorageFailure:
    """One classified store failure and the evidence it was classified on."""

    kind: StorageFailureKind
    http_status: int | None
    provider_code: str | None


# Order is load-bearing: each rule is exact, so a broader one reaching a
# failure first could only lose information. The container rule precedes
# NOT_FOUND because its text ("does not exist") also matches that fallback.
_RULES: tuple[tuple[StorageFailureKind, Callable[[BaseException], bool]], ...] = (
    (StorageFailureKind.AZURE_CONTAINER_MISSING, is_azure_container_missing),
    (StorageFailureKind.GATEWAY_AUTH_UNAVAILABLE, is_gateway_auth_unavailable),
    (StorageFailureKind.GATEWAY_NO_ROUTE, is_gateway_no_route),
    (StorageFailureKind.NOT_FOUND, is_not_found),
)


def classify_storage_failure(exc: BaseException) -> StorageFailure:
    """Name the failure *exc* reports, with its parsed evidence."""
    http_status, provider_code = http_evidence(exc)
    for kind, matches in _RULES:
        if matches(exc):
            return StorageFailure(kind, http_status, provider_code)
    return StorageFailure(StorageFailureKind.UNAVAILABLE, http_status, provider_code)


# --- the gateway no-route retry ----------------------------------------------------

#: obstore's Rust client retries only 5xx/429/408, so without this the gateway's
#: first "no Route matched" answer fails the request even though the next
#: gateway instance would have accepted it. Worst case total sleep is
#: 1.5 x (5 + 10 + 20) = 52.5s: each delay is scaled by a jitter factor in
#: [0.5, 1.5] (``Jitter.SPREAD``) so a burst of requests that all hit a
#: scaling-up gateway does not retry in lockstep.
GATEWAY_NO_ROUTE_RETRY_DELAYS: tuple[float, ...] = (5.0, 10.0, 20.0)


async def past_gateway_no_route(
    op: str,
    key: str,
    call: Callable[[], Awaitable[_T]],
    *,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> _T:
    """Run one store request, retrying the gateway's "no Route matched" 404.

    Wrap the *request*, not a stream consumer: a retried ``call`` must start
    over cleanly. Every other failure propagates on the first attempt,
    unchanged.
    """

    def _log_retry(_attempt: int, _exc: Exception, delay: float) -> None:
        logger.warning(
            "Gateway answered 'no Route matched' on %s of key '%s'; retrying in %.1fs",
            op,
            key,
            delay,
        )

    return await retry_async(
        call,
        backoff=Backoff(steps=GATEWAY_NO_ROUTE_RETRY_DELAYS, jitter=Jitter.SPREAD),
        retry_if=is_gateway_no_route,
        on_retry=_log_retry,
        sleep=sleep,
    )
