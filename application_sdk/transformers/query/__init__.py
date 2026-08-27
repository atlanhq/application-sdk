from __future__ import annotations

import re
import textwrap
import warnings
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml
from pyatlan.model.enums import AtlanConnectorType

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.common.utils import (
    flatten_yaml_columns,
    get_yaml_query_template_path_mappings,
)
from application_sdk.transformers.query.errors import (
    BuildStructLevelRequiredError as BuildStructLevelRequiredError,
)
from application_sdk.transformers.query.errors import (
    BuildStructPrefixRequiredError as BuildStructPrefixRequiredError,
)
from application_sdk.transformers.query.errors import (
    IncompatibleDefaultTypeError as IncompatibleDefaultTypeError,
)
from application_sdk.transformers.query.errors import (
    SqlTransformNotRegisteredError as SqlTransformNotRegisteredError,
)

warnings.warn(
    "application_sdk.transformers.query is deprecated; use the connector-side "
    "asset-mapper pattern (typed records → map_<entity>() → pyatlan_v9 Asset) instead "
    "— will be removed in v4.0. See docs/upgrade-guide-v3.md.",
    DeprecationWarning,
    stacklevel=2,
)

logger = get_logger(__name__)

_SQL_KEYWORD_LITERALS = frozenset({"FALSE", "TRUE", "NULL"})

# A whole value that is one double-quoted SQL identifier, interior quotes doubled
# per the SQL escaping rule (``"a""b"`` denotes the column ``a"b``).  Anchored at
# both ends so an *expression* that merely contains a quoted identifier
# (``concat("a", b)``) does not match.
_QUOTED_IDENTIFIER_RE = re.compile(r'^"(?:[^"]|"")*"$')


def _is_quoted_identifier(value: str) -> bool:
    """Whether *value* is, in its entirety, a double-quoted SQL identifier."""
    return bool(_QUOTED_IDENTIFIER_RE.match(value))


def _unquote_identifier(value: str) -> str:
    """The column name a quoted SQL identifier denotes (``\"a\"\"b\"`` -> ``a\"b``)."""
    return value[1:-1].replace('""', '"')


def _resolution_key(value: Any) -> Any:
    """The name *value* looks up when matched against the available columns.

    A template that already carries SQL quotes inside the YAML value
    (``source_query: '\"order\"'`` -- the remediation the P040 conformance rule
    prescribes) denotes the column ``order``, so it must be matched under that
    name.  Matching the raw text instead is why such a template resolved to
    nothing and had its attribute dropped from published output entirely: the
    quotes turned a loud ``ParserException`` into a silent missing attribute.
    """
    if isinstance(value, str) and _is_quoted_identifier(value):
        return _unquote_identifier(value)
    return value


def _quote_bare_identifier(value: Any) -> Any:
    """Quote *value* for the SELECT expression slot when it denotes a column.

    Both call sites only pass a value that already resolved as a column *name*
    (``source_query`` matched against the available columns, or the literal
    branch's appended column), so every string that reaches here is an
    identifier reference -- including names SQL cannot parse bare, such as
    ``2024_total`` (which DuckDB reads as the expression ``2024 - total``),
    hyphenated names, and Unicode names.  Gating quoting on an ASCII ``[A-Za-z_]``
    shape left exactly those columns unquoted and broken, so any non-quoted
    string is wrapped unconditionally, with interior quotes doubled per the SQL
    escaping rule.

    Idempotent, and that is the point: an already-quoted value re-wrapped emits
    ``\"\"\"order\"\"\"`` -- a quoted identifier whose name is literally
    ``\"order\"``, which DuckDB rejects with ``BinderException: Referenced column
    \"\"order\"\" not found``.  So a quoted value passes through untouched and both
    spellings render the same SQL.

    Non-string values are returned unchanged.
    """
    if not isinstance(value, str) or _is_quoted_identifier(value):
        return value
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


