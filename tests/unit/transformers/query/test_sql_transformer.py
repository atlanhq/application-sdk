import textwrap
from unittest.mock import mock_open, patch

import pyarrow as pa
import pytest

from application_sdk.transformers import query as query_module
from application_sdk.transformers.common.utils import flatten_yaml_columns
from application_sdk.transformers.query import QueryBasedTransformer
from application_sdk.transformers.query.errors import (
    BuildStructLevelRequiredError,
    BuildStructPrefixRequiredError,
    IncompatibleDefaultTypeError,
)


@pytest.fixture
def sql_transformer():
    return QueryBasedTransformer(
        connector_name="test_connector", tenant_id="test_tenant"
    )


@pytest.fixture
def sample_dataframe():
    return pa.Table.from_pydict(
        {
            "table_name": ["table1", "table2"],
            "table_catalog": ["db1", "db2"],
            "table_schema": ["schema1", "schema2"],
            "connection_qualified_name": ["conn1", "conn2"],
            "table_type": ["TABLE", "VIEW"],
            "table_kind": ["r", "v"],
            "is_partition": [True, False],
            "parent_table_name": ["parent1", None],
            "partition_strategy": ["strategy1", None],
            "view_definition": ["SELECT * FROM table1", "SELECT * FROM table2"],
        }
    )


@pytest.fixture
def sample_yaml_template():
    return {
        "columns": {
            "attributes": {
                # Direct column example
                "name": {"source_query": "table_name"},
                # SQL Query example with concat method
                "qualifiedName": {
                    "source_query": "concat(connection_qualified_name, '/', table_catalog, '/', table_schema, '/', table_name)",
                    "source_columns": [
                        "connection_qualified_name",
                        "table_catalog",
                        "table_schema",
                        "table_name",
                    ],
                },
                # SQL Query example with case when
                "type": {
                    "source_query": "case when table_type = 'TABLE' then 'table' when table_type = 'VIEW' then 'view' else table_type end",
                    "source_columns": ["table_type"],
                },
                # Literal value example
                "literal": {"source_query": "'Database'"},
            }
        }
    }


# Unit Tests for Individual Methods
def test_quote_column_name(sql_transformer):
    """Test the quote_column_name method"""
    assert sql_transformer.quote_column_name("normal_column") == "normal_column"
    assert sql_transformer.quote_column_name("column.with.dots") == '"column.with.dots"'


def test_convert_to_sql_expression(sql_transformer):
    """Test the convert_to_sql_expression method"""
    column = {"name": "test.column", "source_query": "source_column"}
    result = sql_transformer.convert_to_sql_expression(column)
    assert result == 'source_column AS "test.column"'


def test_convert_to_sql_expression_with_literal(sql_transformer):
    """Test the convert_to_sql_expression method with literal=True"""
    column = {
        "name": "test.column",
        "source_query": "'Database'",  # testing the literal value
    }
    result = sql_transformer.convert_to_sql_expression(column, is_literal=True)
    assert result == '"test.column" AS "test.column"'


def test_get_sql_column_expressions(
    sql_transformer, sample_dataframe, sample_yaml_template
):
    """Test the get_sql_column_expressions method"""
    default_attributes = {}
    sample_yaml_template["columns"] = flatten_yaml_columns(
        sample_yaml_template["columns"]
    )
    columns, literal_columns = sql_transformer.get_sql_column_expressions(
        sample_yaml_template, sample_dataframe, default_attributes
    )
    assert len(columns) == 4
    assert len(literal_columns) == 1
    assert '"table_name" AS "attributes.name"' in columns
    assert (
        "concat(connection_qualified_name, '/', table_catalog, '/', table_schema, '/', table_name) AS \"attributes.qualifiedName\""
        in columns
    )
    assert (
        "case when table_type = 'TABLE' then 'table' when table_type = 'VIEW' then 'view' else table_type end AS \"attributes.type\""
        in columns
    )
    assert '"attributes.literal" AS "attributes.literal"' in columns
    assert {
        "name": '"attributes.literal"',
        "source_query": "'Database'",
    } == literal_columns[0]


@patch("builtins.open", new_callable=mock_open)
@patch("yaml.safe_load")
def test_generate_sql_query(
    mock_yaml_load, mock_file, sql_transformer, sample_dataframe, sample_yaml_template
):
    """Test the generate_sql_query method"""
    mock_yaml_load.return_value = sample_yaml_template
    default_attributes = {}
    result, literal_columns = sql_transformer.generate_sql_query(
        "dummy_path", sample_dataframe, default_attributes
    )

    assert len(literal_columns) == 1
    assert {
        "name": '"attributes.literal"',
        "source_query": "'Database'",
    } == literal_columns[0]

    expected_result = textwrap.dedent(
        """\n            SELECT\n                "table_name" AS "attributes.name",concat(connection_qualified_name, \'/\', table_catalog, \'/\', table_schema, \'/\', table_name) AS "attributes.qualifiedName",case when table_type = \'TABLE\' then \'table\' when table_type = \'VIEW\' then \'view\' else table_type end AS "attributes.type","attributes.literal" AS "attributes.literal"\n            FROM dataframe\n            """
    )
    assert result == expected_result


def _dropped_field_names(mock_log_method):
    """Field name from each excluded-field diagnostic logged via the given mock method."""
    return [call.args[1] for call in mock_log_method.call_args_list]


def _rendered(call):
    """A logger call rendered the way the log pipeline renders its %-style message."""
    return call.args[0] % tuple(call.args[1:])


def test_quoted_sql_keyword_source_query_is_dropped_with_a_warning(
    sql_transformer, sample_dataframe
):
    """A SQL keyword authored as a quoted YAML string is dropped -- but no longer silently.

    ``source_query: "FALSE"`` parses to the Python string ``'FALSE'``, which is neither an
    available column nor a recognised literal (it is not SQL-quoted), so the field never
    reaches the generated SQL. Correct authoring is the unquoted YAML scalar ``FALSE``.
    """
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "name": {"source_query": "table_name"},
                    "propagate": {"source_query": "FALSE"},
                    "partitionOrder": {"source_query": "NULL"},
                }
            }
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        columns, literal_columns = sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}, yaml_path="tag_attachment.yaml"
        )

    assert columns == ['"table_name" AS "attributes.name"']
    assert literal_columns is None

    assert _dropped_field_names(mock_logger.warning) == [
        "attributes.propagate",
        "attributes.partitionOrder",
    ]
    warning_text = mock_logger.warning.call_args_list[0].args[0] % tuple(
        mock_logger.warning.call_args_list[0].args[1:]
    )
    assert "attributes.propagate" in warning_text
    assert "tag_attachment.yaml" in warning_text
    assert "FALSE" in warning_text


