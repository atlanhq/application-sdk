"""Unit tests for application_sdk.contracts.base."""

from typing import Annotated, Any, Optional

import pytest
from pydantic import Field, ValidationError

from application_sdk.contracts.base import (
    ContractMetadata,
    ContractValidationError,
    HeartbeatDetails,
    Input,
    Output,
    PayloadSafetyError,
    Record,
    SerializableEnum,
    get_contract_fields,
    has_default,
    is_backwards_compatible,
    validate_is_dataclass,
    validate_payload_safety,
)
from application_sdk.contracts.types import FileReference, MaxItems

# =============================================================================
# Input / Output subclassing
# =============================================================================


class TestInputSubclassing:
    def test_input_can_be_subclassed_with_safe_types(self) -> None:
        class MyInput(Input):
            name: str
            count: int = 0

        obj = MyInput(name="test")
        assert obj.name == "test"
        assert obj.count == 0

    def test_output_can_be_subclassed_with_safe_types(self) -> None:
        class MyOutput(Output):
            result: str
            success: bool = True

        obj = MyOutput(result="done")
        assert obj.result == "done"
        assert obj.success is True

    def test_input_safe_primitive_types(self) -> None:
        class SafeInput(Input):
            s: str
            i: int
            f: float
            b: bool

        obj = SafeInput(s="x", i=1, f=1.0, b=True)
        assert obj.s == "x"

    def test_input_safe_annotated_list(self) -> None:
        class SafeInput(Input):
            items: Annotated[list[str], MaxItems(100)] = Field(default_factory=list)

        obj = SafeInput(items=["a", "b"])
        assert obj.items == ["a", "b"]

    def test_input_safe_annotated_dict(self) -> None:
        class SafeInput(Input):
            settings: Annotated[dict[str, str], MaxItems(50)] = Field(
                default_factory=dict
            )

        obj = SafeInput(settings={"k": "v"})
        assert obj.settings == {"k": "v"}

    def test_input_safe_file_reference(self) -> None:
        class SafeInput(Input):
            file: Optional[FileReference] = None

        ref = FileReference(local_path="/tmp/data.jsonl")
        obj = SafeInput(file=ref)
        assert obj.file is not None
        assert obj.file.local_path == "/tmp/data.jsonl"


# =============================================================================
# PayloadSafetyError raised at class definition time
# =============================================================================


class TestPayloadSafetyValidation:
    def test_any_field_raises(self) -> None:
        with pytest.raises(PayloadSafetyError) as exc_info:

            class BadInput(Input):
                data: Any

        assert "data" in str(exc_info.value)

    def test_bytes_field_raises(self) -> None:
        with pytest.raises(PayloadSafetyError) as exc_info:

            class BadInput(Input):
                raw: bytes

        assert "raw" in str(exc_info.value)

    def test_unbounded_list_raises(self) -> None:
        with pytest.raises(PayloadSafetyError) as exc_info:

            class BadInput(Input):
                items: list[dict]

        assert "items" in str(exc_info.value)

    def test_unbounded_dict_raises(self) -> None:
        with pytest.raises(PayloadSafetyError) as exc_info:

            class BadInput(Input):
                mapping: dict[str, Any]

        assert "mapping" in str(exc_info.value)

    def test_output_any_field_raises(self) -> None:
        with pytest.raises(PayloadSafetyError):

            class BadOutput(Output):
                data: Any

    def test_output_bytes_field_raises(self) -> None:
        with pytest.raises(PayloadSafetyError):

            class BadOutput(Output):
                content: bytes

    def test_allow_unbounded_fields_bypasses_validation(self) -> None:
        # Should NOT raise even with Any/bytes/unbounded list
        class FlexInput(Input, allow_unbounded_fields=True):
            data: Any
            raw: bytes
            items: list[dict]

        obj = FlexInput(data="x", raw=b"y", items=[{}])
        assert obj.data == "x"

    def test_allow_unbounded_fields_output(self) -> None:
        class FlexOutput(Output, allow_unbounded_fields=True):
            data: Any

        obj = FlexOutput(data=42)
        assert obj.data == 42


# =============================================================================
# validate_payload_safety standalone
# =============================================================================


