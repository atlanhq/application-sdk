"""Enum base class shared by contracts and the runtime substrate.

Defined here rather than in ``contracts/base.py`` — its public home, which
re-exports it — because ``application_sdk.contracts`` transitively imports
``storage.ops`` (via ``contracts.types`` → ``credentials.ref``), and
:mod:`application_sdk._runtime.progress` needs this class while staying
importable from ``storage/`` at module scope. See ADR-0019.

The class has no dependencies of its own, so the bottom layer is where it can
actually live. ``from application_sdk.contracts import SerializableEnum`` and
``from application_sdk.contracts.base import SerializableEnum`` continue to
resolve to this exact class object, so subclass checks are unaffected.
"""

from enum import StrEnum


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
