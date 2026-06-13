"""SQL filter pipeline — query preparation, regex normalization, database name extraction.

This module consolidates the SQL-specific filter utilities that were previously
in ``common/utils.py``. All functions relate to preparing SQL queries with
include/exclude filter patterns.
"""

import glob
import json
import os
import re
from typing import Any, TypeAlias

from application_sdk.common.sql_filters_errors import InvalidSqlFilterError
from application_sdk.errors import AppError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def safe_substitute_placeholders(template: str, mapping: dict[str, str]) -> str:
    """Single-pass placeholder substitution — replacement values are never re-scanned.

    Replaces every key in *mapping* that appears in *template* using a single
    ``re.sub`` pass.  Contrast with chained ``str.replace()`` calls, where each
    call operates on the *mutated* string: a replacement value that happens to
    contain the literal text of a later placeholder would be re-substituted,
    silently corrupting the output.  ``re.sub`` with alternation scans the
    original string positions exactly once and never re-interprets replacement
    values (including ``\\1``/``\\g<0>`` backref sequences).

    Args:
        template: The SQL (or any string) to substitute into.
        mapping: ``{placeholder: replacement}`` pairs.  Keys are treated as
                 literals (``re.escape``d) so ``{`` / ``}`` in placeholder
                 names are not special.

    Returns:
        The template with every matching key replaced by its value, in a
        single left-to-right scan.
    """
    if not mapping:
        return template
    pattern = re.compile("|".join(re.escape(k) for k in mapping))
    return pattern.sub(lambda m: mapping[m.group()], template)


# ---------------------------------------------------------------------------
# SQL injection deny-list for filter values (BLDX-518).
#
# Filter values (include_filter, exclude_filter, temp_table_regex) are
# substituted into SQL templates via ``str.replace`` and end up inside
# quoted SQL string literals (the template author is contractually
# required to wrap each substitution in single quotes — see the warning
# in ``templates/sql_metadata_extractor.SqlMetadataExtractor._prepare_sql``).
#
# To make that contract attacker-resistant, we forbid the SQL-meaningful
# sequences that have no legitimate use inside a regex pattern destined
# for a quoted literal:
#
#   * ``'`` — string-literal delimiter (would close the surrounding quote)
#   * ``"`` — identifier delimiter in dialects that allow it
#   * ``;`` — statement separator (stacked-query injection)
#   * ``--`` — SQL line comment (eats the rest of the line)
#   * ``/*`` / ``*/`` — block comment
#   * ``\x00`` — null byte; some drivers truncate on it
#
# Regex meta-characters that legitimately appear in filter values
# (``^``, ``$``, ``.``, ``*``, ``+``, ``?``, ``|``, ``()``, ``[]``, ``\``,
# ``{}``, single ``-``) remain allowed. This is a deny-list, not an
# allow-list — a strict allow-list would break virtually every existing
# filter pattern.
# ---------------------------------------------------------------------------

_FORBIDDEN_FILTER_SEQUENCES: tuple[str, ...] = (
    "'",
    '"',
    ";",
    "--",
    "/*",
    "*/",
    "\x00",
)

# Used by Pydantic ``Field(pattern=...)`` on filter-shaped fields. Forbids
# the single-character members of ``_FORBIDDEN_FILTER_SEQUENCES``;
# multi-character sequences (``--``, ``/*``, ``*/``) are caught by the
# dedicated ``validate_filter_no_sql_injection`` validator below.
SAFE_FILTER_PATTERN: str = r"^[^'\";\x00]*$"
_SAFE_FILTER_PATTERN = SAFE_FILTER_PATTERN  # backward-compat alias

FilterValue: TypeAlias = list[str] | str | dict[str, dict[str, Any]]

_MAX_FILTER_NESTING_DEPTH = 4


