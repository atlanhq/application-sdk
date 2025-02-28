from hypothesis import strategies as st

# Strategy for generating database names
database_name_strategy = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),  # Only letters and numbers
        blacklist_characters=[" ", "\t", "\n", "\r", '"', "'", "`", "/", "\\"],
    ),
)

# Strategy for generating schema names
schema_name_strategy = st.text(
    min_size=1,
    max_size=30,
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        blacklist_characters=[" ", "\t", "\n", "\r", '"', "'", "`", "/", "\\"],
    ),
)

# Strategy for generating lists of schema names
schema_list_strategy = st.lists(
    schema_name_strategy, min_size=1, max_size=5, unique=True
)

# Strategy for generating wildcard schema selections
wildcard_schema_strategy = st.just("*")

# Strategy for generating schema selections (either list of schemas or wildcard)
schema_selection_strategy = st.one_of(schema_list_strategy, wildcard_schema_strategy)

# Strategy for generating metadata entries as tuples (hashable)
metadata_entry_tuple_strategy = st.tuples(database_name_strategy, schema_name_strategy)

# Strategy for generating lists of metadata entries
metadata_list_strategy = st.lists(
    metadata_entry_tuple_strategy, min_size=1, max_size=10, unique=True
).map(
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
    keys=st.builds(lambda x: f"^{x}$", database_name_strategy),
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
    mapping=st.one_of(
        st.builds(str, mixed_mapping_strategy)  # Convert dict to JSON string
    ),
)
