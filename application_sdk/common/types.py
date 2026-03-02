from enum import Enum


class DataframeType(Enum):
    """Enumeration of dataframe types."""

    pandas = "pandas"
    daft = "daft"