_REMEDY_NON_STRING = (
    "A source_query must be a string; a YAML list or mapping is neither a column "
    "reference nor a literal."
)
_REMEDY_QUOTED_KEYWORD = (
    "A bare SQL keyword must be authored as an unquoted YAML scalar, not a quoted "
    "string."
)
_REMEDY_UNDECLARED_EXPRESSION = (
    "A multi-token SQL expression must declare the columns it reads in source_columns."
)

# Excluded-field diagnostics are reported once per distinct (template, excluded field
# set) shape rather than once per call. ``get_sql_column_expressions`` runs once per
# input batch -- callers stream parquet in batches of a few thousand rows -- so a
# per-call report repeats every line hundreds of times for a single typename on a large
# tenant, which ADR-0011 rules out for both WARNING ("very low -- per-anomaly") and INFO
# ("do NOT log at INFO inside a loop that runs per-record").
#
# This caps only the input-dependent tier, whose key includes the fields gated on columns
# absent from a given batch: that is the one set a caller with a schema that varies per
# batch could grow without limit. Real callers hold one shape per template. The
# authoring-mistake tier is deduplicated on the template's static text alone and needs no
# cap -- see ``_report_excluded_fields``.
_MAX_REPORTED_GATING_SHAPES = 64


def _is_sql_expression(source_query: str) -> bool:
    """Whether a ``source_query`` is a multi-token SQL construct rather than a name.

    A call or a whitespace-separated construct is an expression, and the gate admits an
    undeclared ``source_query`` only by exact column-name match. A single token that
    merely needs SQL quoting -- ``my-col``, ``2024_total`` -- is deliberately not judged
    here: it can name a real column, so it belongs to the by-design-gating branch rather
    than to the authoring-mistake branch.
    """
    return "(" in source_query or any(character.isspace() for character in source_query)


def _unresolvable_remedy(column: dict[str, Any]) -> str | None:
    """How to fix a field the SQL gate excluded, or ``None`` if it may resolve as authored.

    Separates an authoring mistake from by-design gating. A field whose declared
    ``source_columns`` are merely absent this run -- or whose ``source_query`` names a
    column absent this run -- resolves on a run that supplies them, which is the
    transformer's opt-in behaviour for optional enrichments and not a defect.

    Three shapes indicate an authoring mistake instead, and each needs a different edit,
    so the caller reports the remedy returned here rather than one hard-coded hint:

    * a ``source_query`` that is not a string at all (a YAML list or mapping), which is
      never valid SQL text.
    * a bare SQL keyword authored as a quoted string (``source_query: "FALSE"``), which
      is read as a column reference and so only ever matches a column literally named
      ``FALSE``. The unquoted YAML scalar ``FALSE`` is the correct authoring.
    * a multi-token SQL expression that declares no ``source_columns``, since the gate
      admits an undeclared ``source_query`` only by exact column-name match.

    The type guard is checked first, because a non-string ``source_query`` is never valid
    SQL text whatever the declared inputs. Declared ``source_columns`` are then checked
    before the remaining shape rules, so a field that would resolve on a run supplying
    them is never reported as an authoring mistake.
    """
    source_query = column["source_query"]
    if not isinstance(source_query, str):
        return _REMEDY_NON_STRING
    if column.get("source_columns"):
        return None
    if source_query.upper() in _SQL_KEYWORD_LITERALS:
        return _REMEDY_QUOTED_KEYWORD
    if _is_sql_expression(source_query):
        return _REMEDY_UNDECLARED_EXPRESSION
    return None


