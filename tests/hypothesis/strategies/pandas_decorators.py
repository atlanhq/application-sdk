from pathlib import Path

import pandas as pd
from hypothesis import strategies as st
from hypothesis.strategies import composite

# Strategy for generating safe file path components
safe_path_strategy = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),  # Only letters and numbers
        blacklist_characters=["/", "\\", ".", " ", "\t", "\n", "\r"],
    ),
)

# Strategy for generating file paths
file_path_strategy = st.builds(
    lambda base, suffix: str(Path(base) / suffix),
    base=safe_path_strategy,
    suffix=safe_path_strategy,
)

# Strategy for generating SQL queries
sql_query_strategy = st.text(
    min_size=1,
    max_size=100,
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "P"),  # Letters, numbers, punctuation
        blacklist_characters=["\x00", "\n", "\r", "\t"],  # No control characters
    ),
)

# Strategy for generating chunk sizes
chunk_size_strategy = st.one_of(
    st.none(),  # No chunking
    st.integers(min_value=1, max_value=5),  # Small chunks for testing
)

# Strategy for generating column names
column_name_strategy = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        blacklist_characters=["/", "\\", ".", " ", "\t", "\n", "\r"],
    ),
).map(lambda x: x.strip())

# Strategy for generating cell values
cell_value_strategy = st.one_of(
    st.integers(min_value=-100, max_value=100),
    st.floats(allow_infinity=False, allow_nan=False, min_value=-100, max_value=100),
    st.text(
        min_size=0, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))
    ),
    st.booleans(),
    st.none(),
)


@composite
def dataframe_strategy(draw, min_rows: int = 0, max_rows: int = 10) -> pd.DataFrame:
    """Generate a pandas DataFrame with random data."""
    num_columns = draw(st.integers(min_value=1, max_value=5))
    num_rows = draw(st.integers(min_value=min_rows, max_value=max_rows))

    columns = draw(
        st.lists(
            column_name_strategy,
            min_size=num_columns,
            max_size=num_columns,
            unique=True,
        )
    )

    if num_rows == 0:
        return pd.DataFrame(columns=columns)

    data = {
        col: draw(st.lists(cell_value_strategy, min_size=num_rows, max_size=num_rows))
        for col in columns
    }
    return pd.DataFrame(data)


# Strategy for generating SQL input configurations
sql_input_config_strategy = st.fixed_dictionaries(
    {"query": sql_query_strategy, "chunk_size": chunk_size_strategy}
)

# Strategy for generating JSON input configurations
json_input_config_strategy = st.fixed_dictionaries(
    {
        "path": file_path_strategy,
        "file_names": st.lists(st.just("schema/1.json"), min_size=1, max_size=1),
        "download_file_prefix": st.just("raw"),
    }
)

# Strategy for generating JSON output configurations
json_output_config_strategy = st.fixed_dictionaries(
    {"output_path": file_path_strategy, "output_suffix": st.just("transformed/schema")}
)
