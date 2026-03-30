"""Tests for the @on_event decorator and EventHandlerMetadata."""

from dataclasses import dataclass

import pytest

from application_sdk.app.event import (
    EventContractError,
    EventHandlerMetadata,
    get_event_metadata,
    is_event_handler,
    on_event,
)
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.events import EventFilter

# =============================================================================
# Test fixtures
# =============================================================================


@dataclass
class SimpleInput(Input):
    value: str = ""


@dataclass
class SimpleOutput(Output):
    result: str = ""


# =============================================================================
# @on_event decorator - basic usage
# =============================================================================


class TestOnEventDecoratorBasicUsage:
    """Tests for @on_event decorator syntax variants."""

    def test_on_event_without_parens(self) -> None:
        """@on_event without parens works."""

        class MyApp:
            @on_event
            async def handle_event(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput(result=input.value)

        assert is_event_handler(MyApp.handle_event)

    def test_on_event_with_empty_parens(self) -> None:
        """@on_event() with empty parens works."""

        class MyApp:
            @on_event()
            async def handle_event(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput(result=input.value)

        assert is_event_handler(MyApp.handle_event)

    def test_on_event_with_topic_and_event_name(self) -> None:
        """@on_event(topic=..., event_name=...) sets topic and event_name."""

        class MyApp:
            @on_event(topic="atlas_events", event_name="entity_update")
            async def handle_update(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput(result=input.value)

        metadata = get_event_metadata(MyApp.handle_update)
        assert metadata is not None
        assert metadata.topic == "atlas_events"
        assert metadata.event_name == "entity_update"

    def test_on_event_standalone_function(self) -> None:
        """@on_event works on standalone async functions (no self)."""

        @on_event(topic="my_topic", event_name="my_event")
        async def handle(input: SimpleInput) -> SimpleOutput:
            return SimpleOutput(result=input.value)

        assert is_event_handler(handle)
        metadata = get_event_metadata(handle)
        assert metadata is not None
        assert metadata.input_type is SimpleInput
        assert metadata.output_type is SimpleOutput


# =============================================================================
# EventHandlerMetadata - stored correctly
# =============================================================================


class TestEventHandlerMetadataStored:
    """Tests that metadata is stored correctly on decorated methods."""

    def test_metadata_stored_on_function(self) -> None:
        """Metadata is stored as _event_metadata attribute."""

        class MyApp:
            @on_event(topic="t", event_name="e", version="2.0")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        assert hasattr(MyApp.handle, "_event_metadata")
        metadata = MyApp.handle._event_metadata
        assert isinstance(metadata, EventHandlerMetadata)

    def test_metadata_input_output_types(self) -> None:
        """Metadata captures input and output types."""

        class MyApp:
            @on_event(topic="t")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.input_type is SimpleInput
        assert metadata.output_type is SimpleOutput

    def test_metadata_func_reference(self) -> None:
        """Metadata stores reference to original function."""

        class MyApp:
            @on_event(topic="t")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.func is MyApp.handle

    def test_metadata_description_from_docstring(self) -> None:
        """Description defaults to function docstring when not provided."""

        class MyApp:
            @on_event(topic="t")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                """My handler docstring."""
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.description == "My handler docstring."

    def test_metadata_description_explicit(self) -> None:
        """Explicit description overrides docstring."""

        class MyApp:
            @on_event(topic="t", description="Explicit desc")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                """Docstring."""
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.description == "Explicit desc"


# =============================================================================
# Defaults
# =============================================================================


class TestOnEventDefaults:
    """Tests for default values of @on_event parameters."""

    def test_default_version(self) -> None:
        """Default version is '1.0'."""

        class MyApp:
            @on_event(topic="t")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.version == "1.0"

    def test_default_pre_filters(self) -> None:
        """Default pre_filters is empty list."""

        class MyApp:
            @on_event(topic="t")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.pre_filters == []

    def test_default_retry_max_attempts(self) -> None:
        """Default retry_max_attempts is 3."""

        class MyApp:
            @on_event(topic="t")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.retry_max_attempts == 3

    def test_default_timeout_seconds(self) -> None:
        """Default timeout_seconds is 600."""

        class MyApp:
            @on_event(topic="t")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.timeout_seconds == 600

    def test_default_event_name_from_function(self) -> None:
        """Default event_name is the function name."""

        class MyApp:
            @on_event(topic="t")
            async def handle_entity_update(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle_entity_update)
        assert metadata is not None
        assert metadata.event_name == "handle_entity_update"

    def test_default_topic_empty(self) -> None:
        """Default topic is empty string."""

        class MyApp:
            @on_event
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.topic == ""


# =============================================================================
# event_id property
# =============================================================================


class TestEventHandlerMetadataEventId:
    """Tests for EventHandlerMetadata.event_id property."""

    def test_event_id_basic(self) -> None:
        """event_id is '{event_name}__{version_dots_to_underscores}'."""

        class MyApp:
            @on_event(topic="t", event_name="entity_update", version="1.0")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.event_id == "entity_update__1_0"

    def test_event_id_multi_dot_version(self) -> None:
        """event_id replaces all dots in version."""

        class MyApp:
            @on_event(topic="t", event_name="my_event", version="2.1.3")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.event_id == "my_event__2_1_3"

    def test_event_id_no_dots_in_version(self) -> None:
        """event_id works when version has no dots."""

        class MyApp:
            @on_event(topic="t", event_name="ev", version="3")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.event_id == "ev__3"


# =============================================================================
# pre_filters parameter
# =============================================================================


class TestOnEventPreFilters:
    """Tests for pre_filters parameter."""

    def test_pre_filters_single(self) -> None:
        """Single EventFilter in pre_filters."""
        f = EventFilter(path="data.type", operator="eq", value="TABLE")

        class MyApp:
            @on_event(topic="t", pre_filters=[f])
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert len(metadata.pre_filters) == 1
        assert metadata.pre_filters[0].path == "data.type"
        assert metadata.pre_filters[0].operator == "eq"
        assert metadata.pre_filters[0].value == "TABLE"

    def test_pre_filters_multiple(self) -> None:
        """Multiple EventFilter instances in pre_filters."""
        filters = [
            EventFilter(path="data.type", operator="eq", value="TABLE"),
            EventFilter(path="data.status", operator="neq", value="DELETED"),
        ]

        class MyApp:
            @on_event(topic="t", pre_filters=filters)
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert len(metadata.pre_filters) == 2


# =============================================================================
# Custom parameter values
# =============================================================================


class TestOnEventCustomParams:
    """Tests for non-default parameter values."""

    def test_custom_retry_max_attempts(self) -> None:
        """Custom retry_max_attempts is stored."""

        class MyApp:
            @on_event(topic="t", retry_max_attempts=5)
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.retry_max_attempts == 5

    def test_custom_timeout_seconds(self) -> None:
        """Custom timeout_seconds is stored."""

        class MyApp:
            @on_event(topic="t", timeout_seconds=1800)
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.timeout_seconds == 1800

    def test_custom_version(self) -> None:
        """Custom version is stored."""

        class MyApp:
            @on_event(topic="t", version="3.2")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        assert metadata.version == "3.2"


# =============================================================================
# Validation - rejects sync functions
# =============================================================================


class TestOnEventRejectsSyncFunctions:
    """Tests that @on_event rejects non-async functions."""

    def test_rejects_sync_function(self) -> None:
        """Sync function raises EventContractError."""
        with pytest.raises(EventContractError, match="must be an async function"):

            class MyApp:
                @on_event(topic="t")
                def handle(self, input: SimpleInput) -> SimpleOutput:  # type: ignore[arg-type]
                    return SimpleOutput()

    def test_rejects_sync_standalone(self) -> None:
        """Sync standalone function raises EventContractError."""
        with pytest.raises(EventContractError, match="must be an async function"):

            @on_event(topic="t")
            def handle(input: SimpleInput) -> SimpleOutput:  # type: ignore[arg-type]
                return SimpleOutput()

    def test_rejects_sync_bare_decorator(self) -> None:
        """Sync function with bare @on_event raises EventContractError."""
        with pytest.raises(EventContractError, match="must be an async function"):

            class MyApp:
                @on_event
                def handle(self, input: SimpleInput) -> SimpleOutput:  # type: ignore[arg-type]
                    return SimpleOutput()


# =============================================================================
# Validation - rejects non-Input parameter
# =============================================================================


class TestOnEventRejectsNonInputParam:
    """Tests that @on_event rejects parameters not extending Input."""

    def test_rejects_plain_str_param(self) -> None:
        """String parameter raises EventContractError."""
        with pytest.raises(EventContractError, match="must extend Input base class"):

            class MyApp:
                @on_event(topic="t")
                async def handle(self, data: str) -> SimpleOutput:
                    return SimpleOutput()

    def test_rejects_dict_param(self) -> None:
        """Dict parameter raises EventContractError."""
        with pytest.raises(EventContractError, match="must extend Input base class"):

            class MyApp:
                @on_event(topic="t")
                async def handle(self, data: dict) -> SimpleOutput:  # type: ignore[type-arg]
                    return SimpleOutput()

    def test_rejects_unannotated_param(self) -> None:
        """Parameter without type annotation raises EventContractError."""
        with pytest.raises(EventContractError, match="must have a type annotation"):

            class MyApp:
                @on_event(topic="t")
                async def handle(self, data) -> SimpleOutput:
                    return SimpleOutput()

    def test_rejects_no_params(self) -> None:
        """No parameters (besides self) raises EventContractError."""
        with pytest.raises(EventContractError, match="must have exactly one parameter"):

            class MyApp:
                @on_event(topic="t")
                async def handle(self) -> SimpleOutput:
                    return SimpleOutput()

    def test_rejects_multiple_params(self) -> None:
        """Multiple parameters raises EventContractError."""
        with pytest.raises(EventContractError, match="must have exactly one parameter"):

            class MyApp:
                @on_event(topic="t")
                async def handle(self, input: SimpleInput, extra: str) -> SimpleOutput:
                    return SimpleOutput()


# =============================================================================
# Validation - rejects non-Output return type
# =============================================================================


class TestOnEventRejectsNonOutputReturn:
    """Tests that @on_event rejects return types not extending Output."""

    def test_rejects_str_return(self) -> None:
        """String return type raises EventContractError."""
        with pytest.raises(EventContractError, match="return type must extend Output"):

            class MyApp:
                @on_event(topic="t")
                async def handle(self, input: SimpleInput) -> str:
                    return "bad"

    def test_rejects_dict_return(self) -> None:
        """Dict return type raises EventContractError."""
        with pytest.raises(EventContractError, match="return type must extend Output"):

            class MyApp:
                @on_event(topic="t")
                async def handle(self, input: SimpleInput) -> dict:  # type: ignore[type-arg]
                    return {}

    def test_rejects_no_return_annotation(self) -> None:
        """Missing return annotation raises EventContractError."""
        with pytest.raises(
            EventContractError, match="must have a return type annotation"
        ):

            class MyApp:
                @on_event(topic="t")
                async def handle(self, input: SimpleInput):
                    return None


# =============================================================================
# is_event_handler helper
# =============================================================================


class TestIsEventHandler:
    """Tests for is_event_handler helper."""

    def test_returns_true_for_decorated(self) -> None:
        """Returns True for @on_event decorated functions."""

        class MyApp:
            @on_event
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        assert is_event_handler(MyApp.handle) is True

    def test_returns_false_for_undecorated(self) -> None:
        """Returns False for plain functions."""

        async def plain(input: SimpleInput) -> SimpleOutput:
            return SimpleOutput()

        assert is_event_handler(plain) is False

    def test_returns_false_for_non_callable(self) -> None:
        """Returns False for non-callable objects."""
        assert is_event_handler("not a function") is False
        assert is_event_handler(42) is False
        assert is_event_handler(None) is False


# =============================================================================
# EventHandlerMetadata frozen
# =============================================================================


class TestEventHandlerMetadataFrozen:
    """Tests that EventHandlerMetadata is frozen (immutable)."""

    def test_cannot_modify_topic(self) -> None:
        """Modifying topic raises FrozenInstanceError."""

        class MyApp:
            @on_event(topic="t", event_name="e")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        with pytest.raises(AttributeError):
            metadata.topic = "new_topic"  # type: ignore[misc]

    def test_cannot_modify_event_name(self) -> None:
        """Modifying event_name raises FrozenInstanceError."""

        class MyApp:
            @on_event(topic="t", event_name="e")
            async def handle(self, input: SimpleInput) -> SimpleOutput:
                return SimpleOutput()

        metadata = get_event_metadata(MyApp.handle)
        assert metadata is not None
        with pytest.raises(AttributeError):
            metadata.event_name = "new_name"  # type: ignore[misc]
