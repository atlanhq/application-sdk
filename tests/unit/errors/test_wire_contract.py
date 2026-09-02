"""The typed failure envelope is a cross-repo contract — pin it here.

See ``docs/standards/cross-repo-contracts.md``. The Automation Engine attributes
a failed run from ``ApplicationError.details[0]``, and connector-pulse buckets
runs by ``failure_category`` / ``failure_code`` for its failure boards. Both are
suites that cannot vote in this repo's CI.

The tests over the individual error classes do not pin this: they construct a
leaf and read attributes off it, so a rename of a wire field, a re-spelling of an
enum member, or a change to the category/code relationship would keep them green
while reddening a consumer. These assert the *serialised* shape instead, at the
boundary a consumer actually reads. (FND-957)
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from application_sdk.errors.base import AppError
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import AuthError
from application_sdk.storage.errors import (
    StorageBucketRelocationError,
    StorageEmptyUploadError,
    StorageError,
    StorageNotFoundError,
)


class TestFailureEnvelopeWireContract:
    """``FailureDetails`` as consumers outside this repo receive it."""

    def test_top_level_field_names_are_stable(self) -> None:
        """Renaming any of these is a breaking change for a consumer.

        Asserted as an exact set, not a subset: a *removed* field breaks a
        reader just as surely as a renamed one, and a subset check would miss it.
        """
        payload = json.loads(
            AuthError(message="bad creds").to_failure_details().model_dump_json()
        )
        assert set(payload) == {
            "category",
            "code",
            "retryable",
            "audience",
            "message",
            "suggested_action",
            "evidence",
            "app_name",
            "run_id",
            "cause_repr",
        }

    @pytest.mark.parametrize(
        ("factory", "wire"),
        [
            (lambda: AuthError(message="x"), "AUTH"),
            (lambda: StorageError("x", key="k"), "DEPENDENCY_UNAVAILABLE"),
            (lambda: StorageNotFoundError("x", key="k"), "NOT_FOUND"),
        ],
    )
    def test_category_serialises_to_the_expected_wire_string(
        self, factory: Callable[[], AppError], wire: str
    ) -> None:
        """Consumers match these strings literally, so the spelling is the contract.

        Read off the *serialised* payload, not off ``member.name``. Every
        ``FailureCategory`` member currently has ``name == value``, so an
        assertion against ``.name`` stays green even if the serialiser is
        switched to ``.value`` — and then breaks silently the first time a
        member's name and value diverge. Going through ``model_dump_json`` is
        what actually pins the wire.
        """
        payload = json.loads(factory().to_failure_details().model_dump_json())
        assert payload["category"] == wire

    @pytest.mark.parametrize(
        ("factory", "wire"),
        [
            (lambda: AuthError(message="x"), "USER"),
            (lambda: StorageError("x", key="k"), "PLATFORM"),
            (lambda: StorageEmptyUploadError("x"), "APP_OWNER"),
        ],
    )
    def test_audience_serialises_to_the_expected_wire_string(
        self, factory: Callable[[], AppError], wire: str
    ) -> None:
        """``Audience`` is contract for the same reason ``FailureCategory`` is.

        The doc entry names both, and this used to assert only that the value
        was *some* member name — which passes for any of the three, so it
        pinned neither the spelling nor the serialisation mode. Every member
        has ``name == value`` here too, so membership could not have told a
        ``.name`` serialiser from a ``.value`` one. Pin the exact string per
        leaf, off the serialised payload, and cover all three members: a
        renamed member now reddens here instead of at a consumer.
        """
        payload = json.loads(factory().to_failure_details().model_dump_json())
        assert payload["audience"] == wire

    def test_every_audience_member_is_covered_by_the_pins_above(self) -> None:
        """Guard against a fourth member arriving unpinned.

        The parametrisation above spells out one leaf per ``Audience`` member.
        Adding a member without adding a case would leave a wire value nothing
        asserts, and the gap would be invisible — every existing test stays
        green. This fails the moment the enum grows.
        """
        assert {a.name for a in Audience} == {"USER", "PLATFORM", "APP_OWNER"}

    def test_category_is_coarse_and_code_is_the_specific_cause(self) -> None:
        """The relationship a consumer must not collapse.

        Two different storage failures share one category and differ only by
        code. A consumer keying a customer-facing attribution on ``category``
        alone therefore mis-attributes as soon as any app adds a leaf in that
        category — the defect tracked in FND-1140.
        """
        relocation = StorageBucketRelocationError(
            "bucket relocating", key="artifacts/k.json"
        ).to_failure_details()
        generic = StorageError(
            "unavailable", key="artifacts/k.json"
        ).to_failure_details()

        # Identical category. A consumer reading only `category` cannot tell a
        # transient store outage from a bucket mid-relocation, which have
        # different operator responses and different remediation hints.
        assert relocation.category is FailureCategory.DEPENDENCY_UNAVAILABLE
        assert generic.category is FailureCategory.DEPENDENCY_UNAVAILABLE
        assert relocation.code == "DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION"
        assert generic.code == "DEPENDENCY_UNAVAILABLE_STORAGE"
        assert relocation.code != generic.code

    def test_retryable_becomes_temporals_non_retryable(self) -> None:
        """``retryable`` is the retry decision, not a label.

        ``_to_application_error`` sets ``non_retryable=not effective_retryable``,
        so flipping this for an existing failure class changes production retry
        behaviour. Pinned because the inversion is easy to drop in a refactor and
        nothing else in this repo would notice.
        """
        from application_sdk.execution._temporal.activities import _to_application_error

        permanent = _to_application_error(
            StorageNotFoundError("missing", key="artifacts/k.json")
        )
        transient = _to_application_error(
            StorageError("unavailable", key="artifacts/k.json")
        )
        assert permanent.non_retryable is True
        assert transient.non_retryable is False

    def test_storage_evidence_keys_reach_the_wire(self) -> None:
        """The fields a consumer branches on instead of parsing the message."""
        payload = json.loads(
            StorageBucketRelocationError(
                "bucket relocating",
                key="artifacts/k.json",
                http_status=400,
                provider_code="PreconditionFailed",
                target="gs://example-bucket/artifacts/k.json",
            )
            .to_failure_details()
            .model_dump_json()
        )
        evidence = payload["evidence"]
        assert evidence["service"] == "object_store"
        assert evidence["target"] == "gs://example-bucket/artifacts/k.json"
        assert evidence["key"] == "artifacts/k.json"
        assert evidence["http_status"] == 400
        assert evidence["provider_code"] == "PreconditionFailed"

    def test_envelope_round_trips_through_json(self) -> None:
        """Consumers read this after a Temporal payload round-trip, not in-process."""
        from pydantic import TypeAdapter

        from application_sdk.errors.wire import FailureDetails

        original = StorageBucketRelocationError(
            "bucket relocating", key="artifacts/k.json", http_status=400
        ).to_failure_details()
        restored = TypeAdapter(FailureDetails).validate_json(original.model_dump_json())

        assert restored.category is FailureCategory.DEPENDENCY_UNAVAILABLE
        assert restored.code == "DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION"
        assert restored.retryable is True
        assert restored.evidence["http_status"] == 400
