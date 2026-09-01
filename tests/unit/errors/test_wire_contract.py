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

import pytest

from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import AuthError
from application_sdk.storage.errors import StorageError, StoragePreconditionError


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
        ("member", "wire"),
        [
            (FailureCategory.PRECONDITION, "PRECONDITION"),
            (FailureCategory.DEPENDENCY_UNAVAILABLE, "DEPENDENCY_UNAVAILABLE"),
            (FailureCategory.AUTH, "AUTH"),
        ],
    )
    def test_category_serialises_by_member_name(
        self, member: FailureCategory, wire: str
    ) -> None:
        """Consumers match these strings literally, so the spelling is the contract.

        ``FailureCategory`` serialises by member *name*, which means renaming a
        member is a wire change even though it reads as a local refactor.
        """
        assert member.name == wire

    def test_audience_serialises_by_member_name(self) -> None:
        payload = json.loads(
            AuthError(message="x").to_failure_details().model_dump_json()
        )
        assert payload["audience"] in {a.name for a in Audience}

    def test_category_is_coarse_and_code_is_the_specific_cause(self) -> None:
        """The relationship a consumer must not collapse.

        Two different storage failures share one category and differ only by
        code. A consumer keying a customer-facing attribution on ``category``
        alone therefore mis-attributes as soon as any app adds a leaf in that
        category — the defect tracked in FND-1140.
        """
        precondition = StoragePreconditionError(
            "refused", key="artifacts/k.json"
        ).to_failure_details()
        generic = StorageError(
            "unavailable", key="artifacts/k.json"
        ).to_failure_details()

        assert precondition.category is FailureCategory.PRECONDITION
        assert precondition.code == "PRECONDITION_STORAGE"
        assert generic.category is FailureCategory.DEPENDENCY_UNAVAILABLE
        assert generic.code == "DEPENDENCY_UNAVAILABLE_STORAGE"
        # Same subsystem, same audience-relevant surface, different verdicts:
        # only `code` separates them.
        assert precondition.code != generic.code

    def test_retryable_becomes_temporals_non_retryable(self) -> None:
        """``retryable`` is the retry decision, not a label.

        ``_to_application_error`` sets ``non_retryable=not effective_retryable``,
        so flipping this for an existing failure class changes production retry
        behaviour. Pinned because the inversion is easy to drop in a refactor and
        nothing else in this repo would notice.
        """
        from application_sdk.execution._temporal.activities import _to_application_error

        permanent = _to_application_error(
            StoragePreconditionError("refused", key="artifacts/k.json")
        )
        transient = _to_application_error(
            StorageError("unavailable", key="artifacts/k.json")
        )
        assert permanent.non_retryable is True
        assert transient.non_retryable is False

    def test_storage_evidence_keys_reach_the_wire(self) -> None:
        """The fields a consumer branches on instead of parsing the message."""
        payload = json.loads(
            StoragePreconditionError(
                "refused",
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

        original = StoragePreconditionError(
            "refused", key="artifacts/k.json", http_status=400
        ).to_failure_details()
        restored = TypeAdapter(FailureDetails).validate_json(original.model_dump_json())

        assert restored.category is FailureCategory.PRECONDITION
        assert restored.code == "PRECONDITION_STORAGE"
        assert restored.retryable is False
        assert restored.evidence["http_status"] == 400