def test_unquoted_yaml_scalars_are_published_as_literals_without_warning(
    sql_transformer, sample_dataframe
):
    """The correct authoring of the fields above: YAML scalars hit the literal branch."""
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "propagate": {"source_query": False},
                    "partitionOrder": {"source_query": None},
                }
            }
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        columns, literal_columns = sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}
        )

    assert columns == [
        '"attributes.propagate" AS "attributes.propagate"',
        '"attributes.partitionOrder" AS "attributes.partitionOrder"',
    ]
    assert len(literal_columns) == 2
    mock_logger.warning.assert_not_called()


def test_absent_declared_source_columns_are_debug_not_warning(
    sql_transformer, sample_dataframe
):
    """By-design gating, not an authoring bug: declared inputs simply absent this run.

    The field resolves fine on a run that supplies ``missing_column``, so warning here
    would drown the genuine authoring errors -- these outnumber them roughly 6:1 on a
    real connector's templates.
    """
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "remoteId": {
                        "source_query": "upper(missing_column)",
                        "source_columns": ["missing_column"],
                    },
                }
            }
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        columns, literal_columns = sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}
        )

    assert columns == []
    assert literal_columns is None
    mock_logger.warning.assert_not_called()
    assert _dropped_field_names(mock_logger.debug) == ["attributes.remoteId"]


def test_absent_bare_column_reference_is_debug_not_warning(
    sql_transformer, sample_dataframe
):
    """Same gating, expressed without ``source_columns`` -- the common authoring style.

    ``source_columns`` is redundant for a single-column reference, since the gate admits
    it by exact column-name match, so templates routinely omit it. Keying the severity
    off ``source_columns`` would therefore warn on ordinary optional enrichments.
    """
    template = {
        "columns": flatten_yaml_columns(
            {"attributes": {"tagValue": {"source_query": "missing_column"}}}
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        columns, _ = sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}
        )

    assert columns == []
    mock_logger.warning.assert_not_called()
    assert _dropped_field_names(mock_logger.debug) == ["attributes.tagValue"]


def test_undeclared_sql_expression_can_never_resolve_and_warns(
    sql_transformer, sample_dataframe
):
    """A multi-token expression with no ``source_columns`` is admissible on no input.

    The gate only admits an undeclared ``source_query`` by exact column-name match, and
    an expression is never a column name -- so this is an authoring bug regardless of
    what the run supplies.
    """
    template = {
        "columns": flatten_yaml_columns(
            {"attributes": {"remoteId": {"source_query": "upper(table_name)"}}}
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        columns, _ = sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}
        )

    assert columns == []
    mock_logger.debug.assert_not_called()
    assert _dropped_field_names(mock_logger.warning) == ["attributes.remoteId"]


def test_quoted_keyword_with_declared_source_columns_is_debug_not_warning(
    sql_transformer, sample_dataframe
):
    """Declared ``source_columns`` outrank the keyword shape, because the gate admits them.

    ``{"source_query": "FALSE", "source_columns": ["missing_column"]}`` emits the valid
    SQL literal ``FALSE`` on a run supplying ``missing_column``, so reporting it as an
    authoring mistake when that column is absent would be a false warning.
    """
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "propagate": {
                        "source_query": "FALSE",
                        "source_columns": ["missing_column"],
                    },
                }
            }
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        columns, _ = sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}
        )

    assert columns == []
    mock_logger.warning.assert_not_called()
    assert _dropped_field_names(mock_logger.debug) == ["attributes.propagate"]


def test_absent_quoting_required_column_name_is_debug_not_warning(
    sql_transformer, sample_dataframe
):
    """A column name SQL would need to quote is still a column name, not an expression.

    ``my-col`` is not a Python identifier, but a run whose table carries that column
    resolves the field through the gate's column-name match -- so it is ordinary optional
    gating, and judging the shape by Python identifier rules would warn on it wrongly.
    """
    template = {
        "columns": flatten_yaml_columns(
            {"attributes": {"tagValue": {"source_query": "my-col"}}}
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        columns, _ = sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}
        )

    assert columns == []
    mock_logger.warning.assert_not_called()
    assert _dropped_field_names(mock_logger.debug) == ["attributes.tagValue"]


def test_quoting_required_column_name_resolves_when_the_run_supplies_it(
    sql_transformer,
):
    """The other half of that claim: such a name really does resolve when present.

    It resolves *and* renders quoted -- unquoted, ``my-col`` is a syntax error
    (subtraction), so the resolution gate's "is a column name" answer and the
    renderer's quoting must agree on every shape, not just bare identifiers.
    """
    template = {
        "columns": flatten_yaml_columns(
            {"attributes": {"tagValue": {"source_query": "my-col"}}}
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        columns, _ = sql_transformer.get_sql_column_expressions(
            template, pa.Table.from_pydict({"my-col": ["v1"]}), {}
        )

    assert columns == ['"my-col" AS "attributes.tagValue"']
    mock_logger.warning.assert_not_called()
    mock_logger.debug.assert_not_called()
    assert _execute(
        f"SELECT {columns[0]} FROM dataframe",
        pa.Table.from_pydict({"my-col": ["v1"]}),
    ) == [("v1",)]


def test_non_string_source_query_is_dropped_with_a_warning(
    sql_transformer, sample_dataframe
):
    """A YAML list or mapping ``source_query`` is never valid SQL text.

    It matches no column name and is none of the literal types, so it reaches the type
    guard -- the one warned shape that no amount of input can resolve.
    """
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "remoteId": {"source_query": ["table_name", "table_schema"]}
                }
            }
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        columns, literal_columns = sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}
        )

    assert columns == []
    assert literal_columns is None
    mock_logger.debug.assert_not_called()
    assert _dropped_field_names(mock_logger.warning) == ["attributes.remoteId"]


