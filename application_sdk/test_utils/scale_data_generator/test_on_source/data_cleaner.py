from typing import Optional

from sqlalchemy import DDL, MetaData, Table, create_engine, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.schema import DropSchema, DropTable

from application_sdk.test_utils.scale_data_generator.test_on_source.config_loader import (
    ConfigLoader,
)


class DataCleaner:
    def __init__(
        self,
        config_loader: ConfigLoader,
        connection_url: str,
    ):
        """Initialize the data cleaner.

        Args:
            config_loader: ConfigLoader instance with hierarchy configuration
            connection_url: Connection URL
        """
        self.config_loader = config_loader
        self.connection_url = connection_url
        self.engine: Optional[Engine] = None

    def _get_connection_url(self) -> str:
        return self.connection_url

    def _drop_view(
        self, view_name: str, schema_name: str, connection: Connection
    ) -> None:
        """Drop a view from the database."""
        inspector = inspect(connection)
        if view_name in inspector.get_view_names(schema=schema_name):
            metadata = MetaData()
            view = Table(view_name, metadata, schema=schema_name)
            connection.execute(DropTable(view))

    def _drop_table(
        self, table_name: str, schema_name: str, connection: Connection
    ) -> None:
        """Drop a table from the database."""
        inspector = inspect(connection)
        if table_name in inspector.get_table_names(schema=schema_name):
            metadata = MetaData()
            table = Table(table_name, metadata, schema=schema_name)
            connection.execute(DropTable(table))

    def _drop_schema(self, schema_name: str, connection: Connection) -> None:
        """Drop a schema from the database."""
        inspector = inspect(connection)
        if schema_name in inspector.get_schema_names():
            schema = DropSchema(schema_name, cascade=True)
            connection.execute(schema)

    def _drop_database(self, db_name: str, connection: Connection) -> None:
        """Drop a database."""
        connection.execute(DDL(f"DROP DATABASE IF EXISTS {db_name}"))

    def clean_data(self) -> None:
        """Clean up all generated data according to the hierarchy configuration."""
        try:
            # Create engine and connect
            self.engine = create_engine(self._get_connection_url())
            with self.engine.connect() as connection:
                # Get hierarchy
                hierarchy = self.config_loader.get_hierarchy()
                num_databases = hierarchy.get("records", 1)

                # Drop databases in reverse order
                for db_num in range(num_databases, 0, -1):
                    db_name = f"database_{db_num}"

                    # Connect to the database
                    db_engine = create_engine(
                        self._get_connection_url().replace(self.db_name, db_name)
                    )
                    with db_engine.connect() as db_connection:
                        # Get schemas
                        schemas = self.config_loader.get_table_children("databases")
                        num_schemas = schemas[0].get("records", 1)

                        # Drop schemas in reverse order
                        for schema_num in range(num_schemas, 0, -1):
                            schema_name = f"schema_{schema_num}"

                            # Get tables and views
                            tables = self.config_loader.get_table_children("schema")
                            for table_config in tables:
                                if table_config["name"] == "tables":
                                    num_tables = table_config.get("records", 1)
                                    # Drop tables in reverse order
                                    for table_num in range(num_tables, 0, -1):
                                        table_name = f"table_{table_num}"
                                        self._drop_table(
                                            table_name, schema_name, db_connection
                                        )

                                elif table_config["name"] == "views":
                                    num_views = table_config.get("records", 1)
                                    # Drop views in reverse order
                                    for view_num in range(num_views, 0, -1):
                                        view_name = f"view_{view_num}"
                                        self._drop_view(
                                            view_name, schema_name, db_connection
                                        )

                            # Drop the schema
                            self._drop_schema(schema_name, db_connection)

                        # Drop the database
                        self._drop_database(db_name, connection)

        finally:
            if self.engine:
                self.engine.dispose()
