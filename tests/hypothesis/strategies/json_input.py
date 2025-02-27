from typing import List
from pathlib import Path
from hypothesis import strategies as st

# Strategy for generating safe file path components
safe_path_strategy = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(
        whitelist_categories=('Lu', 'Ll', 'Nd'),  # Only letters and numbers
        blacklist_characters=['/', '\\', '.', ' ', '\t', '\n', '\r']
    )
)

# Strategy for generating file names
file_name_strategy = st.builds(
    lambda name: f"{name}.json",
    name=safe_path_strategy
)

# Strategy for generating lists of file names
file_names_strategy = st.lists(
    file_name_strategy,
    min_size=1,
    max_size=10,
    unique=True
)

# Strategy for generating download file prefixes
download_prefix_strategy = safe_path_strategy

# Strategy for generating complete JsonInput configurations
json_input_config_strategy = st.fixed_dictionaries({
    "path": safe_path_strategy,
    "download_file_prefix": download_prefix_strategy,
    "file_names": file_names_strategy
}) 