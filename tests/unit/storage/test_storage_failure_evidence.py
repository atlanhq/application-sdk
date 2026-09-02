"""Storage failure envelope: typed evidence and backend-verdict classification.

Regression cover for FND-957. Three things went wrong together on a production
GCS write failure and each is asserted here independently:

1. ``service``/``target``/``network_error`` are fields of the categorical
   parent, so they appear in ``evidence`` whether or not anyone sets them. No
   ``Storage*`` class set them, so every storage failure the SDK had ever
   reported shipped them as ``null``.
2. The backend's HTTP status and error code existed only inside the free-text
   cause, so nothing downstream could branch on them.
3. ``retryable`` and ``category`` came from whichever class the raise site
   picked. The write path classified one condition (a missing Azure container),
   so a deterministic 400, a 403 and a genuine 503 were indistinguishable —
   all ``DEPENDENCY_UNAVAILABLE``, all ``retryable=true``.

The message shapes below are the real ``object_store`` formats, not
paraphrases: the classifier reads them, so a wording drift upstream must fail
these tests rather than silently degrade the envelope.
"""

from __future__ import annotations

import pytest
from obstore.store import GCSStore, S3Store

from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.storage.errors import (
    StorageBucketRelocationError,
    StorageConfigError,
    StorageError,
    StorageIntegrityError,
    StorageNotFoundError,
    StoragePermissionError,
)
from application_sdk.storage.ops import _obstore_http_evidence, _storage_error_for


class _GenericError(Exception):
    """Stand-in for ``obstore.exceptions.GenericError``.

    The classifier matches on message shape, not on class, precisely so it
    keeps working across obstore versions — so a local stand-in is a faithful
    test double here rather than a shortcut.
    """


def _gcs_http_error(
    status: str, provider_code: str, *, key: str = "artifacts/t.json"
) -> _GenericError:
    """Build an obstore-shaped GCS HTTP failure for *status* / *provider_code*."""
    url = f"https://storage.googleapis.com/example-bucket/{key.replace('/', '%2F')}"
    return _GenericError(
        f"Generic GCS error: Error performing PUT {url} in 53.300492ms - "
        f"Server returned non-2xx status code: {status}: "
        f"<Error><Code>{provider_code}</Code><Message>detail</Message></Error>"
        '\n\nDebug source:\nGeneric {\n    store: "GCS",\n}'
    )


# ── 1. evidence is populated, not structurally null ─────────────────────────


def test_storage_error_sets_service_so_evidence_is_not_all_null() -> None:
    fd = StorageError("write failed", key="artifacts/t.json").to_failure_details()
    assert fd.evidence["service"] == "object_store"
    assert fd.evidence["key"] == "artifacts/t.json"


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (StorageError, {"key": "artifacts/t.json"}),
        (StorageNotFoundError, {"key": "artifacts/t.json"}),
        (StoragePermissionError, {"key": "artifacts/t.json"}),
        (StorageConfigError, {"key": "artifacts/t.json"}),
        (StorageBucketRelocationError, {"key": "artifacts/t.json"}),
        (StorageIntegrityError, {"key": "artifacts/t.json"}),
    ],
)
def test_every_storage_class_sets_service(factory: type, kwargs: dict) -> None:
    """The bug was fleet-wide, so the guard has to be too.

    Each class hand-writes its own ``__init__``; that duplication is what let
    the field go unset everywhere. Parametrising over the classes is what
    catches a new subclass that forgets ``_init_storage_evidence``.
    """
    fd = factory("failed", **kwargs).to_failure_details()
    assert fd.evidence["service"] == "object_store"


def test_target_is_carried_when_supplied() -> None:
    fd = StorageError(
        "write failed",
        key="artifacts/t.json",
        target="gs://example-bucket/artifacts/t.json",
    ).to_failure_details()
    assert fd.evidence["target"] == "gs://example-bucket/artifacts/t.json"