def validate_filter_no_sql_injection(v: Any) -> Any:
    """Reject filter values containing SQL-meaningful character sequences.

    See the module docstring for the rationale behind each forbidden
    sequence. Applies to:

    * Raw regex strings — checked directly.
    * JSON-encoded strings (legacy AE payload shape) — parsed first;
      the parsed dict is then checked. The JSON's own structural
      double-quotes are stripped by the parse, so we only ever apply
      the deny-list to the actual filter contents.
    * Structured dict filters (``FilterMap``) — checks every key and
      every string in every value list.

    Returns ``v`` unchanged when the value is safe; raises ``ValueError``
    on the first forbidden sequence encountered so the caller (Pydantic
    or a hand-rolled helper) surfaces a clear error before any SQL
    template substitution.

    Args:
        v: A filter value — either a raw regex string, a JSON-encoded
            filter map string, or a normalized ``FilterMap`` dict.

    Returns:
        The same value if it passes all deny-list checks.

    Raises:
        ValueError: When any forbidden sequence is found.
    """

    def _check_str(label: str, value: str) -> None:
        for seq in _FORBIDDEN_FILTER_SEQUENCES:
            if seq in value:
                raise ValueError(
                    f"SQL-unsafe sequence {seq!r} not allowed in filter {label}: {value!r}"
                )

    def _check_value(label: str, value: Any, depth: int = 0) -> None:
        if depth > _MAX_FILTER_NESTING_DEPTH:
            raise ValueError(
                f"Filter nesting exceeds maximum depth {_MAX_FILTER_NESTING_DEPTH}"
            )
        if isinstance(value, str):
            _check_str(label, value)
        elif isinstance(value, list):
            for item in value:
                _check_value("value", item, depth + 1)
        elif isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if isinstance(nested_key, str):
                    _check_str("key", nested_key)
                _check_value("value", nested_value, depth + 1)

    if isinstance(v, str):
        # Legacy AE callers pass the filter as a JSON-encoded string. The
        # JSON syntax itself carries double-quotes that would trip the
        # deny-list, so parse first and validate the resulting dict;
        # fall back to treating the string as a raw regex otherwise.
        # We return the ORIGINAL string unchanged on success — the
        # downstream consumer expects whichever shape it was given,
        # not a coerced one.
        stripped = v.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:  # conformance: ignore[E009] JSON parse probe; None signals "not a dict filter", handled below
                parsed = None
            if isinstance(parsed, dict):
                # Validate the parsed shape; ignore the returned value
                # and keep ``v`` (the original JSON string) intact.
                validate_filter_no_sql_injection(parsed)
                return v
        _check_str("value", v)
    elif isinstance(v, dict):
        for key, values in v.items():
            if isinstance(key, str):
                _check_str("key", key)
            _check_value("value", values)
    return v


# ---------------------------------------------------------------------------
# Legacy quoted-CSV → v3 alternation regex (HYP-1560).
#
# Pre-v3 SaaS-agent callers serialised multi-value filter fields
# (``temp-table-regex``, ``include-filter``, ``exclude-filter``) as a
# quoted, comma-separated list — e.g. ``'"PREFIX_A.","PREFIX_B."'`` for
# two temp-table prefixes the agent then matched independently. The v3
# contract is a single regex string, and the BLDX-518 deny-list above
# rejects the ``"`` and ``,`` characters that shape requires. Migration
# tooling that round-trips the legacy workflow specs unchanged trips
# Pydantic validation on every run after the v3.13 SDK bump.
#
# ``normalize_legacy_filter_value`` lets a migration caller explicitly
# translate the legacy shape into a deny-list-clean alternation regex
# (``A|B``) before it hits the validator. Inputs that don't match the
# legacy shape pass through unchanged, so legitimate v3 single-regex
# values aren't perturbed.
# ---------------------------------------------------------------------------

# Anchored at both ends. Matches one or more double-quoted items
# separated by literal ``,``: ``"A"``, ``"A","B"``, ``"A","B","C"``, …
# The item body intentionally allows everything except ``"`` so we
# don't try to interpret embedded escapes — the legacy SaaS agent
# never produced them. Whatever lives inside the quotes is re-checked
# against the SAFE_FILTER_PATTERN deny-list after normalisation, so a
# legacy value smuggling ``--`` or ``;`` still fails loudly.
_LEGACY_QUOTED_CSV_RE: re.Pattern[str] = re.compile(r'^"[^"]*"(?:,"[^"]*")*$')