def test_warning_remediation_matches_the_shape_that_tripped_it(
    sql_transformer, sample_dataframe
):
    """Each warned shape needs a different edit, so one hard-coded hint misdirects.

    Unquoting a YAML scalar fixes ``source_query: "FALSE"``; it would do nothing for
    ``upper(table_name)``, whose fix is declaring ``source_columns``.
    """
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "propagate": {"source_query": "FALSE"},
                    "remoteId": {"source_query": "upper(table_name)"},
                    "tags": {"source_query": ["a", "b"]},
                }
            }
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        sql_transformer.get_sql_column_expressions(template, sample_dataframe, {})

    messages = {
        call.args[1]: call.args[0] % tuple(call.args[1:])
        for call in mock_logger.warning.call_args_list
    }

    assert "unquoted YAML scalar" in messages["attributes.propagate"]
    assert "source_columns" in messages["attributes.remoteId"]
    assert "must be a string" in messages["attributes.tags"]
    assert "unquoted YAML scalar" not in messages["attributes.remoteId"]


def test_whitespace_expression_without_a_call_is_recognised_as_an_expression(
    sql_transformer, sample_dataframe
):
    """An expression is recognised by its whitespace, not only by a ``(``.

    ``table_name is not null`` carries no parenthesis, so it reaches the expression rule
    through the whitespace operand alone -- the operand a call-shaped case like
    ``upper(table_name)`` short-circuits away. It declares no ``source_columns`` and names
    no column, so the remedy is to declare the columns it reads.
    """
    template = {
        "columns": flatten_yaml_columns(
            {"attributes": {"isActive": {"source_query": "table_name is not null"}}}
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        columns, _ = sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}
        )

    assert columns == []
    mock_logger.debug.assert_not_called()
    assert _dropped_field_names(mock_logger.warning) == ["attributes.isActive"]
    message = mock_logger.warning.call_args.args[0] % tuple(
        mock_logger.warning.call_args.args[1:]
    )
    assert "source_columns" in message


def test_emitted_field_set_equals_declared_field_set_when_inputs_are_present(
    sql_transformer, sample_dataframe, sample_yaml_template
):
    """Every declared field must reach the SQL when the input supplies its columns.

    The end-to-end assertion the sibling issue asked for: a template whose inputs are
    all present must lose nothing and say nothing.
    """
    sample_yaml_template["columns"] = flatten_yaml_columns(
        sample_yaml_template["columns"]
    )
    declared = {column["name"] for column in sample_yaml_template["columns"]}

    with patch.object(query_module, "logger") as mock_logger:
        columns, _ = sql_transformer.get_sql_column_expressions(
            sample_yaml_template, sample_dataframe, {}
        )

    emitted = {expression.rsplit(" AS ", 1)[1].strip('"') for expression in columns}
    assert emitted == declared
    mock_logger.warning.assert_not_called()
    mock_logger.debug.assert_not_called()
    mock_logger.info.assert_not_called()


def test_generate_sql_query_threads_the_template_path_into_the_warning(
    sql_transformer, sample_dataframe
):
    """The warning must name the template file, which only generate_sql_query knows."""
    template = {"columns": {"attributes": {"propagate": {"source_query": "FALSE"}}}}

    with (
        patch("builtins.open", new_callable=mock_open),
        patch("yaml.safe_load", return_value=template),
        patch.object(query_module, "logger") as mock_logger,
    ):
        sql_transformer.generate_sql_query(
            "app/transform/templates/tag_attachment.yaml", sample_dataframe, {}
        )

    assert (
        "app/transform/templates/tag_attachment.yaml"
        in mock_logger.warning.call_args.args
    )


def test_exclusion_summary_is_logged_at_info_with_declared_and_emitted_counts(
    sql_transformer, sample_dataframe
):
    """The production-visible record, since DEBUG is off in production.

    A dropped attribute is only detectable if something recorded in production says a
    declared field did not reach the SQL. The per-field WARNING covers the statically
    unresolvable shapes; this summary covers the class as a whole, including the gated
    fields whose per-field line is DEBUG.
    """
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "name": {"source_query": "table_name"},
                    "propagate": {"source_query": "FALSE"},
                    "remoteId": {
                        "source_query": "upper(missing_column)",
                        "source_columns": ["missing_column"],
                    },
                }
            }
        )
    }

    with patch.object(query_module, "logger") as mock_logger:
        sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}, yaml_path="tag_attachment.yaml"
        )

    summary = _rendered(mock_logger.info.call_args)
    assert "tag_attachment.yaml" in summary
    assert "excluded 2 of 3 declared fields" in summary
    assert "1 authoring mistake(s) ['attributes.propagate']" in summary
    assert "1 gated on inputs absent from this run ['attributes.remoteId']" in summary


def test_diagnostics_are_reported_once_not_once_per_batch(
    sql_transformer, sample_dataframe
):
    """The transformer runs once per input batch, so a per-call report is a flood.

    ``get_sql_column_expressions`` is reached once per parquet batch of a few thousand
    rows, which is hundreds of calls for one typename on a large tenant. The exclusion
    set is a function of the template and the input schema alone, so every call after the
    first carries no new information and must stay silent -- otherwise the WARNING that
    names a genuine authoring bug is repeated hundreds of times and the INFO summary
    lands inside a per-record loop, both of which ADR-0011 rules out.
    """

    def template():
        """A freshly parsed template, as every batch gets from ``generate_sql_query``.

        Built per iteration rather than hoisted, because ``convert_to_sql_expression``
        quotes ``column["name"]`` in place and ``generate_sql_query`` re-reads the YAML on
        every call. Reusing one parsed dict across calls is therefore not the production
        shape, and would accumulate quotes on each pass.
        """
        return {
            "columns": flatten_yaml_columns(
                {
                    "attributes": {
                        "name": {"source_query": "table_name"},
                        "propagate": {"source_query": "FALSE"},
                        "remoteId": {
                            "source_query": "upper(missing_column)",
                            "source_columns": ["missing_column"],
                        },
                    }
                }
            )
        }

    with patch.object(query_module, "logger") as mock_logger:
        for _ in range(200):
            columns, _literals = sql_transformer.get_sql_column_expressions(
                template(), sample_dataframe, {}, yaml_path="tag_attachment.yaml"
            )

    # Every batch still gets the same SQL — only the reporting is deduplicated.
    assert columns == ['"table_name" AS "attributes.name"']

    assert mock_logger.info.call_count == 1
    assert _dropped_field_names(mock_logger.warning) == ["attributes.propagate"]
    assert _dropped_field_names(mock_logger.debug) == ["attributes.remoteId"]


