from typing import TYPE_CHECKING, Union

from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    import daft
    import pandas as pd

logger = get_logger(__name__)


def is_empty_dataframe(dataframe: Union["pd.DataFrame", "daft.DataFrame"]) -> bool:  # noqa: F821
    """Check if a DataFrame is empty.

    This function determines whether a DataFrame has any rows, supporting both
    pandas and daft DataFrame types. For pandas DataFrames, it uses the `empty`
    property, and for daft DataFrames, it checks if the row count is 0.

    Args:
        dataframe (Union[pd.DataFrame, daft.DataFrame]): The DataFrame to check,
            can be either a pandas DataFrame or a daft DataFrame.

    Returns:
        bool: True if the DataFrame has no rows, False otherwise.

    Note:
        If daft is not available and a daft DataFrame is passed, the function
        will log a warning and return True.
    """
    import pandas as pd

    if isinstance(dataframe, pd.DataFrame):
        return dataframe.empty

    try:
        import daft

        if isinstance(dataframe, daft.DataFrame):
            return dataframe.count_rows() == 0
    except Exception:
        logger.warning("Module daft not found")
    return True