# ── 2. the backend verdict reaches the wire as fields ───────────────────────


def test_http_status_and_provider_code_are_parsed_from_the_driver_error() -> None:
    status, provider_code = _obstore_http_evidence(
        _gcs_http_error("400 Bad Request", "PreconditionFailed")
    )
    assert status == 400
    assert provider_code == "PreconditionFailed"


def test_http_evidence_is_none_for_a_non_http_failure() -> None:
    """A timeout or credential error has no status; the parse must not invent one."""
    status, provider_code = _obstore_http_evidence(
        _GenericError("Generic error: Operation timed out after 180s")
    )
    assert status is None
    assert provider_code is None


def test_status_and_provider_code_land_in_evidence() -> None:
    err = _storage_error_for(
        _gcs_http_error("400 Bad Request", "PreconditionFailed"),
        "artifacts/t.json",
        "upload failed",
    )
    fd = err.to_failure_details()
    assert fd.evidence["http_status"] == 400
    assert fd.evidence["provider_code"] == "PreconditionFailed"


# ── 3. the retry verdict follows the backend, not the raise site ────────────


def test_a_non_relocation_precondition_stays_a_retryable_storage_error() -> None:
    """No leaf of its own — deliberately, after review.

    An earlier revision routed any ``PreconditionFailed`` to a dedicated
    non-retryable leaf. The single real instance of that status we
    have turned out to be a bucket relocation (see the relocation test below):
    temporary, platform-side and self-healing. That made the prior on
    "unclassified precondition means permanent" weak, and a wrongly-permanent
    verdict fails a run that would have recovered on its own.

    The status and provider code still reach ``evidence``, which is exactly what
    a future non-relocation case would need to argue for its own leaf.
    """
    err = _storage_error_for(
        _gcs_http_error("400 Bad Request", "PreconditionFailed"),
        "artifacts/t.json",
        "upload failed",
    )
    assert type(err) is StorageError
    fd = err.to_failure_details()
    assert fd.category is FailureCategory.DEPENDENCY_UNAVAILABLE
    assert fd.retryable is True
    assert fd.evidence["http_status"] == 400
    assert fd.evidence["provider_code"] == "PreconditionFailed"


def test_azure_condition_not_met_also_stays_retryable() -> None:
    """A canonical 412 gets the same treatment, and keeps its evidence."""
    err = _storage_error_for(
        _GenericError(
            "Generic MicrosoftAzure error: Error performing PUT "
            "https://acct.blob.core.windows.net/c/k in 40ms - Server returned non-2xx "
            "status code: 412 Precondition Failed: "
            "<Error><Code>ConditionNotMet</Code></Error>"
        ),
        "artifacts/t.json",
        "upload failed",
    )
    assert type(err) is StorageError
    fd = err.to_failure_details()
    assert fd.retryable is True
    assert fd.evidence["http_status"] == 412
    assert fd.evidence["provider_code"] == "ConditionNotMet"


def test_a_real_outage_stays_retryable_even_when_the_key_contains_412() -> None:
    """The reason the classifier does not reuse ``_is_precondition``.

    That helper falls back to ``"412" in msg``, and object keys and run GUIDs
    appear in every obstore message — roughly 1 artifact key in 94 contains the
    digits 412. Routing on the substring would turn a transient outage into a
    permanent failure and stop the orchestrator retrying something that would
    have succeeded.
    """
    key = "artifacts/apps/example-app/runs/01a0412f-5c0c-7195-8d7e-4174674032eb/t.json"
    err = _storage_error_for(
        _gcs_http_error("503 Service Unavailable", "ServiceUnavailable", key=key),
        key,
        "upload failed",
    )
    assert type(err) is StorageError
    fd = err.to_failure_details()
    assert fd.category is FailureCategory.DEPENDENCY_UNAVAILABLE
    assert fd.retryable is True
    assert fd.evidence["http_status"] == 503