def test_a_changed_exclusion_set_is_reported_again(sql_transformer, sample_dataframe):
    """Deduplication must not swallow new information.

    A run whose input schema changes between batches gates a different field set, and a
    newly excluded field has never been reported. Keying on the field sets rather than on
    the template alone keeps that case visible.
    """
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "remoteId": {
                        "source_query": "upper(missing_column)",
                        "source_columns": ["missing_column"],
                    },
                    "sourceId": {
                        "source_query": "upper(other_missing_column)",
                        "source_columns": ["other_missing_column"],
                    },
                }
            }
        )
    }
    wider_dataframe = pa.Table.from_pydict(
        {"table_name": ["table1"], "missing_column": ["value1"]}
    )

    with patch.object(query_module, "logger") as mock_logger:
        sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}, yaml_path="column.yaml"
        )
        sql_transformer.get_sql_column_expressions(
            template, wider_dataframe, {}, yaml_path="column.yaml"
        )

    assert mock_logger.info.call_count == 2
    assert _dropped_field_names(mock_logger.debug) == [
        "attributes.remoteId",
        "attributes.sourceId",
        "attributes.sourceId",
    ]


def test_gating_reports_stop_at_the_shape_cap_with_one_notice(
    sql_transformer, sample_dataframe
):
    """The input-dependent dedupe set is bounded, and running out is announced.

    Once those diagnostics are incomplete the absence of a summary no longer means the
    field was published, which is exactly the false reassurance this PR set out to remove
    — so the cap warns once on the way out.
    """
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "remoteId": {
                        "source_query": "upper(missing_column)",
                        "source_columns": ["missing_column"],
                    }
                }
            }
        )
    }
    cap = query_module._MAX_REPORTED_GATING_SHAPES

    with patch.object(query_module, "logger") as mock_logger:
        for index in range(cap + 5):
            sql_transformer.get_sql_column_expressions(
                template, sample_dataframe, {}, yaml_path=f"template_{index}.yaml"
            )

    assert mock_logger.info.call_count == cap
    assert len(_dropped_field_names(mock_logger.debug)) == cap
    # Exactly one suppression notice, and nothing else at WARNING.
    assert mock_logger.warning.call_count == 1
    assert "diagnostics suppressed" in _rendered(mock_logger.warning.call_args)
    assert "authoring-mistake warnings continue" in _rendered(
        mock_logger.warning.call_args
    )


def test_authoring_mistakes_are_still_warned_past_the_gating_cap(
    sql_transformer, sample_dataframe
):
    """The cap must not silence the tier it does not need to bound.

    A never-resolvable field is a pure function of the template's static text, so the
    number of distinct sets is bounded by the connector's template count and cannot grow
    with batches at all. Capping that tier too would mean a genuine authoring mistake in a
    template first seen after the cap gets no line naming it — only a generic suppression
    notice — which is the failure mode this PR exists to remove.
    """
    gated_only = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "remoteId": {
                        "source_query": "upper(missing_column)",
                        "source_columns": ["missing_column"],
                    }
                }
            }
        )
    }
    cap = query_module._MAX_REPORTED_GATING_SHAPES

    with patch.object(query_module, "logger") as mock_logger:
        # Exhaust the gated tier on templates that carry no authoring mistake.
        for index in range(cap + 1):
            sql_transformer.get_sql_column_expressions(
                gated_only, sample_dataframe, {}, yaml_path=f"template_{index}.yaml"
            )
        mock_logger.reset_mock()

        # A template seen for the first time past the cap, carrying a real mistake.
        sql_transformer.get_sql_column_expressions(
            {
                "columns": flatten_yaml_columns(
                    {"attributes": {"propagate": {"source_query": "FALSE"}}}
                )
            },
            sample_dataframe,
            {},
            yaml_path="tag_attachment.yaml",
        )

    assert _dropped_field_names(mock_logger.warning) == ["attributes.propagate"]
    warning = _rendered(mock_logger.warning.call_args)
    assert "tag_attachment.yaml" in warning
    assert "unquoted YAML scalar" in warning
    # The summary is the capped tier, so it is gone — the naming WARNING is not.
    mock_logger.info.assert_not_called()


def test_a_changed_gated_set_does_not_re_warn_the_same_authoring_mistake(
    sql_transformer, sample_dataframe
):
    """The two tiers are keyed separately, so input churn cannot amplify the WARNING.

    A schema that changes between batches gates a different field set and is reported
    again — but the authoring mistake in the same template has not changed and must not be
    warned twice, or the input-independence the severity split bought is lost.
    """
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "propagate": {"source_query": "FALSE"},
                    "remoteId": {
                        "source_query": "upper(missing_column)",
                        "source_columns": ["missing_column"],
                    },
                    "sourceId": {
                        "source_query": "upper(other_missing_column)",
                        "source_columns": ["other_missing_column"],
                    },
                }
            }
        )
    }
    wider_dataframe = pa.Table.from_pydict(
        {"table_name": ["table1"], "missing_column": ["value1"]}
    )

    with patch.object(query_module, "logger") as mock_logger:
        sql_transformer.get_sql_column_expressions(
            template, sample_dataframe, {}, yaml_path="column.yaml"
        )
        sql_transformer.get_sql_column_expressions(
            template, wider_dataframe, {}, yaml_path="column.yaml"
        )

    assert _dropped_field_names(mock_logger.warning) == ["attributes.propagate"]
    assert mock_logger.info.call_count == 2


def test_build_struct_with_none_level(sql_transformer):
    """Test the _build_struct method raises ValueError when level is None"""
    with pytest.raises(BuildStructLevelRequiredError):
        sql_transformer._build_struct(level=None, prefix="test")


def test_build_struct_with_none_prefix(sql_transformer):
    """Test the _build_struct method raises ValueError when prefix is None"""
    level = {
        "columns": [
            ("attributes.name", "name"),
            ("attributes.qualifiedName", "qualifiedName"),
        ],
    }
    with pytest.raises(BuildStructPrefixRequiredError):
        sql_transformer._build_struct(level=level, prefix=None)


