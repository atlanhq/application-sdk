import string
from pathlib import Path

import pandas as pd
from hypothesis import strategies as st
from hypothesis.strategies import composite

# Every value a JSON writer can serialise: orjson rejects integers outside the
# signed 64-bit range.
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

# Upper bound on drawn rows: enough to span several chunks without making each
# example build a large frame.
_MAX_ROWS = 50

# Strategy for generating file path components. Portable filename characters
# only: an arbitrary code point can be a separator, NUL, or a byte sequence the
# filesystem refuses (macOS rejects unpaired surrogates and some planes).
safe_path_strategy = st.text(
    alphabet=string.ascii_letters + string.digits + "_-",
    min_size=1,
    max_size=32,
)

# Strategy for generating output paths
output_path_strategy = st.builds(
    lambda base, suffix: str(Path(base) / suffix),
    base=safe_path_strategy,
    suffix=safe_path_strategy,
)

# Strategy for generating output prefixes
output_prefix_strategy = safe_path_strategy

# Strategy for generating chunk sizes. A writer needs at least one row per
# chunk; the upper bound is well past _MAX_ROWS so a single chunk is drawn too.
chunk_size_strategy = st.integers(min_value=1, max_value=10_000)

# Text a source can actually hand the writer: any code point except unpaired
# surrogates (category Cs), which no UTF-8 input decodes to and which neither
# pandas nor orjson can encode.
_text_characters = st.characters(exclude_categories=("Cs",))

# Strategy for generating column names
column_name_strategy = st.text(
    alphabet=_text_characters,
).map(lambda x: x.strip())

# Strategy for generating cell values
cell_value_strategy = st.one_of(
    st.integers(min_value=_INT64_MIN, max_value=_INT64_MAX),
    st.floats(allow_infinity=True, allow_nan=True),
    st.text(
        alphabet=_text_characters,
    ),
    st.booleans(),
    st.none(),
)

# Strategy for generating DataFrame columns
dataframe_columns_strategy = st.lists(column_name_strategy, max_size=10)


@composite
def dataframe_strategy(draw) -> pd.DataFrame:
    """Generate a pandas DataFrame with random data."""
    columns = draw(dataframe_columns_strategy)
    num_rows = draw(st.integers(min_value=0, max_value=_MAX_ROWS))

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
        "output_prefix": output_prefix_strategy,
        "chunk_size": chunk_size_strategy,
    }
)
