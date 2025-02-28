from typing import Dict, List

import pandas as pd
from hypothesis import strategies as st

# Strategy for generating simple numeric values
numeric_value_strategy = st.integers(min_value=0, max_value=1000)

# Strategy for generating test data dictionaries
test_data_dict_strategy = st.builds(
    lambda value: {"value": value},
    value=numeric_value_strategy,
)

# Strategy for generating lists of test data
test_data_list_strategy = st.lists(
    test_data_dict_strategy,
    min_size=1,
    max_size=20,
)

# Strategy for generating temporary file paths
file_name_strategy = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),  # Letters and numbers
        blacklist_characters=["/", "\\", " ", "\t", "\n", "\r"],
    ),
)

# Strategy for generating file extensions
file_extension_strategy = st.just(".parquet")

# Strategy for generating complete file paths
file_path_strategy = st.builds(
    lambda name, ext: f"{name}{ext}",
    name=file_name_strategy,
    ext=file_extension_strategy,
)

# Strategy for generating chunk sizes
chunk_size_strategy = st.one_of(
    st.none(),  # No chunking
    st.integers(min_value=1, max_value=10),  # Chunk sizes from 1 to 10
)

# Strategy for generating ParquetInput configurations
parquet_input_config_strategy = st.builds(
    lambda file_path, chunk_size: {
        "file_path": file_path,
        "chunk_size": chunk_size,
    },
    file_path=file_path_strategy,
    chunk_size=chunk_size_strategy,
)

# Strategy for generating output modes
output_mode_strategy = st.sampled_from(["overwrite", "append"])

# Strategy for generating output suffixes
output_suffix_strategy = st.builds(
    lambda name: f"/{name}",
    name=file_name_strategy,
)

# Strategy for generating ParquetOutput configurations
parquet_output_config_strategy = st.builds(
    lambda path, suffix, prefix, mode: {
        "output_path": path,
        "output_suffix": suffix,
        "output_prefix": prefix,
        "mode": mode,
    },
    path=file_path_strategy,
    suffix=output_suffix_strategy,
    prefix=file_name_strategy,
    mode=output_mode_strategy,
)


# Helper function to convert test data to DataFrame
def create_test_dataframe(data: List[Dict[str, int]]) -> pd.DataFrame:
    """Convert test data list to pandas DataFrame"""
    return pd.DataFrame(data)


# Strategy for generating test DataFrames
test_dataframe_strategy = st.builds(
    create_test_dataframe,
    data=test_data_list_strategy,
)
