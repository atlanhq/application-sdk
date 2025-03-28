from typing import Any, Dict, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from application_sdk.test_utils.scale_data_generator.test_on_source.config_loader import (
    ConfigLoader,
)


class DataCleaner:
    def __init__(
        self,
        config_loader: ConfigLoader,
        db_name: str,
        source_type: str,
        connection_params: Dict[str, Any],
    ):
        """Initialize the data cleaner.

        Args:
            config_loader: ConfigLoader instance with hierarchy configuration
            db_name: Name of the database to connect to
            source_type: Type of database (e.g., 'postgresql', 'mysql')
            connection_params: Additional connection parameters
        """
        self.config_loader = config_loader
        self.db_name = db_name
        self.source_type = source_type
        self.connection_params = connection_params
        self.engine: Optional[Engine] = None

    def _get_connection_url(self) -> str:
        """Generate SQLAlchemy connection URL based on source type."""
        base_params = {
            "username": self.connection_params.get("username", "postgres"),
            "password": self.connection_params.get("password", "postgres"),
            "host": self.connection_params.get("host", "localhost"),
            "port": self.connection_params.get("port", "5432"),
        }

        if self.source_type == "postgresql":
            return f"postgresql://{base_params['username']}:{base_params['password']}@{base_params['host']}:{base_params['port']}/{self.db_name}"
        elif self.source_type == "mysql":
            return f"mysql+pymysql://{base_params['username']}:{base_params['password']}@{base_params['host']}:{base_params['port']}/{self.db_name}"
        else:
            raise ValueError(f"Unsupported database type: {self.source_type}")

    def _drop_view(self, view_name: str, schema_name: str, connection: Engine) -> None:
        """Drop a view from the database."""
        drop_query = f"""
        DROP VIEW IF EXISTS {schema_name}.{view_name}
        """
        connection.execute(text(drop_query))

    def _drop_table(self, table_name: str, schema_name: str, connection: Engine) -> None:
        """Drop a table from the database."""
        drop_query = f"""
        DROP TABLE IF EXISTS {schema_name}.{table_name} CASCADE
        """
        connection.execute(text(drop_query))

    def _drop_schema(self, schema_name: str, connection: Engine) -> None:
        """Drop a schema from the database."""
        if self.source_type == "postgresql":
            drop_query = f"""
            DROP SCHEMA IF EXISTS {schema_name} CASCADE
            """
        else:  # mysql
            drop_query = f"""
            DROP SCHEMA IF EXISTS {schema_name}
            """
        connection.execute(text(drop_query))

    def _drop_database(self, db_name: str, connection: Engine) -> None:
        """Drop a database."""
        if self.source_type == "postgresql":
            # PostgreSQL requires a separate connection to drop databases
            connection.execute(text("COMMIT"))
            connection.execute(text(f"DROP DATABASE IF EXISTS {db_name}"))
        else:  # mysql
            connection.execute(text(f"DROP DATABASE IF EXISTS {db_name}"))

    def clean_data(self) -> None:
        """Clean up all generated data according to the hierarchy configuration."""
        try:
            # Create engine and connect
            self.engine = create_engine(self._get_connection_url())
            connection = self.engine.connect()

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
                db_connection = db_engine.connect()

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
                                self._drop_table(table_name, schema_name, db_connection)

                        elif table_config["name"] == "views":
                            num_views = table_config.get("records", 1)
                            # Drop views in reverse order
                            for view_num in range(num_views, 0, -1):
                                view_name = f"view_{view_num}"
                                self._drop_view(view_name, schema_name, db_connection)

                    # Drop the schema
                    self._drop_schema(schema_name, db_connection)

                db_connection.close()
                db_engine.dispose()

                # Drop the database
                self._drop_database(db_name, connection)

            connection.close()

        finally:
            if self.engine:
                self.engine.dispose()
