from typing import Dict, Iterator, Any
from application_sdk.inputs import Input
from sqlalchemy.engine import Engine
import pandas as pd
from sqlalchemy import text

from application_sdk.workflows.sql.utils import prepare_filters

class SQLQueryInput(Input):
    query: str | None
    engine: Engine | None
    def __init__(self, engine: Engine, query: str | None = None):
        self.query = query
        self.engine = engine

    def get_batched_df(self, workflow_args) -> Iterator[pd.DataFrame]:
        self._prepare_query(workflow_args)
        with self.engine.connect() as conn:
            return pd.read_sql_query(
                text(self.query), conn, chunksize=100000
            )

    def get_df(self, workflow_args: Dict[str, Any] = None) -> pd.DataFrame:
        if workflow_args:
            self._prepare_query(workflow_args)
        with self.engine.connect() as conn:
            return pd.read_sql_query(text(self.query), conn)
    
    def _prepare_query(self, workflow_args: Dict[str, Any]):
        """
        Method to prepare the query with the include and exclude filters
        """
        include_filter = workflow_args.get("metadata", workflow_args.get("form_data",{})).get("include_filter", "{}")
        exclude_filter = workflow_args.get("metadata", workflow_args.get("form_data",{})).get("exclude_filter", "{}")
        temp_table_regex = workflow_args.get("metadata", workflow_args.get("form_data",{})).get("temp_table_regex", "")
        normalized_include_regex, normalized_exclude_regex, exclude_table = prepare_filters(
            include_filter,
            exclude_filter,
            temp_table_regex,
        )
        self.query = self.query.format(
            normalized_include_regex=normalized_include_regex,
            normalized_exclude_regex=normalized_exclude_regex,
            exclude_table=exclude_table,
        )

    def get_key(self, key: str) -> Any:
        raise AttributeError("SQLQueryInput does not support get_key method")