def test_get_grouped_dataframe_by_prefix(sql_transformer):
    """
    Test the get_grouped_dataframe_by_prefix method
    and validate the nested structure of the returned list of dicts.
    """
    table = pa.Table.from_pydict(
        {
            "attributes.name": ["table1", "table2", "table3"],
            "attributes.qualifiedName": [
                "conn1/db1/schema1/table1",
                "conn1/db1/schema2/table2",
                "conn1/db1/schema3/table3",
            ],
            "attributes.database.typeName": ["Database", "Database", "Database"],
            "attributes.database.uniqueAttributes.qualifiedName": [
                "conn1/db1",
                "conn1/db1",
                "conn1/db1",
            ],
            "customAttributes.parent_name": ["parent1", None, None],
            "attributes.type": ["TABLE", "TABLE", "TABLE"],
            "attributes.kind": ["r", "r", "r"],
            "attributes.isPartition": [True, False, False],
            "attributes.partitionStrategy": ["strategy1", None, None],
            "attributes.viewDefinition": ["SELECT * FROM table1", None, None],
            "typeName": ["Table", "Table", "Table"],
            "status": ["ACTIVE", "ACTIVE", "ACTIVE"],
        }
    )

    result = sql_transformer.get_grouped_dataframe_by_prefix(table)
    assert len(result) == 3
    # Standalone columns are preserved
    assert result[0]["typeName"] == "Table"
    assert result[0]["status"] == "ACTIVE"
    # Dot-notation columns become nested dicts
    assert result[0]["attributes"]["name"] == "table1"
    assert result[0]["attributes"]["qualifiedName"] == "conn1/db1/schema1/table1"
    assert result[0]["attributes"]["type"] == "TABLE"
    assert result[0]["attributes"]["isPartition"] is True
    # Deep nesting works
    assert result[0]["attributes"]["database"]["typeName"] == "Database"
    assert (
        result[0]["attributes"]["database"]["uniqueAttributes"]["qualifiedName"]
        == "conn1/db1"
    )
    # None scalar leaf values are preserved (only all-None dicts collapse to None)
    assert result[1]["attributes"]["partitionStrategy"] is None
    assert result[0]["customAttributes"]["parent_name"] == "parent1"


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_and_attributes(
    mock_generate, sql_transformer, sample_dataframe
):
    """Test the prepare_template_and_attributes method"""
    mock_generate.return_value = ("SELECT * FROM dataframe", None)
    workflow_id = "test_workflow"
    workflow_run_id = "test_run"
    connection_qualified_name = "default/postgres/1746717318"
    connection_name = "test_conn"

    result_df, sql_template = sql_transformer.prepare_template_and_attributes(
        sample_dataframe,
        workflow_id,
        workflow_run_id,
        connection_qualified_name,
        connection_name,
        "dummy_path",
    )

    assert "connection_qualified_name" in result_df.schema.names
    assert "connection_name" in result_df.schema.names
    assert "tenant_id" in result_df.schema.names
    assert "last_sync_workflow_name" in result_df.schema.names
    assert "last_sync_run" in result_df.schema.names
    assert "last_sync_run_at" in result_df.schema.names
    assert "connector_name" in result_df.schema.names


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_passes_null_typed_columns_through(
    mock_generate, sql_transformer
):
    """Null-typed columns are passed through unchanged.

    DuckDB handles null-typed columns in SUBSTRING / REGEXP_REPLACE / CASE-WHEN
    without any pre-promotion, so prepare_template_and_attributes leaves the
    pyarrow schema as-is.
    """
    mock_generate.return_value = ("SELECT * FROM dataframe", None)
    df = pa.Table.from_pydict(
        {"name": ["a"], "remarks": pa.array([None], type=pa.null())}
    )
    assert pa.types.is_null(df.schema.field("remarks").type)

    result_df, _ = sql_transformer.prepare_template_and_attributes(
        df, "wf", "wf-run", "default/pg/1", "test", "dummy_path"
    )

    assert pa.types.is_null(result_df.schema.field("remarks").type)
    assert pa.types.is_string(
        result_df.schema.field("name").type
    ) or pa.types.is_large_string(result_df.schema.field("name").type)


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_leaves_non_null_dtypes_untouched(
    mock_generate, sql_transformer
):
    """Only Null-typed columns get cast; other dtypes pass through unchanged."""
    mock_generate.return_value = ("SELECT * FROM dataframe", None)
    df = pa.Table.from_pydict(
        {
            "name": ["a", "b"],
            "rows": [1, 2],
            "flag": [True, False],
            "remarks": pa.array([None, None], type=pa.null()),
        }
    )
    assert pa.types.is_integer(df.schema.field("rows").type)
    assert pa.types.is_boolean(df.schema.field("flag").type)
    assert pa.types.is_null(df.schema.field("remarks").type)

    result_df, _ = sql_transformer.prepare_template_and_attributes(
        df, "wf", "wf-run", "default/pg/1", "test", "dummy_path"
    )

    assert pa.types.is_integer(result_df.schema.field("rows").type)
    assert pa.types.is_boolean(result_df.schema.field("flag").type)
    assert pa.types.is_null(result_df.schema.field("remarks").type)


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_no_null_columns_is_noop(mock_generate, sql_transformer):
    """When no column is Null-typed, the cast path is a no-op."""
    mock_generate.return_value = ("SELECT * FROM dataframe", None)
    df = pa.Table.from_pydict({"name": ["a"], "remarks": ["r"]})
    assert pa.types.is_string(df.schema.field("remarks").type)

    result_df, _ = sql_transformer.prepare_template_and_attributes(
        df, "wf", "wf-run", "default/pg/1", "test", "dummy_path"
    )

    assert pa.types.is_string(result_df.schema.field("remarks").type)


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_enables_utf8_sql_on_null_column(
    mock_generate, sql_transformer
):
    """After the cast, DuckDB with utf8 functions on the formerly-Null
    column succeeds — the SUBSTRING / CASE-WHEN-IS-NOT-NULL patterns work after promotion."""
    import duckdb

    mock_generate.return_value = ("SELECT * FROM dataframe", None)
    df = pa.Table.from_pydict(
        {"name": ["a", "b"], "remarks": pa.array([None, None], type=pa.null())}
    )
    assert pa.types.is_null(df.schema.field("remarks").type)

    result_df, _ = sql_transformer.prepare_template_and_attributes(
        df, "wf", "wf-run", "default/pg/1", "test", "dummy_path"
    )

    conn = duckdb.connect(":memory:")
    conn.register("daft_table", result_df)
    out = (
        conn.execute(
            "SELECT name, "
            "SUBSTRING(remarks, 1, 5) AS sub, "
            "CASE WHEN remarks IS NOT NULL THEN SUBSTRING(remarks, 1, 5) ELSE '' END AS guarded "
            "FROM daft_table"
        )
        .fetch_arrow_table()
        .to_pydict()
    )
    conn.close()

    assert out["name"] == ["a", "b"]
    assert out["sub"] == [None, None]
    assert out["guarded"] == ["", ""]


