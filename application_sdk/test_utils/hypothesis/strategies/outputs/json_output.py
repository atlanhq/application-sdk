from pathlib import Path

import pandas as pd
from hypothesis import strategies as st
from hypothesis.strategies import composite

# Strategy for generating safe file path components
safe_path_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=10,
)

# Strategy for generating output paths
output_path_strategy = st.builds(
    lambda base, suffix: str(Path(base) / suffix),
    base=safe_path_strategy,
    suffix=safe_path_strategy,
)

# Strategy for generating output prefixes
output_prefix_strategy = safe_path_strategy

# Strategy for generating chunk sizes
chunk_size_strategy = st.integers(min_value=1, max_value=1000)

# Strategy for generating column names
column_name_strategy = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=10,
).map(lambda x: x.strip())

# Strategy for generating cell values
cell_value_strategy = st.one_of(
    st.integers(min_value=-100, max_value=100),
    st.floats(allow_infinity=False, allow_nan=False, min_value=-100, max_value=100),
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- ",
        min_size=0,
        max_size=10,
    ),
    st.booleans(),
    st.none(),
)

# Strategy for generating DataFrame columns
dataframe_columns_strategy = st.lists(column_name_strategy)


@composite
def dataframe_strategy(draw) -> pd.DataFrame:
    """Generate a pandas DataFrame with random data."""
    columns = draw(dataframe_columns_strategy)
    num_rows = draw(st.integers(min_value=0, max_value=100))

    if num_rows == 0 or not columns:
        return pd.DataFrame(columns=columns)

    data = {
        col: draw(st.lists(cell_value_strategy, min_size=num_rows, max_size=num_rows))
        for col in columns
    }
    return pd.DataFrame(data)


# Strategy for generating JsonOutput configuration
json_output_config_strategy = st.fixed_dictionaries(
    {
        "output_path": safe_path_strategy,
        "output_suffix": st.builds(lambda x: f"/{x}", safe_path_strategy),
        "output_prefix": output_prefix_strategy,
        "chunk_size": chunk_size_strategy,
    }
)
