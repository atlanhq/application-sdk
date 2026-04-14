from hypothesis import strategies as st
from packaging import version

# Strategy for generating database names
database_name_strategy = st.text()

# Strategy for generating schema names
schema_name_strategy = st.text()

# Strategy for generating lists of schema names
schema_list_strategy = st.lists(schema_name_strategy, unique=False)

# Strategy for generating wildcard schema selections
wildcard_schema_strategy = st.just("*")

# Strategy for generating schema selections (either list of schemas or wildcard)
schema_selection_strategy = st.one_of(schema_list_strategy, wildcard_schema_strategy)

# Strategy for generating metadata entries as tuples (hashable)
metadata_entry_tuple_strategy = st.tuples(database_name_strategy, schema_name_strategy)

# Strategy for generating lists of metadata entries
metadata_list_strategy = st.lists(metadata_entry_tuple_strategy, unique=False).map(
    lambda entries: [
        {"TABLE_CATALOG": db, "TABLE_SCHEMA": schema} for db, schema in entries
    ]
)

# Strategy for generating database to schema mappings
db_schema_mapping_strategy = st.dictionaries(
    keys=database_name_strategy,
    values=schema_selection_strategy,
    min_size=1,
    max_size=3,
)

# Strategy for generating regex-style database to schema mappings
regex_db_schema_mapping_strategy = st.dictionaries(
    keys=st.one_of(
        st.builds(lambda x: f"^{x}$", database_name_strategy),
        st.builds(lambda x: f"{x}.*", database_name_strategy),
        st.builds(lambda x: f".*{x}", database_name_strategy),
        st.builds(lambda x: f".*{x}.*", database_name_strategy),
    ),
    values=schema_selection_strategy,
    min_size=1,
    max_size=3,
)

# Strategy for generating mixed format mappings
mixed_mapping_strategy = st.one_of(
    db_schema_mapping_strategy, regex_db_schema_mapping_strategy
)

# Strategy for generating complete preflight check payloads
preflight_check_payload_strategy = st.builds(
    lambda mapping: {"metadata": {"include-filter": mapping}},
    mapping=st.one_of(st.builds(str, mixed_mapping_strategy)),
)

# Strategy for generating version tuples
version_tuple_strategy = st.tuples(
    st.integers(min_value=1, max_value=20),  # Major version
    st.integers(min_value=0, max_value=99),  # Minor version
).map(lambda v: f"{v[0]}.{v[1]}")  # Convert to string format

# Strategy for version comparison test cases
version_comparison_strategy = st.builds(
    lambda client, min_ver: (
        client,
        min_ver,
        version.parse(client) >= version.parse(min_ver),
    ),
    client=version_tuple_strategy,
    min_ver=version_tuple_strategy,
)  # Creates a tuple[str, str, bool]