def test_transform_metadata_empty_dataframe(sql_transformer):
    """Test transform_metadata with empty input returns None"""
    empty_df = pa.Table.from_pydict({"dummy": pa.array([], type=pa.string())})
    result = sql_transformer.transform_metadata(
        "TABLE", empty_df, "test_workflow", "test_run"
    )
    assert result is None


@patch(
    "application_sdk.transformers.query.QueryBasedTransformer.prepare_template_and_attributes"
)
@patch(
    "application_sdk.transformers.query.QueryBasedTransformer.get_grouped_dataframe_by_prefix"
)
def test_transform_metadata_list_input_unifies_keys_across_records(
    mock_group, mock_prepare, sql_transformer
):
    """Keys absent from the first record must not be dropped from the batch.

    pa.Table.from_pylist infers the schema from the first record only, so a
    naive coercion silently loses any column the first record lacks.
    """
    records = [
        {"table_name": "table1"},
        {"table_name": "table2", "view_definition": "SELECT 1"},
    ]
    mock_prepare.side_effect = lambda dataframe, *a, **k: (
        dataframe,
        "SELECT * FROM dataframe",
    )
    mock_group.return_value = [{"typeName": "Table"}]

    sql_transformer.transform_metadata("TABLE", records, "test_workflow", "test_run")

    coerced = mock_prepare.call_args.args[0]
    assert coerced.schema.names == ["table_name", "view_definition"]
    assert coerced.to_pylist()[0]["view_definition"] is None
    assert coerced.to_pylist()[1]["view_definition"] == "SELECT 1"


LITERAL_STATUS_TEMPLATE = textwrap.dedent("""
columns:
  status:
    source_query: "'ACTIVE'"
  attributes:
    name:
      source_query: table_name
""")


def test_template_literal_wins_when_colliding_column_is_all_null(
    sql_transformer, tmp_path
):
    """Records carrying only explicit Nones for a literal-declared name must
    still get the template literal, not a null column."""
    template = tmp_path / "table.yaml"
    template.write_text(LITERAL_STATUS_TEMPLATE)
    records = [
        {"table_name": "t1", "status": None},
        {"table_name": "t2", "status": None},
    ]

    result = sql_transformer.transform_metadata(
        "TABLE",
        records,
        "wf",
        "run",
        entity_class_definitions={"TABLE": str(template)},
        connection_qualified_name="default/snowflake/1",
    )

    assert [row["status"] for row in result] == ["ACTIVE", "ACTIVE"]


def test_literal_precedence_is_row_local_on_colliding_column(sql_transformer, tmp_path):
    """A genuine source value wins for its own row; rows without one get the
    template literal. Row-local, so the outcome is independent of batch
    membership and record order. Collisions still belong fixed at the
    extractor."""
    template = tmp_path / "table.yaml"
    template.write_text(LITERAL_STATUS_TEMPLATE)
    records = [
        {"table_name": "t1"},
        {"table_name": "t2", "status": "DELETED"},
    ]

    result = sql_transformer.transform_metadata(
        "TABLE",
        records,
        "wf",
        "run",
        entity_class_definitions={"TABLE": str(template)},
        connection_qualified_name="default/snowflake/1",
    )

    assert [row["status"] for row in result] == ["ACTIVE", "DELETED"]


def test_castable_default_is_coerced_to_the_column_type(sql_transformer, tmp_path):
    """An int template literal colliding with a string source column is cast
    to the column's type and filled row-locally ('0', not a crash). Lossy but
    representable casts are accepted deliberately; only unrepresentable
    combinations raise (see the list-column test below)."""
    template = tmp_path / "table.yaml"
    template.write_text(
        textwrap.dedent("""
        columns:
          status:
            source_query: 0
          attributes:
            name:
              source_query: table_name
        """)
    )
    records = [
        {"table_name": "t1", "status": None},
        {"table_name": "t2", "status": "OK"},
    ]

    result = sql_transformer.transform_metadata(
        "TABLE",
        records,
        "wf",
        "run",
        entity_class_definitions={"TABLE": str(template)},
        connection_qualified_name="default/snowflake/1",
    )

    assert [row["status"] for row in result] == ["0", "OK"]


@pytest.mark.parametrize(
    "status_values",
    [
        pytest.param([None, ["a", "b"]], id="partially-null"),
        pytest.param([None, None], id="all-null"),
    ],
)
def test_uncastable_default_raises_regardless_of_null_shape(
    sql_transformer, tmp_path, status_values
):
    """A string literal cannot fill a list-typed colliding column: pyarrow's
    fill_null would explode 'ACTIVE' into single characters silently. The
    guard must raise a typed error naming the column — and the answer must
    not depend on how many of the column's values happen to be null in this
    batch."""
    template = tmp_path / "table.yaml"
    template.write_text(LITERAL_STATUS_TEMPLATE)
    table = pa.table(
        {
            "table_name": ["t1", "t2"],
            "status": pa.array(status_values, type=pa.list_(pa.string())),
        }
    )

    with pytest.raises(IncompatibleDefaultTypeError) as exc_info:
        sql_transformer.transform_metadata(
            "TABLE",
            table,
            "wf",
            "run",
            entity_class_definitions={"TABLE": str(template)},
            connection_qualified_name="default/snowflake/1",
        )

    assert "status" in str(exc_info.value)