def normalize_legacy_filter_value(value: str) -> str:
    """Translate a legacy quoted-CSV filter value into a v3 alternation regex.

    The pre-v3 SaaS agent serialised multi-value filter fields as a
    double-quoted, comma-separated list. The v3 contract expects a
    single regex string and the SQL-injection deny-list (see
    :data:`SAFE_FILTER_PATTERN`) rejects the quote/comma characters
    that shape carries, so legacy workflow specs round-tripped through
    v3 validation now fail at input decoding.

    This helper recognises the legacy shape and re-emits it as an
    equivalent regex alternation::

        '"PREFIX_A.","PREFIX_B."'  →  'PREFIX_A.|PREFIX_B.'
        '"only"'                   →  'only'
        '"x","y","z"'              →  'x|y|z'

    Values that don't match the legacy shape pass through unchanged,
    so a legitimate v3 single-regex value like ``'^prod_.*$'`` or an
    already-normalised ``'A|B'`` is returned as-is. The output is run
    through :func:`validate_filter_no_sql_injection` before being
    returned, so a legacy value with a forbidden sequence inside an
    item (e.g. ``'"name--bad"'``) raises ``ValueError`` rather than
    deferring the failure to a later SQL-substitution call site.

    Args:
        value: A filter value, possibly in the legacy quoted-CSV shape.

    Returns:
        ``value`` unchanged when it isn't a legacy quoted-CSV shape;
        otherwise the equivalent alternation regex.

    Raises:
        ValueError: When normalisation produces a value that still
            contains a sequence forbidden by the deny-list.
    """
    if not isinstance(value, str) or not _LEGACY_QUOTED_CSV_RE.match(value):
        return value

    # Each split chunk is a complete ``"..."`` token; strip the outer
    # quotes to recover the original item. Drop empty items so a stray
    # ``""`` (or ``"A",""``) collapses cleanly rather than producing a
    # bogus empty branch in the alternation regex.
    items = [chunk[1:-1] for chunk in value.split(",")]
    items = [item for item in items if item]
    if not items:
        # All-empty input — let the caller's validator surface the
        # original (still-quoted) value as the SQL-unsafe ValueError
        # it actually is, rather than silently returning ``""``.
        return value

    normalised = "|".join(items)
    validate_filter_no_sql_injection(normalised)
    return normalised


def extract_database_names_from_regex_common(
    normalized_regex: str,
    empty_default: str,
    require_wildcard_schema: bool,
) -> str:
    """Common implementation for extracting database names from regex patterns.

    Args:
        normalized_regex: The normalized regex pattern containing database.schema patterns
        empty_default: Default value to return for empty/null inputs
        require_wildcard_schema: Whether to only extract database names for wildcard schemas

    Returns:
        A regex string in the format ^(name1|name2|...)$ or default values
    """
    try:
        if not normalized_regex or normalized_regex == "^$":
            return empty_default

        if normalized_regex == ".*":
            return "'.*'"

        database_names: set[str] = set()
        patterns = normalized_regex.split("|")

        for pattern in patterns:
            try:
                if not pattern or not pattern.strip():
                    continue

                parts = pattern.split("\\.")

                if require_wildcard_schema:
                    if len(parts) < 2:
                        logger.warning("Invalid database name format: %s", pattern)
                        continue
                    db_name = parts[0].strip().lstrip("^")
                    schema_part = parts[1].strip().rstrip("$")
                    # Accept both legacy ``*`` and anchored ``.*`` wildcard forms.
                    if not (
                        db_name
                        and db_name not in (".*", "^$")
                        and schema_part in ("*", ".*")
                    ):
                        continue
                else:
                    if not parts:
                        continue
                    db_name = parts[0].strip().lstrip("^")
                    if not (db_name and db_name not in (".*", "^$")):
                        continue

                if re.match(r"^[a-zA-Z_][a-zA-Z0-9_$-]*$", db_name):
                    database_names.add(db_name)
                else:
                    logger.warning("Invalid database name format: %s", db_name)
            except Exception:
                logger.warning("Error processing pattern: %s", pattern, exc_info=True)
                continue

        if not database_names:
            return empty_default
        return f"'^({'|'.join(sorted(database_names))})$'"

    except Exception:
        logger.error(
            "Error extracting database names from regex: %s",
            normalized_regex,
            exc_info=True,
        )
        return empty_default


