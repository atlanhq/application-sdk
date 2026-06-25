"""Property-based tests for sql_filters placeholder rendering.

``prepare_query`` substitutes include/exclude filter values into SQL query
templates via ``str.format`` against a documented placeholder set.  A
malformed substitution surfaces either as a ``KeyError`` (template references
a placeholder the SDK doesn't supply) or as a leftover ``{placeholder}`` in
the emitted SQL — both of which have historically only been caught in
connectors at runtime.  These properties pin the contract:

* for arbitrary well-formed include/exclude filters (and optional
  temp-table regexes), rendering NEVER raises
* the emitted SQL contains no unsubstituted ``{...}`` placeholders
* every documented placeholder is supplied in both regex dialects
  (``use_posix_regex`` True and False)
"""

from __future__ import annotations

import json
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from application_sdk.common.sql_filters import prepare_query

# A template that uses EVERY documented placeholder prepare_query supplies.
TEMPLATE = """SELECT table_catalog, table_schema, table_name
FROM information_schema.tables
WHERE concat(table_catalog, '.', table_schema) ~ '{normalized_include_regex}'
  AND concat(table_catalog, '.', table_schema) !~ '{normalized_exclude_regex}'
  AND table_catalog ~ {include_databases}
  AND table_catalog !~ {exclude_databases}
  {temp_table_regex_sql}
  AND exclude_empty = {exclude_empty_tables}
  AND exclude_views = {exclude_views}"""

TEMP_TABLE_SQL = "AND table_name !~ '{exclude_table_regex}'"

# Any "{word}" left in the output is an unsubstituted placeholder: filter
# values cannot contain braces (the identifier alphabets below exclude them).
_UNSUBSTITUTED = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")

# SQL-identifier-shaped database / schema names (also regex-safe).
_identifier = st.from_regex(r"[a-zA-Z_][a-zA-Z0-9_]{0,8}", fullmatch=True)

# Filter-map values: "*" (all schemas), [] (same), or explicit schema lists.
_schemas = st.one_of(
    st.just("*"),
    st.just([]),
    st.lists(_identifier.map(lambda s: f"^{s}$"), min_size=1, max_size=4),
)

# Include/exclude filter maps keyed by anchored database patterns.
_filter_map = st.dictionaries(
    keys=_identifier.map(lambda d: f"^{d}$"),
    values=_schemas,
    max_size=4,
)

# Temp-table regexes from a deny-list-clean alphabet (no quotes/semicolons,
# no "-" so "--" can never be generated, no braces).
_temp_table_regex = st.one_of(
    st.none(),
    st.text(alphabet="abcdefXYZ0123456789_.*^$|", min_size=1, max_size=20),
)


def _workflow_args(
    include: dict, exclude: dict, temp_regex: str | None, empty: bool, views: bool
) -> dict:
    metadata: dict = {
        "include-filter": json.dumps(include),
        "exclude-filter": json.dumps(exclude),
        "exclude_empty_tables": empty,
        "exclude_views": views,
    }
    if temp_regex is not None:
        metadata["temp-table-regex"] = temp_regex
    return {"metadata": metadata}


@given(
    include=_filter_map,
    exclude=_filter_map,
    temp_regex=_temp_table_regex,
    use_posix=st.booleans(),
    exclude_empty=st.booleans(),
    exclude_views=st.booleans(),
)
@settings(max_examples=50, deadline=None)
def test_rendered_query_has_no_unsubstituted_placeholders(
    include: dict,
    exclude: dict,
    temp_regex: str | None,
    use_posix: bool,
    exclude_empty: bool,
    exclude_views: bool,
) -> None:
    """Rendering is KeyError-free and leaves no '{...}' behind."""
    result = prepare_query(
        TEMPLATE,
        _workflow_args(include, exclude, temp_regex, exclude_empty, exclude_views),
        temp_table_regex_sql=TEMP_TABLE_SQL,
        use_posix_regex=use_posix,
    )

    assert result is not None
    leftover = _UNSUBSTITUTED.search(result)
    assert leftover is None, f"unsubstituted placeholder {leftover.group()!r}"


@given(
    include=_filter_map,
    exclude=_filter_map,
    use_posix=st.booleans(),
)
@settings(max_examples=50, deadline=None)
def test_documented_boolean_placeholders_render_their_values(
    include: dict,
    exclude: dict,
    use_posix: bool,
) -> None:
    """exclude_empty_tables / exclude_views render the metadata values."""
    result = prepare_query(
        TEMPLATE,
        _workflow_args(include, exclude, None, True, False),
        temp_table_regex_sql=TEMP_TABLE_SQL,
        use_posix_regex=use_posix,
    )
    assert result is not None
    assert "exclude_empty = True" in result
    assert "exclude_views = False" in result


@given(
    include=_filter_map,
    exclude=_filter_map,
    temp_regex=st.text(alphabet="abcdefXYZ0123456789_.*^$|", min_size=1, max_size=20),
    use_posix=st.booleans(),
)
@settings(max_examples=50, deadline=None)
def test_temp_table_regex_is_substituted_into_its_snippet(
    include: dict,
    exclude: dict,
    temp_regex: str,
    use_posix: bool,
) -> None:
    """When a temp-table regex is supplied, the snippet carries it verbatim."""
    result = prepare_query(
        TEMPLATE,
        _workflow_args(include, exclude, temp_regex, False, False),
        temp_table_regex_sql=TEMP_TABLE_SQL,
        use_posix_regex=use_posix,
    )
    assert result is not None
    assert TEMP_TABLE_SQL.format(exclude_table_regex=temp_regex) in result


@given(
    include=_filter_map,
    exclude=_filter_map,
    use_posix=st.booleans(),
)
@settings(max_examples=25, deadline=None)
def test_missing_temp_table_regex_drops_the_snippet_entirely(
    include: dict,
    exclude: dict,
    use_posix: bool,
) -> None:
    """No temp-table regex → the snippet (and its placeholder) vanish."""
    result = prepare_query(
        TEMPLATE,
        _workflow_args(include, exclude, None, False, False),
        temp_table_regex_sql=TEMP_TABLE_SQL,
        use_posix_regex=use_posix,
    )
    assert result is not None
    assert "exclude_table_regex" not in result
    assert "table_name !~" not in result