def test_compatible_default_fills_typed_all_null_column_preserving_type(
    sql_transformer, tmp_path
):
    """A castable default filling a typed all-null column must keep the
    column's Arrow type, not re-infer its own."""
    template = tmp_path / "table.yaml"
    template.write_text(
        textwrap.dedent("""
        columns:
          status:
            source_query: 0
          attributes:
            name:
              source_query: table_name
        """)
    )
    table = pa.table(
        {
            "table_name": ["t1", "t2"],
            "status": pa.array([None, None], type=pa.string()),
        }
    )

    prepared, _ = sql_transformer.prepare_template_and_attributes(
        table,
        "wf",
        "run",
        connection_qualified_name="default/snowflake/1",
        entity_sql_template_path=str(template),
    )

    assert prepared.column("status").type == pa.string()
    assert prepared.column("status").to_pylist() == ["0", "0"]


def test_reserved_default_fills_null_rows_of_colliding_column(
    sql_transformer, tmp_path
):
    """Reserved defaults (connection_qualified_name etc.) follow the same
    row-local rule as template literals when records carry a colliding key."""
    template = tmp_path / "table.yaml"
    template.write_text(
        textwrap.dedent("""
        columns:
          attributes:
            qualifiedName:
              source_query: concat(connection_qualified_name, '/', table_name)
              source_columns: [connection_qualified_name, table_name]
        """)
    )
    records = [
        {"table_name": "t1", "connection_qualified_name": None},
        {"table_name": "t2", "connection_qualified_name": "custom/cqn/2"},
    ]

    result = sql_transformer.transform_metadata(
        "TABLE",
        records,
        "wf",
        "run",
        entity_class_definitions={"TABLE": str(template)},
        connection_qualified_name="default/snowflake/1",
    )

    assert [row["attributes"]["qualifiedName"] for row in result] == [
        "default/snowflake/1/t1",
        "custom/cqn/2/t2",
    ]


@patch(
    "application_sdk.transformers.query.QueryBasedTransformer.prepare_template_and_attributes"
)
@patch(
    "application_sdk.transformers.query.QueryBasedTransformer.get_grouped_dataframe_by_prefix"
)
def test_transform_metadata(
    mock_group, mock_prepare, sql_transformer, sample_dataframe
):
    """Test the transform_metadata method"""
    mock_prepare.return_value = (sample_dataframe, "SELECT * FROM dataframe")
    mock_group.return_value = [{"typeName": "Table"}]

    result = sql_transformer.transform_metadata(
        "TABLE",
        sample_dataframe,
        "test_workflow",
        "test_run",
        connection_qualified_name="test_connection",
    )

    assert result is not None
    mock_prepare.assert_called_once()
    mock_group.assert_called_once()


# ---------------------------------------------------------------------------
# Reserved keywords in the SELECT expression slot (FND-51)
# ---------------------------------------------------------------------------
#
# DuckDB restricts the expression slot but not the alias slot -- verified on the
# pinned 1.5.5: ``SELECT column AS column`` raises ParserException while
# ``SELECT x AS column`` parses.  ``source_query`` lands in the expression slot,
# so a template whose source column is named after a reserved keyword failed
# every transform of that entity type at runtime.  These tests execute the
# generated SQL rather than only asserting on its text, so they fail if DuckDB's
# behaviour ever diverges from the assumption the fix rests on.


@pytest.fixture
def keyword_dataframe():
    """A table whose columns are DuckDB reserved keywords."""
    return pa.Table.from_pydict(
        {
            "column": ["c1", "c2"],
            "order": [1, 2],
            "qualify": ["q1", "q2"],
            "normal": ["n1", "n2"],
        }
    )


def _execute(sql, table):
    """Run *sql* against *table* registered as ``dataframe``, returning the rows."""
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    try:
        connection.register("dataframe", table)
        return connection.sql(sql).fetchall()
    finally:
        connection.close()


def test_reserved_keyword_source_query_renders_valid_sql(
    sql_transformer, keyword_dataframe
):
    """A bare reserved keyword resolved as a column reference is quoted."""
    template = {
        "columns": flatten_yaml_columns(
            {"attributes": {"name": {"source_query": "column"}}}
        )
    }

    columns, _ = sql_transformer.get_sql_column_expressions(
        template, keyword_dataframe, {}
    )

    assert columns == ['"column" AS "attributes.name"']
    assert _execute(f"SELECT {columns[0]} FROM dataframe", keyword_dataframe) == [
        ("c1",),
        ("c2",),
    ]


def test_reserved_keyword_source_query_is_a_parse_error_unquoted(keyword_dataframe):
    """The premise: the same expression unquoted does not parse.

    Pins the behaviour the fix exists for, so this suite fails loudly rather than
    quietly over-quoting if DuckDB ever starts accepting the bare form.
    """
    duckdb = pytest.importorskip("duckdb")
    with pytest.raises(duckdb.ParserException):
        _execute('SELECT column AS "attributes.name" FROM dataframe', keyword_dataframe)


def test_already_quoted_source_query_is_not_double_wrapped(
    sql_transformer, keyword_dataframe
):
    """Templates remediated by hand against P040 now resolve, and are not re-wrapped.

    ``source_query: '"order"'`` carries the quotes in the value.  Two things have
    to hold for that spelling:

    * it must match the column ``order`` -- matching the raw quoted text instead
      is why such a template resolved to nothing and had its attribute dropped
      from published output entirely, trading a loud ParserException for a
      silent missing attribute;
    * it must not be re-wrapped -- that emits a quoted identifier literally named
      ``"order"``, which DuckDB rejects with a BinderException.

    Both spellings therefore render the same SQL.
    """
    template = {
        "columns": flatten_yaml_columns(
            {"attributes": {"name": {"source_query": '"order"'}}}
        )
    }

    columns, _ = sql_transformer.get_sql_column_expressions(
        template, keyword_dataframe, {}
    )

    assert columns == ['"order" AS "attributes.name"']
    assert _execute(f"SELECT {columns[0]} FROM dataframe", keyword_dataframe) == [
        (1,),
        (2,),
    ]