class QueryBasedTransformer(TransformerInterface):
    """Query based transformer that uses YAML files for SQL queries and DuckDB for execution.

    Uses a YAML file to define SQL queries for each asset type and executes them on raw dataframes
    using DuckDB to get transformed data.

    The execution flow is:
        1. Initialize transformer with connector name and tenant ID
        2. Map asset types (DATABASE, SCHEMA, TABLE, COLUMN etc) to YAML template paths
           from default or custom template directories
        3. Transform metadata by:
           - Loading YAML template for the typename
           - Preparing default attributes and SQL template
           - Generating SQL query from template
           - Executing query on raw pyarrow Table via DuckDB
           - Converting flat table with dot notation to nested structure
           - Returning transformed list of dicts

    Args:
        connector_name: Name of the connector
        tenant_id: ID of the tenant
        **kwargs: Additional keyword arguments

    .. deprecated:: 3.20.0
        Use the connector-side asset-mapper pattern (typed records →
        ``map_<entity>()`` → ``pyatlan_v9`` Asset) instead — will be removed in
        v4.0. See ``docs/upgrade-guide-v3.md``.
    """

    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        warnings.warn(
            "QueryBasedTransformer is deprecated; use the connector-side asset-mapper "
            "pattern (typed records → map_<entity>() → pyatlan_v9 Asset) instead — "
            "will be removed in v4.0. See docs/upgrade-guide-v3.md.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.connector_name = connector_name
        self.tenant_id = tenant_id
        # Exclusion shapes already reported, so the diagnostics in
        # get_sql_column_expressions are emitted once instead of once per input batch.
        # Split by tier because only the input-dependent one needs bounding.
        self._reported_authoring_mistakes: set[tuple[Any, ...]] = set()
        self._reported_gating_shapes: set[tuple[Any, ...]] = set()
        self._gating_reporting_capped = False
        self.entity_class_definitions: dict[str, str] = (
            get_yaml_query_template_path_mappings(
                assets=[
                    "TABLE",
                    "COLUMN",
                    "DATABASE",
                    "SCHEMA",
                    "EXTRAS-PROCEDURE",
                    "FUNCTION",
                ]
            )
        )

    def quote_column_name(self, column_name: str) -> str:
        """Handle column names that contain dots by quoting them.

        Args:
            column_name: The column name to process

        Returns:
            The processed column name, quoted if it contains dots
        """
        if _is_quoted_identifier(column_name):
            return column_name
        if "." in column_name:
            return f'"{column_name}"'
        return column_name

    def convert_to_sql_expression(
        self,
        column: dict[str, str],
        is_literal: bool = False,
        quote_source_identifier: bool = False,
    ) -> str:
        """Process a single column definition into a SQL column expression.

        The rendered form is ``{expression} AS {alias}``. DuckDB restricts only the
        *expression* slot: a bare reserved keyword there is a parse error
        (``SELECT column AS column`` raises ``ParserException``), while the alias
        slot accepts every keyword tried (``SELECT x AS column`` parses). So the
        quoting below targets the expression slot alone, and quoting is
        behaviour-neutral because DuckDB keeps quoted identifiers
        case-insensitive.

        Args:
            column: The column definition dictionary
            is_literal: Whether the value is a literal. The literal branch puts
                ``name`` in the expression slot too -- it references the column
                :meth:`prepare_template_and_attributes` appends for that literal
                -- so ``name`` is quoted there for the same reason.
            quote_source_identifier: Whether ``source_query`` resolved as a plain
                column *reference* (see :meth:`get_sql_column_expressions`) and may
                therefore be quoted. Off for the ``source_columns``-driven route,
                whose ``source_query`` is an arbitrary SQL expression: quoting a
                bare identifier there would turn a zero-argument SQL keyword such
                as ``current_date`` into a column lookup that resolves to nothing.

        Returns:
            A SQL column expression string
        """
        column["name"] = self.quote_column_name(column["name"])
        if is_literal:
            return f"{_quote_bare_identifier(column['name'])} AS {column['name']}"
        source = column["source_query"]
        if quote_source_identifier:
            source = _quote_bare_identifier(source)
        return f"{source} AS {column['name']}"

    def get_sql_column_expressions(
        self,
        sql_template: dict[str, Any],
        dataframe: pa.Table,
        default_attributes: dict[str, Any],
        yaml_path: str | None = None,
    ) -> tuple[list[str], list[dict[str, str]] | None]:
        """Get the columns and literal columns for the SQL query.

        A declared field that resolves to neither an available column nor a recognised
        literal cannot be emitted, and is reported rather than dropped silently -- the
        resulting symptom is an attribute missing from published output, which nothing
        downstream can detect. An excluded field whose shape indicates an authoring
        mistake is reported at WARNING with the edit that fixes it; one that could still
        resolve on a run supplying its declared inputs is by-design gating and stays at
        DEBUG.

        Because DEBUG is off in production, the gated class would otherwise stay as
        invisible as the silent drop this replaces -- and a typo that happens to look
        like a plausible column name is statically indistinguishable from an optional
        enrichment, so it lands there. Any exclusion therefore also emits a per-template
        INFO summary of declared-vs-emitted counts naming the excluded fields, which is
        recorded in production and makes "this attribute was declared but never
        published" queryable without a line per field. See
        :meth:`_report_excluded_fields` for the reporting cadence.

        Args:
            sql_template (Dict[str, Any]): The SQL template
            dataframe (pa.Table): The Table to get columns from
            default_attributes (Dict[str, Any]): The default attributes to add to the SQL query
            yaml_path (str | None): Template path, used to identify the template in warnings

        Returns:
            A list of column expressions for the SQL query
        """
        columns: list[str] = []
        literal_columns: list[dict[str, str]] = []
        never_resolvable: list[tuple[str, Any, str]] = []
        inputs_absent: list[dict[str, Any]] = []
        column_names = list(dataframe.schema.names) + list(default_attributes.keys())

        for column in sql_template["columns"]:
            # The two routes into the non-literal branch are kept apart because
            # only one of them proves ``source_query`` is a column reference:
            #
            # * source_columns declared and all present -- source_query is an
            #   arbitrary SQL expression over them, quoted at the author's
            #   discretion;
            # * source_query itself names an available column -- a plain
            #   reference, which is exactly the case a bare DuckDB reserved
            #   keyword (``column``, ``order``, ``qualify``) needs quoting for.
            #
            # Route precedence matches the original ``or``, so which expression a
            # column renders to is unchanged; what is new is the quoting decision
            # and matching an already-quoted value under the name it denotes.
            via_source_columns = bool(column.get("source_columns")) and all(
                col in column_names for col in column["source_columns"]
            )
            resolves_as_column_reference = (
                not via_source_columns
                and _resolution_key(column["source_query"]) in column_names
            )
            if via_source_columns or resolves_as_column_reference:
                columns.append(
                    self.convert_to_sql_expression(
                        column,
                        quote_source_identifier=resolves_as_column_reference,
                    )
                )

            elif (
                isinstance(column["source_query"], float)
                or isinstance(column["source_query"], int)
                or isinstance(column["source_query"], bool)
                or column["source_query"] is None
            ) or (
                isinstance(column["source_query"], str)
                and column["source_query"].startswith("'")
                and column["source_query"].endswith("'")
                and len(column["source_query"]) > 1
            ):
                literal_columns.append(column)
                columns.append(self.convert_to_sql_expression(column, is_literal=True))

            elif (remedy := _unresolvable_remedy(column)) is not None:
                never_resolvable.append(
                    (column["name"], column["source_query"], remedy)
                )

            else:
                inputs_absent.append(column)

        if never_resolvable or inputs_absent:
            self._report_excluded_fields(
                yaml_path or "<template>",
                declared=len(sql_template["columns"]),
                emitted=len(columns),
                never_resolvable=never_resolvable,
                inputs_absent=inputs_absent,
            )

        return columns, literal_columns or None

    def _report_excluded_fields(
        self,
        template: str,
        *,
        declared: int,
        emitted: int,
        never_resolvable: list[tuple[str, Any, str]],
        inputs_absent: list[dict[str, Any]],
    ) -> None:
        """Report the fields the SQL gate excluded, once per distinct exclusion shape.

        Emits, for a shape not yet reported by this transformer:

        * one INFO summary of declared-vs-emitted counts naming both excluded sets, which
          is the production-visible record that a declared attribute never reached
          published output.
        * one WARNING per never-resolvable field, carrying the edit that fixes it.
        * one DEBUG per field gated on inputs absent this run, carrying its declared
          inputs for diagnosis.

        Callers stream batches of one typename through an unchanging schema, so a report
        per call repeats identical lines hundreds of times on a large tenant. Reports are
        deduplicated instead, on two separate keys because the two tiers have different
        bounds:

        * authoring mistakes on ``(template, never-resolvable names)``, kept uncapped.
          That set is a pure function of the template's static ``source_query`` and
          ``source_columns`` text, so it cannot grow with the number of batches at all --
          it is bounded by the connector's template count. Capping it would mean a genuine
          authoring mistake in a template first seen late in a run goes unnamed.
        * everything input-dependent -- the summary and the gated-field DEBUG lines -- on
          ``(template, never-resolvable names, gated names)``, capped, since the gated set
          varies with each batch's schema and so is the only tier a pathological caller
          could grow without limit. A schema that genuinely changes mid-run reports again,
          because a newly gated field is new information.

        Nothing is emitted at all when every declared field is emitted, so a healthy run
        stays silent at every level.

        Passing ``_MAX_REPORTED_GATING_SHAPES`` distinct gated shapes stops that tier with
        a single WARNING: once its diagnostics are incomplete, the absence of a line no
        longer means the field was published, and that is worth a human's attention.
        """
        authoring_shape = (template, tuple(name for name, _, _ in never_resolvable))
        gating_shape = authoring_shape + (
            tuple(column["name"] for column in inputs_absent),
        )

        new_authoring = (
            bool(never_resolvable)
            and authoring_shape not in self._reported_authoring_mistakes
        )
        new_gating = gating_shape not in self._reported_gating_shapes
        capped = new_gating and (
            len(self._reported_gating_shapes) >= _MAX_REPORTED_GATING_SHAPES
        )
        if capped:
            new_gating = False

        if new_gating:
            self._reported_gating_shapes.add(gating_shape)
            logger.info(
                "Template %s excluded %d of %d declared fields from the generated SQL, "
                "so those attributes will be missing from published output: %d authoring "
                "mistake(s) %s, %d gated on inputs absent from this run %s",
                template,
                declared - emitted,
                declared,
                len(never_resolvable),
                [name for name, _, _ in never_resolvable],
                len(inputs_absent),
                [column["name"] for column in inputs_absent],
            )

        if new_authoring:
            self._reported_authoring_mistakes.add(authoring_shape)
            for name, source_query, remedy in never_resolvable:
                logger.warning(
                    "Template field %r dropped from %s: source_query %r matched no "
                    "available column and is not a recognised literal, and its shape "
                    "indicates an authoring mistake, so the attribute will be missing "
                    "from published output. %s",
                    name,
                    template,
                    source_query,
                    remedy,
                )

        if new_gating:
            for column in inputs_absent:
                logger.debug(
                    "Template field %r skipped from %s: declared inputs absent from "
                    "this run (source_query %r, source_columns %r)",
                    column["name"],
                    template,
                    column["source_query"],
                    column.get("source_columns"),
                )

        if capped and not self._gating_reporting_capped:
            self._gating_reporting_capped = True
            logger.warning(
                "Excluded-field summaries and input-gated field diagnostics suppressed "
                "after %d distinct template and gated field-set combinations; "
                "authoring-mistake warnings continue to be reported",
                _MAX_REPORTED_GATING_SHAPES,
            )

    def generate_sql_query(
        self,
        yaml_path: str,
        dataframe: pa.Table,
        default_attributes: dict[str, Any],
    ) -> tuple[str, list[dict[str, str]] | None]:
        """
        Generate a SQL query from a YAML template and a DataFrame.

        Args:
            yaml_path (str): The path to the YAML template
            dataframe (pa.Table): The Table to reference for column names
            default_attributes (Dict[str, Any]): The default attributes to add to the SQL query

        Returns:
            str: The generated SQL query
        """
        with open(yaml_path, "r", encoding="utf-8") as f:
            sql_template = yaml.safe_load(f)

        sql_template["columns"] = flatten_yaml_columns(sql_template["columns"])

        columns, literal_columns = self.get_sql_column_expressions(
            sql_template, dataframe, default_attributes, yaml_path=yaml_path
        )

        sql_query = textwrap.dedent(f"""
        SELECT
            {",".join(columns)}
        FROM dataframe
        """)
        return sql_query, literal_columns or None

    def _build_struct(self, level: dict, prefix: str = "") -> None:  # type: ignore[return]
        """No-op shim — struct building moved to get_grouped_dataframe_by_prefix.

        Kept as a no-op so callers that were patching this in tests do not blow up.
        (The enclosing class is itself deprecated; see the class notice.)

        Args:
            level (dict): The current level of the struct hierarchy
            prefix (str): The prefix for the current struct level
        """
        if level is None:
            raise BuildStructLevelRequiredError()
        if prefix is None:
            raise BuildStructPrefixRequiredError()

    def get_grouped_dataframe_by_prefix(self, table: pa.Table) -> list[dict[str, Any]]:
        """Convert flat dot-notation columns to nested dicts.

        We have a flat structured table with columns that have dot notation.
        For example columns like ``attributes.name``, ``attributes.qualifiedName``
        are converted into nested dicts:
        ``{"attributes": {"name": ..., "qualifiedName": ...}}``.

        Args:
            table (pa.Table): Table with flat dot-notation column names

        Returns:
            list[dict[str, Any]]: List of nested dicts
        """

        def collapse_all_none(value: Any) -> Any:
            """Collapse a dict where every (recursive) leaf is None into None itself."""
            if not isinstance(value, dict):
                return value
            collapsed = {k: collapse_all_none(v) for k, v in value.items()}
            return None if all(v is None for v in collapsed.values()) else collapsed

        result = []
        for row_dict in table.to_pylist():
            nested: dict[str, Any] = {}
            for key, value in row_dict.items():
                parts = key.split(".")
                current = nested
                for part in parts[:-1]:
                    child = current.get(part)
                    if not isinstance(child, dict):
                        child = {}
                        current[part] = child
                    current = child
                final_key = parts[-1]
                if not isinstance(current.get(final_key), dict):
                    current[final_key] = value
            result.append({k: collapse_all_none(v) for k, v in nested.items()})
        return result

    def prepare_template_and_attributes(
        self,
        dataframe: pa.Table,
        workflow_id: str,
        workflow_run_id: str,
        connection_qualified_name: str | None = None,
        connection_name: str | None = None,
        entity_sql_template_path: str | None = None,
    ) -> tuple[pa.Table, str]:
        """
        Prepare the entity SQL template and the default attributes for the Table.

        Args:
            dataframe (pa.Table): Input Table
            workflow_id (str): ID of the workflow
            workflow_run_id (str): ID of the workflow run
            connection_qualified_name (str): Qualified name of the connection
            connection_name (str): Name of the connection
            entity_sql_template_path (str): Path to the SQL template

        Returns:
            Tuple[pa.Table, str]: Table with default attributes added and the entity SQL template
        """
        import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow

        # prepare default attributes as scalar values
        default_attributes: dict[str, Any] = {
            "connection_qualified_name": connection_qualified_name,
            "connection_name": connection_name,
            "tenant_id": self.tenant_id,
            "last_sync_workflow_name": workflow_id,
            "last_sync_run": workflow_run_id,
            "last_sync_run_at": int(datetime.now(UTC).timestamp() * 1000),
            "connector_name": AtlanConnectorType.get_connector_name(
                connection_qualified_name
            ),
        }
        entity_sql_template, literal_columns = self.generate_sql_query(
            entity_sql_template_path, dataframe, default_attributes=default_attributes
        )

        # Add literal columns to default_attributes
        default_attributes.update(
            {
                column["name"].strip('"').strip("'"): (
                    column["source_query"].strip("'")
                    if isinstance(column["source_query"], str)
                    else column["source_query"]
                )
                for column in literal_columns or []
            }
        )

        # Append default attribute columns to the table. The decision is
        # row-local: a genuine source value always wins for its row, and the
        # default/literal fills the rows that have none — so the output does
        # not depend on batch membership or record order.
        import pyarrow.compute as pc  # noqa: PLC0415 — optional dep: pyarrow

        n = len(dataframe)
        for col_name, value in default_attributes.items():
            names = dataframe.schema.names
            if col_name not in names:
                dataframe = dataframe.append_column(col_name, pa.array([value] * n))
                continue
            column = dataframe.column(col_name)
            if column.null_count == 0 or value is None:
                continue
            if pa.types.is_null(column.type):
                # untyped all-null column: there is no source type to
                # preserve, so the default legitimately defines the column
                # (fill_null cannot cast a null-typed column)
                replacement = pa.array([value] * n)
            else:
                # fill_null does not type-check: a str default against a list
                # column silently explodes into single characters. Cast first
                # so representable defaults coerce and unrepresentable ones
                # fail loudly, naming the collision — the same answer whatever
                # the batch's null shape.
                try:
                    fill = pa.scalar(value).cast(column.type)
                except (
                    pa.ArrowInvalid,
                    pa.ArrowNotImplementedError,
                    pa.ArrowTypeError,
                ) as exc:
                    raise IncompatibleDefaultTypeError(
                        message=(
                            f"default {col_name!r} ({type(value).__name__}) is "
                            f"incompatible with source column type {column.type}; "
                            "rename the colliding extractor key"
                        ),
                        expectation=f"default castable to {column.type}",
                        observed=type(value).__name__,
                        location=col_name,
                    ) from exc
                if column.null_count == n:
                    # keep the column's type; a bare rebuild would re-infer
                    # the default's own
                    replacement = pa.array([fill.as_py()] * n, type=column.type)
                else:
                    replacement = pc.fill_null(column, fill)
            dataframe = dataframe.set_column(
                names.index(col_name), col_name, replacement
            )

        return dataframe, entity_sql_template

    def transform_metadata(  # type: ignore
        self,
        typename: str,
        dataframe: pa.Table | pd.DataFrame | list[dict[str, Any]],
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions: dict[str, type[Any]] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]] | None:
        """Transform records using SQL executed through DuckDB"""
        import sys  # noqa: PLC0415

        import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow

        # Readers (e.g. ParquetFileReader) return pandas; bridge it to the
        # pyarrow Table this transformer operates on. Producing a pandas
        # DataFrame in the first place requires pandas to already be
        # installed and imported, so probing sys.modules here (rather than
        # importing pandas unconditionally) never forces the optional
        # dependency on callers passing a pa.Table or list[dict] input.
        pd = sys.modules.get("pandas")

        if isinstance(dataframe, list):
            if dataframe:
                # pa.Table.from_pylist infers the schema from the first record
                # only, silently dropping any key it lacks; build the table
                # column-wise over the union of keys instead.
                all_keys = dict.fromkeys(key for record in dataframe for key in record)
                dataframe = pa.Table.from_pydict(
                    {key: [record.get(key) for record in dataframe] for key in all_keys}
                )
            else:
                dataframe = None
        elif pd is not None and isinstance(dataframe, pd.DataFrame):
            dataframe = pa.Table.from_pandas(dataframe, preserve_index=False)
        if dataframe is None or len(dataframe) == 0:
            return None

        # Load the YAML template for the given typename
        typename = typename.upper()
        self.entity_class_definitions = (
            entity_class_definitions or self.entity_class_definitions
        )
        entity_sql_template_path = self.entity_class_definitions.get(typename)
        if not entity_sql_template_path:
            raise SqlTransformNotRegisteredError(
                message=f"No SQL transformation registered for {typename}"
            )

        # prepare the SQL to run on the dataframe and the default attributes
        dataframe, entity_sql_template = self.prepare_template_and_attributes(
            dataframe,
            workflow_id,
            workflow_run_id,
            connection_qualified_name=kwargs.get("connection_qualified_name"),
            connection_name=kwargs.get("connection_name"),
            entity_sql_template_path=entity_sql_template_path,
        )

        # run the SQL on the table via DuckDB
        from application_sdk.common.incremental.storage.duckdb_utils import (  # noqa: PLC0415
            DuckDBConnectionManager,
        )

        logger.debug(
            "Running transformer for asset typename=%s sql=%s",
            typename,
            entity_sql_template,
        )
        with DuckDBConnectionManager() as db:
            db.connection.register("dataframe", dataframe)
            result_table = db.connection.sql(entity_sql_template).to_arrow_table()

        # Convert flat dot-notation columns into nested dicts
        return self.get_grouped_dataframe_by_prefix(result_table)