def test_bucket_relocation_wins_over_the_precondition_rule() -> None:
    """Ordering is safety-critical, not stylistic.

    A bucket mid-relocation is rejected with the same HTTP 400 /
    ``PreconditionFailed`` pair that would otherwise route to
    a plain retryable storage failure — but the verdicts differ. A relocation
    is temporary and platform-side: it clears when the move finishes, so it must
    stay retryable and PLATFORM-attributed. If the precondition rule won, a
    self-healing condition would be marked permanently failed and billed to the
    connector.
    """
    exc = _GenericError(
        "Generic GCS error: Error performing POST "
        "https://storage.googleapis.com/example-bucket/k?uploads= in 53ms - "
        "Server returned non-2xx status code: 400 Bad Request: "
        "<Error><Code>PreconditionFailed</Code><Message>Invalid precondition "
        "due to bucket relocation in progress</Message></Error>"
    )
    err = _storage_error_for(exc, "artifacts/t.json", "upload failed")
    fd = err.to_failure_details()
    assert type(err).__name__ == "StorageBucketRelocationError"
    assert fd.retryable is True
    assert fd.audience is Audience.PLATFORM
    assert fd.suggested_action is not None


def test_a_plain_precondition_is_not_mistaken_for_a_relocation() -> None:
    """The converse guard: the relocation rule needs BOTH tokens.

    An etag/if-match 412 says "precondition" and nothing about a relocation, so
    it must NOT pick up the relocation code or its remediation hint — telling an
    operator to wait out a move that is not happening is worse than saying
    nothing.
    """
    exc = _GenericError(
        "Generic MicrosoftAzure error: Error performing PUT "
        "https://acct.blob.core.windows.net/c/k in 40ms - Server returned "
        "non-2xx status code: 412 Precondition Failed: "
        "<Error><Code>ConditionNotMet</Code></Error>"
    )
    err = _storage_error_for(exc, "artifacts/t.json", "upload failed")
    assert type(err) is StorageError
    fd = err.to_failure_details()
    assert fd.code == "DEPENDENCY_UNAVAILABLE_STORAGE"
    assert fd.suggested_action is None


@pytest.mark.parametrize(
    ("status", "provider_code"),
    [("403 Forbidden", "AccessDenied"), ("404 Not Found", "NoSuchKey")],
)
def test_403_and_404_carry_the_status_without_reclassifying(
    status: str, provider_code: str
) -> None:
    """The status reaches evidence, but the leaf and audience are unchanged.

    Reclassifying these would silently move attribution. ``upload_file`` takes
    an explicit store, so a 403 arrives both from the deployment's own artifact
    store (APP_OWNER) and from a caller-supplied store pointing at a customer
    bucket (USER) — the status alone cannot tell those apart, and guessing would
    put customer-facing attribution on our own infrastructure half the time. A
    consumer that *does* hold the missing context can branch on
    ``evidence["http_status"]``.
    """
    err = _storage_error_for(
        _gcs_http_error(status, provider_code), "artifacts/t.json", "upload failed"
    )
    assert type(err) is StorageError
    fd = err.to_failure_details()
    assert fd.evidence["http_status"] == int(status.split()[0])
    assert fd.evidence["provider_code"] == provider_code
    assert fd.category is FailureCategory.DEPENDENCY_UNAVAILABLE
    assert fd.audience is Audience.PLATFORM


# ── target: identity of the store, never its credentials ────────────────────


def test_target_is_derived_from_the_resolved_store() -> None:
    err = _storage_error_for(
        _gcs_http_error("500 Internal Server Error", "InternalError"),
        "artifacts/t.json",
        "upload failed",
        GCSStore("example-bucket"),
    )
    assert err.to_failure_details().evidence["target"] == (
        "gs://example-bucket/artifacts/t.json"
    )