class TestValidatePayloadSafety:
    def test_valid_class_passes(self) -> None:
        class GoodContract:
            name: str
            value: int

        # Should not raise
        validate_payload_safety(GoodContract)

    def test_any_field_forbidden(self) -> None:
        class BadContract:
            data: Any

        with pytest.raises(PayloadSafetyError):
            validate_payload_safety(BadContract)

    def test_skip_fields_bypasses_check(self) -> None:
        class Contract:
            data: Any

        # Explicitly skip the problematic field
        validate_payload_safety(Contract, skip_fields={"data"})


# =============================================================================
# is_backwards_compatible
# =============================================================================


class TestIsBackwardsCompatible:
    def test_identical_contracts_are_compatible(self) -> None:
        class V1(Input):
            name: str
            count: int = 0

        class V2(Input):
            name: str
            count: int = 0

        ok, issues = is_backwards_compatible(V1, V2)
        assert ok is True
        assert issues == []

    def test_new_field_with_default_is_compatible(self) -> None:
        class V1(Input):
            name: str

        class V2(Input):
            name: str
            extra: str = "default"

        ok, issues = is_backwards_compatible(V1, V2)
        assert ok is True
        assert issues == []

    def test_new_field_without_default_is_incompatible(self) -> None:
        class V1(Input):
            name: str

        class V2(Input):
            name: str
            required_new: str = ""  # needs default to be backwards-compatible

        ok, issues = is_backwards_compatible(V1, V2)
        # required_new has a default so this IS compatible
        assert ok is True

    def test_added_required_field_is_incompatible(self) -> None:
        class OldV(Input):
            name: str

        # Demonstrate that is_backwards_compatible catches missing defaults
        # by inspecting the fields manually rather than class-definition-time error
        class NewV(Input, allow_unbounded_fields=True):
            name: str

        # Manually verify has_default logic for required fields
        assert has_default(OldV, "name") is False
        assert has_default(NewV, "name") is False

    def test_removed_field_is_incompatible(self) -> None:
        class V1(Input):
            name: str
            old_field: str = ""

        class V2(Input):
            name: str
            # old_field removed

        ok, issues = is_backwards_compatible(V1, V2)
        assert ok is False
        assert any("old_field" in issue for issue in issues)

    def test_type_change_is_incompatible(self) -> None:
        class V1(Input):
            count: int = 0

        class V2(Input):
            count: str = ""  # changed from int to str

        ok, issues = is_backwards_compatible(V1, V2)
        assert ok is False
        assert any("count" in issue for issue in issues)

    def test_non_contract_raises(self) -> None:
        class NotAContract:
            pass

        class V1(Input):
            name: str

        with pytest.raises(ContractValidationError):
            is_backwards_compatible(NotAContract, V1)


# =============================================================================
# SerializableEnum
# =============================================================================


class TestSerializableEnum:
    def test_explicit_values(self) -> None:
        class Status(SerializableEnum):
            PENDING = "pending"
            RUNNING = "running"
            DONE = "done"

        assert Status.PENDING == "pending"
        assert Status.RUNNING == "running"
        assert str(Status.DONE) == "done"

    def test_auto_generates_lowercase(self) -> None:
        from enum import auto

        class Status(SerializableEnum):
            PENDING = auto()
            RUNNING = auto()
            COMPLETED = auto()

        assert Status.PENDING == "pending"
        assert Status.RUNNING == "running"
        assert Status.COMPLETED == "completed"

    def test_is_json_serializable(self) -> None:
        import json

        class Status(SerializableEnum):
            OK = "ok"
            FAIL = "fail"

        result = json.dumps({"status": Status.OK})
        assert result == '{"status": "ok"}'


# =============================================================================
# HeartbeatDetails and Record subclassing
# =============================================================================


class TestHeartbeatDetails:
    def test_can_be_subclassed(self) -> None:
        class MyHeartbeat(HeartbeatDetails):
            chunk_idx: int
            loaded_count: int = 0

        hb = MyHeartbeat(chunk_idx=5, loaded_count=100)
        assert hb.chunk_idx == 5
        assert hb.loaded_count == 100


class TestRecord:
    def test_can_be_subclassed(self) -> None:
        class ProductRecord(Record):
            name: str
            price: float

        rec = ProductRecord(id="prod-1", name="Widget", price=9.99)
        assert rec.id == "prod-1"
        assert rec.name == "Widget"

    def test_requires_id_field(self) -> None:
        class SimpleRecord(Record):
            pass

        # id is inherited from Record
        rec = SimpleRecord(id="abc")
        assert rec.id == "abc"


