"""Serializable enum base, shared by the SDK and its api package.

``application_sdk.contracts.base.SerializableEnum`` is this same class.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SerializableEnum"]


class SerializableEnum(StrEnum):
    """Base class for enums that need to be serialized through Temporal.

    Enums that inherit from this class are automatically JSON serializable
    because they inherit from both ``str`` and ``Enum``. The enum value is used
    as the serialized string representation.

    This solves the "Object of type XEnum is not JSON serializable" error
    that occurs when using regular enums in Temporal activity/workflow payloads.

    Usage:
        class MyStatus(SerializableEnum):
            PENDING = "pending"
            RUNNING = "running"
            COMPLETED = "completed"
            FAILED = "failed"

        class MyOutput(Output):
            status: MyStatus  # Works with Temporal serialization

    The enum values should be strings that match the desired serialized form.
    When deserialized, Temporal will reconstruct the enum from the string value.
    """

    @staticmethod
    def _generate_next_value_(  # type: ignore[override]
        name: str, start: int, count: int, last_values: list[str]
    ) -> str:
        """Auto-generate value from name in lowercase.

        This allows defining enums without explicit values:

            class Status(SerializableEnum):
                PENDING = auto()  # value will be "pending"
                RUNNING = auto()  # value will be "running"
        """
        return name.lower()