def test_target_never_leaks_store_credentials() -> None:
    """``store.config`` holds live secrets, so ``target`` must be an allowlist.

    An ``S3Store`` exposes ``secret_access_key`` and an ``AzureStore`` exposes
    ``account_key`` in plaintext. Serialising the config — or the request URL,
    whose ``X-Goog-Signature`` ``redact_secrets`` does not strip — would put
    credential material on the wire.
    """
    store = S3Store(
        "example-bucket",
        region="us-east-1",
        access_key_id="AKIAEXAMPLE",
        secret_access_key="test-secret-do-not-emit",
    )
    # Guard the premise: if obstore stops exposing the secret this test is
    # still correct, but it is no longer testing what it claims to.
    assert store.config.get("secret_access_key") == "test-secret-do-not-emit"

    fd = _storage_error_for(
        _gcs_http_error("500 Internal Server Error", "InternalError"),
        "artifacts/t.json",
        "upload failed",
        store,
    ).to_failure_details()

    serialised = fd.model_dump_json()
    assert "test-secret-do-not-emit" not in serialised
    assert "AKIAEXAMPLE" not in serialised
    assert fd.evidence["target"] == "s3://example-bucket/artifacts/t.json"


def test_target_is_none_when_no_store_is_available() -> None:
    """Absent identity must degrade to ``None``, never to a raise."""
    err = _storage_error_for(
        _gcs_http_error("500 Internal Server Error", "InternalError"),
        "artifacts/t.json",
        "upload failed",
    )
    assert err.to_failure_details().evidence["target"] is None


def test_unclassifiable_failure_stays_a_retryable_dependency_error() -> None:
    """No status parsed means no verdict to act on — keep the old, safe default.

    Guessing here is the expensive direction: a wrongly non-retryable transient
    failure fails a run that would have healed itself.
    """
    err = _storage_error_for(
        _GenericError("Generic error: Operation timed out after 180s"),
        "artifacts/t.json",
        "upload failed",
    )
    assert type(err) is StorageError
    assert err.to_failure_details().retryable is True


def test_azure_missing_container_still_wins_over_status_routing() -> None:
    """Its remediation is a config change, not a retry, so it is checked first."""
    err = _storage_error_for(
        _GenericError(
            "Generic MicrosoftAzure error: ContainerNotFound: The specified "
            "container does not exist"
        ),
        "artifacts/t.json",
        "upload failed",
    )
    assert isinstance(err, StorageConfigError)


def test_azure_missing_container_carries_the_same_evidence() -> None:
    """Every branch carries evidence, including the one that short-circuits.

    This branch previously returned a bare message: no ``key``, no ``cause``
    (so no ``cause_repr`` at all), no ``target``. The docstring claimed
    otherwise, and an operator debugging a missing container got less than one
    debugging a generic failure. (FND-957 review)
    """
    exc = _GenericError(
        "Generic MicrosoftAzure error: Error performing PUT "
        "https://acct.blob.core.windows.net/c/k in 40ms - Server returned "
        "non-2xx status code: 404 Not Found: <Error><Code>ContainerNotFound"
        "</Code><Message>The specified container does not exist</Message></Error>"
    )
    fd = _storage_error_for(
        exc, "artifacts/t.json", "upload failed", GCSStore("example-bucket")
    ).to_failure_details()

    assert fd.evidence["key"] == "artifacts/t.json"
    assert fd.evidence["service"] == "object_store"
    assert fd.evidence["target"] == "gs://example-bucket/artifacts/t.json"
    assert fd.evidence["http_status"] == 404
    assert fd.evidence["provider_code"] == "ContainerNotFound"
    assert fd.cause_repr is not None and "ContainerNotFound" in fd.cause_repr


# ── the human-readable form keeps the new fields ────────────────────────────


def test_str_surfaces_the_backend_verdict() -> None:
    err = StorageError(
        "upload failed",
        key="artifacts/t.json",
        http_status=400,
        provider_code="PreconditionFailed",
    )
    rendered = str(err)
    assert "http_status=400" in rendered
    assert "provider_code=PreconditionFailed" in rendered