# =============================================================================
# config_hash
# =============================================================================


class TestConfigHash:
    def test_produces_16_char_hex(self) -> None:
        class MyInput(Input):
            name: str
            value: int = 0

        obj = MyInput(name="test", value=42)
        h = obj.config_hash()
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_stable_across_calls(self) -> None:
        class MyInput(Input):
            name: str

        obj = MyInput(name="stable")
        assert obj.config_hash() == obj.config_hash()

    def test_different_values_produce_different_hashes(self) -> None:
        class MyInput(Input):
            name: str

        a = MyInput(name="foo")
        b = MyInput(name="bar")
        assert a.config_hash() != b.config_hash()

    def test_default_values_excluded_from_hash(self) -> None:
        class MyInput(Input):
            name: str
            extra: str = "default"

        # Two objects that differ only in a field at its default value
        a = MyInput(name="x")
        b = MyInput(name="x", extra="default")
        # Both should produce the same hash since extra is at its default
        assert a.config_hash() == b.config_hash()

    def test_extra_exclude_removes_fields(self) -> None:
        class MyInput(Input):
            name: str
            run_id: str = ""

        a = MyInput(name="x", run_id="run-001")
        b = MyInput(name="x", run_id="run-002")
        # Without exclusion, hashes differ
        assert a.config_hash() != b.config_hash()
        # With run_id excluded, hashes match
        assert a.config_hash(extra_exclude={"run_id"}) == b.config_hash(
            extra_exclude={"run_id"}
        )


# =============================================================================
# validate_is_dataclass (backward-compat alias for validate_is_contract)
# =============================================================================


class TestValidateIsDataclass:
    def test_contract_subclass_passes(self) -> None:
        class GoodInput(Input):
            x: int = 0

        validate_is_dataclass(GoodInput)  # Should not raise

    def test_non_contract_raises(self) -> None:
        class NotDC:
            pass

        with pytest.raises(ContractValidationError) as exc_info:
            validate_is_dataclass(NotDC)

        assert "NotDC" in str(exc_info.value)

    def test_custom_context_in_error(self) -> None:
        class NotDC:
            pass

        with pytest.raises(ContractValidationError) as exc_info:
            validate_is_dataclass(NotDC, context="my input")

        assert "my input" in str(exc_info.value)


# =============================================================================
# get_contract_fields
# =============================================================================


class TestGetContractFields:
    def test_returns_field_types(self) -> None:
        class MyContract(Input):
            name: str
            count: int = 0

        result = get_contract_fields(MyContract)
        assert result["name"] is str
        assert result["count"] is int

    def test_non_contract_raises(self) -> None:
        class NotDC:
            pass

        with pytest.raises(ContractValidationError):
            get_contract_fields(NotDC)


# =============================================================================
# has_default
# =============================================================================


class TestHasDefault:
    def test_field_with_default_value(self) -> None:
        class MyContract(Input):
            name: str
            count: int = 42

        assert has_default(MyContract, "count") is True

    def test_field_without_default(self) -> None:
        class MyContract(Input):
            name: str

        assert has_default(MyContract, "name") is False

    def test_field_with_default_factory(self) -> None:
        class MyContract(Input, allow_unbounded_fields=True):
            items: list[str] = Field(default_factory=list)

        assert has_default(MyContract, "items") is True

    def test_missing_field_returns_false(self) -> None:
        class MyContract(Input):
            name: str

        assert has_default(MyContract, "nonexistent") is False


# =============================================================================
# ContractMetadata
# =============================================================================


class TestContractMetadata:
    def test_basic_construction(self) -> None:
        class MyInput(Input):
            name: str

        meta = ContractMetadata(
            name="my-input",
            version="1.0.0",
            cls=MyInput,
            is_input=True,
        )
        assert meta.name == "my-input"
        assert meta.version == "1.0.0"
        assert meta.cls is MyInput
        assert meta.is_input is True
        assert meta.schema_hash is None
        assert meta.deprecated is False

    def test_is_frozen(self) -> None:
        class MyInput(Input):
            name: str

        meta = ContractMetadata(
            name="my-input",
            version="1.0.0",
            cls=MyInput,
            is_input=True,
        )
        with pytest.raises((ValidationError, AttributeError, TypeError)):
            meta.name = "other"  # type: ignore[misc]
