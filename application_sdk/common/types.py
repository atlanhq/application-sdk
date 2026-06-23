from enum import Enum


class DataframeType(Enum):
    """Enumeration of dataframe types."""

    pandas = "pandas"
    daft = (
        "daft"  # Deprecated: will be removed in v4.0; routes to the pandas/pyarrow path
    )
