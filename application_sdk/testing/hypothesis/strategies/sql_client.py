"""Deprecated home of the SQL client strategies.

.. deprecated:: 3.59.0
    Every strategy here moved to
    :mod:`application_sdk.testing.hypothesis.strategies.clients.sql`, which
    duplicated this module. The old names are served from there, with a
    warning, until v4.0.0.
"""

from application_sdk import _deprecation

#: name -> (replacement, why). Served by __getattr__ below (PEP 562).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "mock_sql_row_strategy": (
        "application_sdk.testing.hypothesis.strategies.clients.sql.mock_sql_row_strategy",
        "the SQL client strategies live in one module now",
    ),
    "mock_sql_column_description_strategy": (
        "application_sdk.testing.hypothesis.strategies.clients.sql.mock_sql_column_description_strategy",
        "the SQL client strategies live in one module now",
    ),
    "mock_sql_batch_strategy": (
        "application_sdk.testing.hypothesis.strategies.clients.sql.mock_sql_batch_strategy",
        "the SQL client strategies live in one module now",
    ),
    "mock_sql_query_result_strategy": (
        "application_sdk.testing.hypothesis.strategies.clients.sql.mock_sql_query_result_strategy",
        "the SQL client strategies live in one module now",
    ),
    "sql_engine_config_strategy": (
        "application_sdk.testing.hypothesis.strategies.clients.sql.sql_engine_config_strategy",
        "the SQL client strategies live in one module now",
    ),
    "sql_connection_string_strategy": (
        "application_sdk.testing.hypothesis.strategies.clients.sql.sql_connection_string_strategy",
        "the SQL client strategies live in one module now",
    ),
    "sql_error_strategy": (
        "application_sdk.testing.hypothesis.strategies.clients.sql.sql_error_strategy",
        "the SQL client strategies live in one module now",
    ),
}

#: A star import reads ``__all__`` and resolves each name through ``__getattr__``,
#: so ``import *`` keeps binding every old name, with the warning. Without it,
#: Python would enumerate only the module's real globals and skip them all.
__all__ = [*_DEPRECATED_CONSTANTS]


__getattr__ = _deprecation.deprecated_module_getattr(
    __name__, _DEPRECATED_CONSTANTS, remove_in="4.0.0"
)