def transform_posix_regex(regex_pattern: str) -> str:
    r"""Transform regex pattern for POSIX compatibility.

    Rules:
    1. Add ^ before each database name before \.
    2. Add an additional . between \. and * if * follows \.

    Example: 'dev\.public$|dev\.atlan_test_schema$|wide_world_importers\.*'
    Becomes: '^dev\.public$|^dev\.atlan_test_schema$|^wide_world_importers\..*'
    """
    if not regex_pattern:
        return regex_pattern

    patterns = regex_pattern.split("|")
    transformed_patterns = []

    for pattern in patterns:
        if not pattern.startswith("^"):
            pattern = "^" + pattern
            pattern = re.sub(r"\\\.\*", r"\..*", pattern)
        transformed_patterns.append(pattern)

    return "|".join(transformed_patterns)


def prepare_query(
    query: str | None,
    workflow_args: dict[str, Any],
    temp_table_regex_sql: str | None = "",
    use_posix_regex: bool | None = False,
) -> str | None:
    """Prepare a SQL query by applying include/exclude filters.

    Modifies the provided SQL query using filters and settings defined in
    workflow_args. The include and exclude filters determine which data should
    be included or excluded from the query.

    Args:
        query: The base SQL query string to modify with filters.
        workflow_args: Dictionary containing metadata and workflow-related arguments.
        temp_table_regex_sql: SQL snippet for excluding temporary tables.
        use_posix_regex: Whether to use POSIX-compatible regex.

    Returns:
        The prepared SQL query with filters applied, or None if an error occurs.
    """
    try:
        if not query:
            logger.warning("SQL query is not set.")
            return None

        metadata = workflow_args.get("metadata", {})

        include_filter = metadata.get("include-filter") or "{}"
        exclude_filter = metadata.get("exclude-filter") or "{}"
        # Defense in depth (BLDX-518): callers of ``prepare_query`` pass a
        # raw ``workflow_args`` dict, bypassing the Pydantic validators
        # that gate the typed extraction inputs. Run the same deny-list
        # here so user-controlled metadata can't smuggle SQL escape
        # sequences into the substituted templates.
        temp_table_regex = metadata.get("temp-table-regex")
        if isinstance(temp_table_regex, str):
            # Translate legacy quoted-CSV shape to a v3 alternation regex
            # before applying the deny-list. Without this, raw-dict
            # callers (which bypass the typed ``ExtractionInput``
            # validators) still trip on migrated workflow specs.
            temp_table_regex = normalize_legacy_filter_value(temp_table_regex)
            validate_filter_no_sql_injection(temp_table_regex)
        if temp_table_regex and temp_table_regex_sql is not None:
            temp_table_regex_sql = temp_table_regex_sql.format(
                exclude_table_regex=temp_table_regex
            )
        else:
            temp_table_regex_sql = ""

        normalized_include_regex, normalized_exclude_regex = prepare_filters(
            include_filter, exclude_filter
        )

        if use_posix_regex:
            normalized_include_regex_posix = transform_posix_regex(
                normalized_include_regex
            )
            normalized_exclude_regex_posix = transform_posix_regex(
                normalized_exclude_regex
            )

        include_databases = extract_database_names_from_regex_common(
            normalized_regex=normalized_include_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )
        exclude_databases = extract_database_names_from_regex_common(
            normalized_regex=normalized_exclude_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        exclude_empty_tables = metadata.get("exclude_empty_tables", False)
        exclude_views = metadata.get("exclude_views", False)

        if use_posix_regex:
            return query.format(
                include_databases=include_databases,
                exclude_databases=exclude_databases,
                normalized_include_regex=normalized_include_regex_posix,
                normalized_exclude_regex=normalized_exclude_regex_posix,
                temp_table_regex_sql=temp_table_regex_sql,
                exclude_empty_tables=exclude_empty_tables,
                exclude_views=exclude_views,
            )
        else:
            return query.format(
                include_databases=include_databases,
                exclude_databases=exclude_databases,
                normalized_include_regex=normalized_include_regex,
                normalized_exclude_regex=normalized_exclude_regex,
                temp_table_regex_sql=temp_table_regex_sql,
                exclude_empty_tables=exclude_empty_tables,
                exclude_views=exclude_views,
            )
    except AppError as e:
        error_message = str(e).split(": ", 1)[-1] if ": " in str(e) else str(e)
        logger.error(
            "Error preparing query: error_code=%s error_message=%s",
            e.code,
            error_message,
            exc_info=True,
        )
        return None


async def get_database_names(
    sql_client, workflow_args, fetch_database_sql
) -> list[str] | None:
    """Get database names from include-filter or by running a SQL query.

    Args:
        sql_client: SQL client for executing queries.
        workflow_args: The workflow arguments.
        fetch_database_sql: SQL query to fetch all database names.

    Returns:
        List of database names.
    """
    raw_include = workflow_args.get("metadata", {}).get("include-filter", {})
    # BLDX-518: validate before any downstream caller can interpolate these
    # names into SQL. ``prepare_query`` does its own validation on the
    # fall-through path; this catches the case where the include-filter
    # carries database names that are returned directly to the caller.
    # HYP-1560: normalise the legacy quoted-CSV shape on string inputs
    # so migrated workflow specs validate cleanly here too.
    if isinstance(raw_include, str):
        raw_include = normalize_legacy_filter_value(raw_include)
    if isinstance(raw_include, (str, dict)):
        validate_filter_no_sql_injection(raw_include)
    database_names = parse_filter_input(raw_include)

    database_names = [
        re.sub(r"^[^\w]+|[^\w]+$", "", database_name)
        for database_name in database_names
    ]
    if not database_names:
        temp_table_regex_sql = workflow_args.get("metadata", {}).get(
            "temp-table-regex", ""
        )
        prepared_query = prepare_query(
            query=fetch_database_sql,
            workflow_args=workflow_args,
            temp_table_regex_sql=temp_table_regex_sql,
            use_posix_regex=True,
        )
        database_dataframe = await sql_client.get_results(prepared_query)
        database_names = list(database_dataframe["database_name"])
    return database_names


def parse_filter_input(
    filter_input: str | dict[str, Any] | None,
) -> dict[str, Any]:
    """Robustly parse filter input from various formats.

    Args:
        filter_input: Can be None, empty string, JSON string, or dict.

    Returns:
        Parsed filter dictionary (empty dict if input is invalid/empty).
    """
    if not filter_input:
        return {}
    if isinstance(filter_input, dict):
        return filter_input
    if isinstance(filter_input, str):
        if not filter_input.strip():
            return {}
        try:
            return json.loads(filter_input)
        except json.JSONDecodeError as e:
            raise InvalidSqlFilterError(cause=e) from e


def prepare_filters(
    include_filter_str: str, exclude_filter_str: str
) -> tuple[str, str]:
    """Prepare include/exclude filters for SQL queries.

    Args:
        include_filter_str: The include filter string.
        exclude_filter_str: The exclude filter string.

    Returns:
        Tuple of (normalized_include_regex, normalized_exclude_regex).

    Raises:
        CommonError: If JSON parsing fails for either filter.
    """
    # BLDX-518: gate raw input before ``parse_filter_input``. The parse
    # step raises on non-JSON strings, which would mask the deny-list
    # check for raw-regex callers (``"prefix'injection"`` would surface
    # as a JSONDecodeError instead of the SQL-unsafe ValueError).
    # HYP-1560: normalise legacy quoted-CSV strings first so migrated
    # workflow specs surviving the v3.13 SDK bump still validate.
    if isinstance(include_filter_str, str):
        include_filter_str = normalize_legacy_filter_value(include_filter_str)
        validate_filter_no_sql_injection(include_filter_str)
    if isinstance(exclude_filter_str, str):
        exclude_filter_str = normalize_legacy_filter_value(exclude_filter_str)
        validate_filter_no_sql_injection(exclude_filter_str)

    include_filter = parse_filter_input(include_filter_str)
    exclude_filter = parse_filter_input(exclude_filter_str)

    # Re-validate the parsed shapes — defense in depth for callers that
    # pass a dict directly without going through the string path.
    validate_filter_no_sql_injection(include_filter)
    validate_filter_no_sql_injection(exclude_filter)

    normalized_include_filter_list = normalize_filters(include_filter, True)
    normalized_exclude_filter_list = normalize_filters(exclude_filter, False)

    normalized_include_regex = (
        "|".join(normalized_include_filter_list)
        if normalized_include_filter_list
        else ".*"
    )
    normalized_exclude_regex = (
        "|".join(normalized_exclude_filter_list)
        if normalized_exclude_filter_list
        else "^$"
    )

    return normalized_include_regex, normalized_exclude_regex


def normalize_filters(
    filter_dict: dict[str, FilterValue], is_include: bool
) -> list[str]:
    """Normalize filter dict to fully-anchored ``db.schema`` regex patterns.

    Each emitted pattern is anchored with ``^`` and ``$`` so that callers
    using POSIX ``~`` (substring match by default) get exact ``db.schema``
    semantics rather than substring matches.

    Mapping:
        - ``{"^db$": []}`` or ``{"^db$": "*"}`` → ``^db\\..*$`` (every schema in db)
        - ``{"^db$": ["^sch$"]}`` → ``^db\\.sch$`` (exactly that schema)
        - ``{"^db$": {"^sch$": {}}}`` → ``^db\\.sch$`` (APITree object shape)

    The previous implementation emitted unanchored ``db\\.*`` (literal dot,
    zero-or-more), which substring-matched targets like ``something.atlan_dev``
    when the user only meant database ``dev``.

    Args:
        filter_dict: Filter dict, e.g. ``{"^db$": ["^schema$"]}``.
        is_include: Whether this is an include filter (currently unused — both
            include and exclude need the same anchored shape; kept for API
            stability).

    Returns:
        List of anchored regex segments suitable for joining with ``|``.
    """
    normalized_filter_list: list[str] = []
    for filtered_db, filtered_schemas in filter_dict.items():
        db = filtered_db.strip("^$")

        if filtered_schemas == "*" or not filtered_schemas:
            normalized_filter_list.append(f"^{db}\\..*$")
            continue

        if isinstance(filtered_schemas, dict):
            filtered_schemas = list(filtered_schemas.keys())

        if isinstance(filtered_schemas, list):
            for schema in filtered_schemas:
                sch = schema.strip("^$")
                normalized_filter_list.append(f"^{db}\\.{sch}$")

    return normalized_filter_list


def read_sql_files(
    queries_prefix: str = f"{os.path.dirname(os.path.abspath(__file__))}/queries",
) -> dict[str, str]:
    """Read all SQL files from a directory and return as a name→content mapping.

    Args:
        queries_prefix: Directory containing SQL query files.

    Returns:
        Dictionary mapping SQL file names (uppercase, without extension) to contents.
    """
    sql_files: list[str] = glob.glob(
        os.path.join(queries_prefix, "**/*.sql"),
        recursive=True,
    )

    result: dict[str, str] = {}
    for file in sql_files:
        with open(file, encoding="utf-8") as f:
            result[os.path.basename(file).upper().replace(".SQL", "")] = (
                f.read().strip()
            )

    return result