def test_source_columns_expression_is_never_quoted(sql_transformer, keyword_dataframe):
    """The source_columns route passes arbitrary SQL and must not be touched.

    A bare identifier is legal there too -- ``current_date`` is a zero-argument
    SQL keyword, and quoting it would turn a working expression into a lookup for
    a column that does not exist.
    """
    template = {
        "columns": flatten_yaml_columns(
            {
                "attributes": {
                    "asOf": {
                        "source_query": "current_date",
                        "source_columns": ["normal"],
                    },
                    "combined": {
                        "source_query": "concat(\"column\", '/', normal)",
                        "source_columns": ["column", "normal"],
                    },
                }
            }
        )
    }

    columns, _ = sql_transformer.get_sql_column_expressions(
        template, keyword_dataframe, {}
    )

    assert columns == [
        'current_date AS "attributes.asOf"',
        'concat("column", \'/\', normal) AS "attributes.combined"',
    ]
    assert _execute(f"SELECT {','.join(columns)} FROM dataframe", keyword_dataframe)


def test_literal_column_named_after_a_keyword_renders_valid_sql(
    sql_transformer, keyword_dataframe
):
    """The literal branch puts ``name`` in the expression slot, so it is quoted too.

    ``prepare_template_and_attributes`` appends a real column for each literal, so
    that expression is a reference to the appended column -- and a
    reserved-keyword name breaks it in exactly the same way.
    """
    template = {
        "columns": flatten_yaml_columns({"select": {"source_query": "'Database'"}})
    }

    columns, literal_columns = sql_transformer.get_sql_column_expressions(
        template, keyword_dataframe, {}
    )

    assert columns == ['"select" AS select']
    assert literal_columns == [{"name": "select", "source_query": "'Database'"}]
    # The literal's own column is appended before the SQL runs; emulate that.
    with_literal = keyword_dataframe.append_column(
        "select", pa.array(["Database"] * len(keyword_dataframe))
    )
    assert _execute(f"SELECT {columns[0]} FROM dataframe", with_literal) == [
        ("Database",),
        ("Database",),
    ]


def test_plain_column_reference_quoting_is_behaviour_neutral(
    sql_transformer, keyword_dataframe
):
    """An ordinary column name is quoted too, and resolves identically.

    DuckDB keeps quoted identifiers case-insensitive, so quoting every plain
    reference -- rather than only the ones matching a keyword list -- needs no
    list to stay current with DuckDB's grammar.
    """
    template = {
        "columns": flatten_yaml_columns(
            {"attributes": {"name": {"source_query": "normal"}}}
        )
    }

    columns, _ = sql_transformer.get_sql_column_expressions(
        template, keyword_dataframe, {}
    )

    assert columns == ['"normal" AS "attributes.name"']
    assert _execute(f"SELECT {columns[0]} FROM dataframe", keyword_dataframe) == [
        ("n1",),
        ("n2",),
    ]


# ---------------------------------------------------------------------------
# Non-bare-identifier column names (digit-prefixed, hyphenated, Unicode)
# ---------------------------------------------------------------------------
#
# A resolved column reference is quoted for the expression slot whatever its
# shape: ``_resolution_key`` matching accepts *any* string equal to an available
# column name, and pyarrow places no grammar on names.  Gating the quoting on an
# ASCII identifier shape left exactly the names SQL cannot parse bare -- a
# digit-prefixed name reads as arithmetic (``2024_total`` -> ``2024 - total``),
# a hyphenated name as subtraction, a Unicode name as a syntax error -- broken
# at runtime on every transform of that entity type.


@pytest.fixture
def awkward_dataframe():
    """A table whose column names SQL cannot parse as bare identifiers."""
    return pa.Table.from_pydict(
        {
            "2024_total": [10, 20],
            "a-b": ["h1", "h2"],
            "café": ["u1", "u2"],
        }
    )


@pytest.mark.parametrize(
    "column_name, expected_rows",
    [
        ("2024_total", [(10,), (20,)]),
        ("a-b", [("h1",), ("h2",)]),
        ("café", [("u1",), ("u2",)]),
    ],
)
def test_non_bare_identifier_source_query_is_quoted(
    sql_transformer, awkward_dataframe, column_name, expected_rows
):
    """A digit-prefixed, hyphenated, or Unicode column resolves and is quoted.

    Unquoted, DuckDB parses ``SELECT 2024_total FROM t`` as the literal
    expression ``2024 - total`` rather than the column -- verified live: the
    unquoted form returns ``(2024,)`` for every row instead of the column
    values.
    """
    template = {
        "columns": flatten_yaml_columns(
            {"attributes": {"name": {"source_query": column_name}}}
        )
    }

    columns, _ = sql_transformer.get_sql_column_expressions(
        template, awkward_dataframe, {}
    )

    assert columns == [f'"{column_name}" AS "attributes.name"']
    assert _execute(f"SELECT {columns[0]} FROM dataframe", awkward_dataframe) == (
        expected_rows
    )


def test_digit_prefixed_source_query_is_not_a_column_unquoted(awkward_dataframe):
    """The premise: unquoted, a digit-prefixed name never resolves the column.

    In the full rendered shape the bare form is a hard ``ParserException``
    (``2024_total AS ...`` parses ``2024_total`` as the arithmetic expression
    ``2024 - total``, after which ``AS`` is a syntax error); without the alias,
    DuckDB accepts it and silently returns the literal ``2024`` for every row.
    Either way the column value is unreachable, which is what the quoting
    exists to prevent -- and if DuckDB's grammar ever changes here, this test
    says so.
    """
    duckdb = pytest.importorskip("duckdb")
    with pytest.raises(duckdb.ParserException):
        _execute(
            'SELECT 2024_total AS "attributes.name" FROM dataframe',
            awkward_dataframe,
        )
    assert _execute("SELECT 2024_total FROM dataframe", awkward_dataframe) == [
        (2024,),
        (2024,),
    ]


def test_column_name_with_embedded_quote_is_escaped(sql_transformer):
    """A column name containing ``"`` resolves with the quote doubled.

    The SQL escaping rule denotes a literal quote inside a quoted identifier by
    doubling it (``"a""b"`` is the column ``a"b``); wrapping without escaping
    would render a syntax error.
    """
    table = pa.Table.from_pydict({'a"b': [1]})
    template = {
        "columns": flatten_yaml_columns(
            {"attributes": {"name": {"source_query": 'a"b'}}}
        )
    }

    columns, _ = sql_transformer.get_sql_column_expressions(template, table, {})

    assert columns == ['"a""b" AS "attributes.name"']
    assert _execute(f"SELECT {columns[0]} FROM dataframe", table) == [(1,)]
