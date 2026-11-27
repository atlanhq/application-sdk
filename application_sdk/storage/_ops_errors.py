"""How :mod:`application_sdk.storage.ops` turns a driver failure into a typed error.

The verdicts are the shared table's (:mod:`application_sdk._storage_classify`),
so the API host reads a failure the same way. What lives here is the worker's
half: the per-predicate wrappers ``ops`` branches on, and
:func:`_storage_error_for`, which picks the ``StorageError`` leaf and attaches
the evidence (``http_status``, ``provider_code``, ``target``) every branch
carries.

Private: import the predicates from here, and the public names from ``ops``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, TypedDict

from application_sdk import _storage_classify
from application_sdk.storage._classify import (
    RELOCATION_BUCKET,
    classify_access_error,
    is_precondition,
)

if TYPE_CHECKING:
    from obstore.store import ObjectStore


def _is_azure_container_not_found(exc: BaseException) -> bool:
    """Return True when *exc* indicates an Azure container does not exist.

    Azure Blob Storage returns HTTP 404 with error code ``ContainerNotFound``
    when a write targets a container that has never been created.  This is
    distinct from a missing *blob* (``BlobNotFound``) and needs a separate,
    actionable error message so operators know to pre-create the container.
    The rule is the shared table's
    (:func:`application_sdk._storage_classify.is_azure_container_missing`), so
    the API host reads the same failure the same way.
    """
    return _storage_classify.is_azure_container_missing(exc)


def _azure_container_not_found_message(key: str) -> str:
    """Return the standard user-facing message for a missing Azure container."""
    return (
        "Azure container does not exist — v3 does not auto-create "
        "containers (v2 Dapr did); pre-create the container before "
        f"running (failed key: '{key}')"
    )


def _is_not_found(exc: BaseException) -> bool:
    """Return True if the exception indicates a missing key.

    The shared table's rule
    (:func:`application_sdk._storage_classify.is_not_found`), which the API
    host uses too: the built-in :class:`FileNotFoundError` (current obstore),
    ``obstore.exceptions.NotFoundError`` (older obstore), and a substring
    fallback for a generic error that carries only the status in its text.
    Class-based detection runs first.

    The gateway's transient ``404 "no Route matched"`` is excluded before the
    substring fallback: the key is not missing, the gateway instance has not
    loaded its routes, so callers fall through to ``_storage_error_for`` and
    raise the retryable ``StorageGatewayNoRouteError`` instead of reporting
    the key absent.
    """
    return _storage_classify.is_not_found(exc)


def _is_local_dir_collision(exc: BaseException, store: ObjectStore, key: str) -> bool:
    """Return True when *key* names a directory in a local store, not an object.

    A :class:`~obstore.store.LocalStore` maps an object key onto a filesystem
    path, so a probe of a key that names a directory (e.g. the bare root marker
    ``"t"`` when ``t/`` holds children) does not surface as "not found": on
    Windows the stat raises ``GenericError("… Unable to open file …: Access is
    denied. (os error 5)")``, and a failed open can surface as "… Is a
    directory".  A key that resolves to a directory can never also exist as an
    object, so this is a directory collision — not a permission or I/O failure
    — and the caller contract here ("skip when the object is not there") is
    served by treating it as missing.

    Deliberately not a message match on its own; message text alone would
    reclassify a genuine failure that happens to be worded the same way (a
    non-local backend, or a local *file* that is unreadable).  Three gates, in
    order of authority:

    1. Not a ``LocalStore`` → never a directory collision.  Cloud backends have
       no directories; a 403 wording similar to Windows' stays fatal.
    2. A ``LocalStore`` with a prefix (what :func:`storage.factory.create_local_store`
       builds) → resolve the key and ask the filesystem.  ``is_dir()`` is the
       authoritative answer and needs no message parsing: it is ``False`` for an
       unreadable *file*, so a real permission failure still raises.
    3. A rootless ``LocalStore`` (keys are not resolvable to a path here) → fall
       back to the conjunctive message shape: an "unable to open file" prefix
       *and* a directory-stat suffix.  A plain ``ERROR_ACCESS_DENIED`` or a
       cloud 403 containing "access is denied" does not match.

    Permission failures must stay fatal in every case — see the ``StorageError``
    contract on :func:`delete` / :func:`exists`, and ``StoragePermissionError``
    in ``storage.preflight``.
    """
    from obstore.store import LocalStore  # noqa: PLC0415 — defensive: keep inline

    if not isinstance(store, LocalStore):
        return False

    root = store.prefix
    if root is not None:
        # PurePosixPath: obstore keys are always '/'-separated, whatever the OS.
        return Path(root, *PurePosixPath(key).parts).is_dir()

    msg = str(exc).lower()
    return "unable to open file" in msg and (
        "access is denied" in msg or "is a directory" in msg
    )


def _exc_class_name(exc: BaseException) -> str:
    """Return a stable short class name for structured-log error_class fields."""
    return type(exc).__name__


def _is_precondition(exc: BaseException) -> bool:
    """True when *exc* is an etag precondition failure (HTTP 412 / if_match miss).

    The rule is :func:`application_sdk.storage._classify.is_precondition`.
    """
    return is_precondition(exc)


# ``object_store`` formats every backend HTTP failure as
#   "Generic {store} error: Error performing {METHOD} {url} in {elapsed} -
#    Server returned non-2xx status code: {status} {reason}: {body}"
# and GCS, S3 and Azure Blob all put an XML
# ``<Error><Code>...</Code><Message>...</Message></Error>`` in ``{body}``.
# Parsing the two routing-relevant tokens out is what lets a consumer branch on
# a field: before FND-957 the status existed only inside the free-text cause,
# which the envelope's length cap usually truncated away before it arrived.


class _StorageEvidence(TypedDict):
    """The kwargs every branch of :func:`_storage_error_for` splats into a leaf.

    Typed rather than ``dict[str, Any]`` so pyright checks the splat against
    each ``__init__`` it reaches. Under ``Any`` that check is silently skipped,
    and the failure mode is the one this module exists to prevent: add a field
    here, miss one leaf's ``__init__``, and the ``TypeError`` surfaces at raise
    time — on the error path, under load, replacing the failure it was meant to
    describe.
    """

    key: str
    cause: Exception
    http_status: int | None
    provider_code: str | None
    target: str | None


def _obstore_http_evidence(exc: BaseException) -> tuple[int | None, str | None]:
    """Return ``(http_status, provider_code)`` parsed from *exc*, best-effort.

    The shared table's parser
    (:func:`application_sdk._storage_classify.http_evidence`). Both are
    ``None`` when *exc* is not a backend HTTP failure. Never raises.
    """
    return _storage_classify.http_evidence(exc)


# Scheme per obstore store class, for the ``target`` evidence field.
_STORE_SCHEMES: dict[str, str] = {
    "GCSStore": "gs",
    "S3Store": "s3",
    "AzureStore": "abfs",
    "LocalStore": "file",
}


# Identity keys that are safe to read off a store's ``config``. That dict also
# carries live credentials -- an ``S3Store`` exposes ``secret_access_key`` and
# an ``AzureStore`` exposes ``account_key``, both in plaintext -- so this is an
# allowlist and must stay one. Never put ``store.config`` itself on an error.
_STORE_IDENTITY_KEYS: tuple[str, ...] = ("bucket", "container_name")


def _store_target(store: ObjectStore, key: str) -> str | None:
    """Return a ``scheme://container/path`` identity for the failing request.

    This is the ``target`` evidence field -- "what were we talking to". It is
    assembled from the store's class and one allowlisted identity key, never
    from the request URL and never from ``store.config`` wholesale: a signed
    URL carries ``X-Goog-Signature``, which ``redact_secrets`` does not strip,
    and the config dict holds credentials outright.

    Best-effort by design. Returns ``None`` rather than raising, because this
    only decorates a failure and must never be able to replace it. (FND-957)
    """
    try:
        scheme = _STORE_SCHEMES.get(type(store).__name__, "objectstore")
        container: str | None = None
        config = getattr(store, "config", None)
        if config is not None and hasattr(config, "get"):
            for candidate in _STORE_IDENTITY_KEYS:
                value = config.get(candidate)
                if isinstance(value, str) and value:
                    container = value
                    break
        prefix = getattr(store, "prefix", None)
        path = f"{prefix}/{key}" if isinstance(prefix, str) and prefix else key
        return f"{scheme}://{container}/{path}" if container else f"{scheme}:///{path}"
    # conformance: ignore[E004] the identity is decoration on an error message; the error itself is still raised
    except Exception:  # noqa: BLE001 — identity is decoration on an error message that is still raised
        return None


def _storage_error_for(
    exc: Exception,
    key: str,
    message: str,
    store: ObjectStore | None = None,
) -> Exception:
    """Build the typed storage error for a failed write.

    Only four conditions reclassify: a missing Azure container, a blobstorage
    gateway that could not reach its identity provider, a gateway instance whose
    routes have not synced, and a bucket mid-relocation. Everything else — including a bare ``PreconditionFailed``
    — is a retryable ``StorageError``. The parsed ``http_status`` and
    ``provider_code`` are otherwise *evidence, not routing*: they ride on the
    envelope so a consumer holding context the SDK lacks can branch on them,
    and so a future case can argue for its own leaf from data rather than
    inference. The gateway branch is the first case to have made that argument,
    and it routes on the ``(401, 1005)`` pair specifically — not on either
    field alone.

    Order is load-bearing, but not for the retry verdict — every reclassifying
    branch is retryable. It decides which ``code`` and which
    ``suggested_action`` an operator sees, and those are wrong if a broader
    rule matches first. Do not reorder without re-reading the notes at each
    branch.

    Every branch carries the same evidence, so a consumer reads ``http_status``,
    ``provider_code`` and ``target`` off the envelope regardless of which leaf
    was chosen. (FND-957)
    """
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageBucketRelocationError,
        StorageConfigError,
        StorageError,
        StorageGatewayAuthUnavailableError,
        StorageGatewayNoRouteError,
    )

    # The shared table names the failure; the API host routes the same verdict
    # onto an HTTP answer (handler/object_routes.py), so the two stacks cannot
    # drift on what a gateway or container failure is.
    failure = _storage_classify.classify_storage_failure(exc)
    kind = failure.kind
    http_status, provider_code = failure.http_status, failure.provider_code
    evidence: _StorageEvidence = {
        "key": key,
        "cause": exc,
        "http_status": http_status,
        "provider_code": provider_code,
        "target": _store_target(store, key) if store is not None else None,
    }

    # Checked before the status routing because its remediation is a config
    # change rather than a retry, and Azure reports it as a plain 404 that
    # would otherwise be indistinguishable from a missing blob. It carries the
    # same evidence as every other branch: the container name is in ``target``
    # and the 404/ContainerNotFound pair is what tells an operator the
    # container, not the object, is what is absent.
    if kind is _storage_classify.StorageFailureKind.AZURE_CONTAINER_MISSING:
        return StorageConfigError(_azure_container_not_found_message(key), **evidence)

    # Checked before the relocation branch and the generic fallthrough for the
    # same reason the Azure branch is: the pair it matches on is exact, so a
    # broader rule reaching it first can only lose information. Matching on
    # BOTH the 401 and the gateway's own ``1005`` is what keeps a real
    # credential rejection — any other 401 from any other store — out of this
    # leaf; ``1005`` is emitted by exactly one hop in the fleet.
    #
    # The retry verdict does not change here (StorageError is already
    # retryable). What changes is what an operator is told: without this leaf
    # the envelope says "lacked valid authentication credentials", which points
    # at the connector's static Keycloak client id/secret — credentials the SDR
    # preflight already proved good against this same store at startup. The
    # actual condition is the proxy's Keycloak lookup failing, and no
    # credential change fixes it. (FND-2076)
    if kind is _storage_classify.StorageFailureKind.GATEWAY_AUTH_UNAVAILABLE:
        return StorageGatewayAuthUnavailableError(
            f"{message}: the object-store gateway could not verify the request "
            "signature because its own identity-provider lookup failed — this "
            "is the gateway being unavailable, not a rejected credential",
            suggested_action=(
                "Do not rotate or re-issue the store's credentials: they are a "
                "static Keycloak client id/secret that the SDR preflight probe "
                "already exercised against this store at startup. Retry the "
                "run; if the condition persists beyond a few minutes, the "
                "tenant's blobstorage gateway cannot reach Keycloak and the "
                "fix is platform-side."
            ),
            **evidence,
        )

    # Checked before the relocation branch and the generic fallthrough for the
    # same reason as the gateway-auth branch: the match is exact (a 404 AND the
    # gateway's own "no Route matched" text), so a broader rule reaching it first
    # can only lose information. A plain 404 never matches, which keeps a real
    # missing bucket or container out of this leaf. Retryable either way; what
    # changes is the code and the hint, so an operator is not sent looking for a
    # missing key or bucket when the gateway merely has not loaded its routes.
    if kind is _storage_classify.StorageFailureKind.GATEWAY_NO_ROUTE:
        return StorageGatewayNoRouteError(
            f"{message}: the object-store gateway answered 404 because the "
            "serving instance has not loaded its routes yet — the object and "
            "bucket are not missing",
            suggested_action=(
                "Wait and retry: a gateway instance that just joined the load "
                "balancer loads its routes within seconds. Nothing in the app "
                "or its credentials needs to change; if it persists beyond a "
                "few minutes, the gateway's route sync is failing and the fix "
                "is platform-side."
            ),
            **evidence,
        )

    # Relocation is checked BEFORE the generic fallthrough below, and the order
    # is load-bearing rather than stylistic — though not for the reason it might
    # look. Both outcomes are retryable, so nothing here changes a retry
    # decision. What the order buys is the *specific* code
    # (DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION, which the preflight gate
    # stamps for the same condition) and the remediation hint telling an
    # operator to wait the move out. Fall through to the generic StorageError
    # first and both are silently lost: the failure still retries, but nobody
    # can tell a relocation from any other storage blip.
    #
    # The detection is deliberately the preflight gate's, not a second one: its
    # rule requires BOTH the "precondition" and "relocation" tokens, so a plain
    # etag/if-match 412 can never be misread as a relocation and pick up a
    # "wait for the move to finish" hint for a move that is not happening.
    bucket, hint = classify_access_error(exc)
    if bucket == RELOCATION_BUCKET:
        return StorageBucketRelocationError(
            f"{message}: the destination bucket is being relocated",
            suggested_action=hint,
            **evidence,
        )
    # Nothing else reclassifies. A 403 or 404 is genuinely ambiguous here:
    # ``upload_file`` accepts an explicit store, so the same status arrives both
    # from the deployment's own artifact store (audience APP_OWNER) and from a
    # caller-supplied store pointing at a customer bucket (audience USER).
    # Routing audience off the status would put customer-facing attribution on
    # our own infrastructure half the time.
    #
    # A bare ``PreconditionFailed`` that is *not* a relocation deliberately
    # stays a retryable ``StorageError`` too. The one real instance we have of
    # that status turned out to be a relocation — temporary and self-healing —
    # so the prior on "unclassified precondition means permanent" is weak, and
    # a wrongly-permanent verdict fails a run that would have recovered. The
    # status and provider code still reach ``evidence``, which is what a future
    # non-relocation case would need to make the argument for its own leaf.
    return StorageError(message, **evidence)
