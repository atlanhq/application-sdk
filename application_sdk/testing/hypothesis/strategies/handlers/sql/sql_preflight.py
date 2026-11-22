"""Hypothesis strategies for SQL preflight-check payloads.

.. deprecated:: 3.59.0
    Every strategy here is deprecated and removed in v4.0.0. Nothing in the
    SDK imports them and no test exercises them; build strategies from your
    app's own models in its tests instead.
"""

from hypothesis import strategies as st
from packaging import version

from application_sdk import _deprecation

# Strategy for generating database names
_database_name_strategy = st.text()

# Strategy for generating schema names
_schema_name_strategy = st.text()

# Strategy for generating lists of schema names
_schema_list_strategy = st.lists(_schema_name_strategy, unique=False)

# Strategy for generating wildcard schema selections
_wildcard_schema_strategy = st.just("*")

# Strategy for generating schema selections (either list of schemas or wildcard)
_schema_selection_strategy = st.one_of(_schema_list_strategy, _wildcard_schema_strategy)

# Strategy for generating metadata entries as tuples (hashable)
_metadata_entry_tuple_strategy = st.tuples(
    _database_name_strategy, _schema_name_strategy
)

# Strategy for generating lists of metadata entries
_metadata_list_strategy = st.lists(_metadata_entry_tuple_strategy, unique=False).map(
    lambda entries: [
        {"TABLE_CATALOG": db, "TABLE_SCHEMA": schema} for db, schema in entries
    ]
)

# Strategy for generating database to schema mappings
_db_schema_mapping_strategy = st.dictionaries(
    keys=_database_name_strategy,
    values=_schema_selection_strategy,
    min_size=1,
    max_size=3,
)

# Strategy for generating regex-style database to schema mappings
_regex_db_schema_mapping_strategy = st.dictionaries(
    keys=st.one_of(
        st.builds(lambda x: f"^{x}$", _database_name_strategy),
        st.builds(lambda x: f"{x}.*", _database_name_strategy),
        st.builds(lambda x: f".*{x}", _database_name_strategy),
        st.builds(lambda x: f".*{x}.*", _database_name_strategy),
    ),
    values=_schema_selection_strategy,
    min_size=1,
    max_size=3,
)

# Strategy for generating mixed format mappings
_mixed_mapping_strategy = st.one_of(
    _db_schema_mapping_strategy, _regex_db_schema_mapping_strategy
)

# Strategy for generating complete preflight check payloads
_preflight_check_payload_strategy = st.builds(
    lambda mapping: {"metadata": {"include-filter": mapping}},
    mapping=st.one_of(st.builds(str, _mixed_mapping_strategy)),
)

# Strategy for generating version tuples
_version_tuple_strategy = st.tuples(
    st.integers(min_value=1, max_value=20),  # Major version
    st.integers(min_value=0, max_value=99),  # Minor version
).map(lambda v: f"{v[0]}.{v[1]}")  # Convert to string format

# Strategy for version comparison test cases
_version_comparison_strategy = st.builds(
    lambda client, min_ver: (
        client,
        min_ver,
        version.parse(client) >= version.parse(min_ver),
    ),
    client=_version_tuple_strategy,
    min_ver=_version_tuple_strategy,
)  # Creates a tuple[str, str, bool]


#: name -> (replacement, why). Served by __getattr__ below (PEP 562).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "database_name_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "schema_name_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "schema_list_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "wildcard_schema_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "schema_selection_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "metadata_entry_tuple_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "metadata_list_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "db_schema_mapping_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "regex_db_schema_mapping_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "mixed_mapping_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "preflight_check_payload_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "version_tuple_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
    "version_comparison_strategy": (
        "a strategy built from your app's typed preflight Input contract",
        "no SDK code or test uses this module",
    ),
}

#: A star import reads ``__all__`` and resolves each name through ``__getattr__``,
#: so ``import *`` keeps binding every old name, with the warning. Without it,
#: Python would enumerate only the module's real globals and skip them all.
__all__ = [*_DEPRECATED_CONSTANTS]


def _deprecated_constant_value(name: str) -> object:
    return globals()[f"_{name}"]


__getattr__ = _deprecation.deprecated_module_getattr(
    __name__,
    _DEPRECATED_CONSTANTS,
    remove_in="4.0.0",
    resolve=_deprecated_constant_value,
)
