from typing import Any, Dict, Optional

import faker
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.engine import Engine
from testcontainers.mysql import MySqlContainer
from testcontainers.postgres import PostgresContainer

from application_sdk.test_utils.scale_data_generator.test_on_source.config_loader import (
    ConfigLoader,
)


class DataGenerator:
    # Mapping of faker types to SQLAlchemy types
    TYPE_MAPPING = {
        "string": String,
        "integer": Integer,
        "float": Float,
        "boolean": Boolean,
        "datetime": DateTime,
        "date": DateTime,
        "email": String,
        "phone": String,
        "address": String,
        "name": String,
    }

    # Mapping of faker types to faker methods
    FAKER_METHODS = {
        "string": "word",
        "integer": "random_int",
        "float": "pyfloat",
        "boolean": "boolean",
        "datetime": "date_time",
        "date": "date",
        "email": "email",
        "phone": "phone_number",
        "address": "address",
        "name": "name",
    }

    def __init__(
        self,
        config_loader: ConfigLoader,
        source_type: str,
    ):
        """Initialize the data generator.

        Args:
            config_loader: ConfigLoader instance with hierarchy configuration
            source_type: Type of database (e.g., 'postgresql', 'mysql')
        """
        self.config_loader = config_loader
        self.source_type = source_type
        self.fake = faker.Faker()
        self.engine: Optional[Engine] = None
        self.metadata = MetaData()
        self.container = None

    def _start_container(self) -> Dict[str, Any]:
        """Start a test container and return connection parameters."""
        if self.source_type == "postgresql":
            self.container = PostgresContainer("postgres:latest")
            self.container.start()
            return {
                "username": self.container.POSTGRES_USER,
                "password": self.container.POSTGRES_PASSWORD,
                "host": self.container.get_container_host_ip(),
                "port": str(self.container.get_exposed_port(5432)),
                "db_name": self.container.POSTGRES_DB,
            }
        elif self.source_type == "mysql":
            self.container = MySqlContainer("mysql:latest")
            self.container.start()
            return {
                "username": self.container.MYSQL_USER,
                "password": self.container.MYSQL_ROOT_PASSWORD,
                "host": self.container.get_container_host_ip(),
                "port": str(self.container.get_exposed_port(3306)),
                "db_name": self.container.MYSQL_DATABASE,
            }
        else:
            raise ValueError(f"Unsupported database type: {self.source_type}")

    def _get_connection_url(self, connection_params: Dict[str, Any]) -> str:
        """Generate SQLAlchemy connection URL based on source type."""
        if self.source_type == "postgresql":
            return f"postgresql://{connection_params['username']}:{connection_params['password']}@{connection_params['host']}:{connection_params['port']}/{connection_params['db_name']}"
        elif self.source_type == "mysql":
            return f"mysql+pymysql://{connection_params['username']}:{connection_params['password']}@{connection_params['host']}:{connection_params['port']}/{connection_params['db_name']}"
        else:
            raise ValueError(f"Unsupported database type: {self.source_type}")

    def _generate_column_type(self) -> str:
        """Generate a random column type from supported types."""
        return self.fake.random_element(elements=list(self.TYPE_MAPPING.keys()))

    def _generate_column_value(self, column_type: str) -> Any:
        """Generate a random value for a column using faker."""
        faker_method = getattr(self.fake, self.FAKER_METHODS[column_type])
        return faker_method()

    def _create_table(
        self,
        table_name: str,
        schema_name: str,
        num_columns: int,
        connection: Engine,
    ) -> None:
        """Create a table with random columns and insert data."""
        columns = []
        for i in range(num_columns):
            column_type = self._generate_column_type()
            columns.append(
                Column(
                    f"column_{i+1}",
                    self.TYPE_MAPPING[column_type],
                    nullable=True,
                )
            )

        table = Table(
            table_name,
            self.metadata,
            *columns,
            schema=schema_name,
        )

        # Create the table
        table.create(connection, checkfirst=True)

        # Insert data
        records = self.config_loader.get_table_records(table_name)
        for _ in range(records):
            data = {
                f"column_{i+1}": self._generate_column_value(
                    self._generate_column_type()
                )
                for i in range(num_columns)
            }
            connection.execute(table.insert().values(**data))

    def _create_view(
        self,
        view_name: str,
        schema_name: str,
        num_columns: int,
        connection: Engine,
    ) -> None:
        """Create a view with random columns and insert data."""
        # Create a temporary table for the view
        temp_table_name = f"temp_{view_name}"
        self._create_table(temp_table_name, schema_name, num_columns, connection)

        # Create view
        view_query = f"""
        CREATE OR REPLACE VIEW {schema_name}.{view_name} AS
        SELECT * FROM {schema_name}.{temp_table_name}
        """
        connection.execute(text(view_query))

    def generate_data(self) -> Dict[str, Any]:
        """Generate data in a test container according to the hierarchy configuration."""
        try:
            # Start container and get connection parameters
            connection_params = self._start_container()

            # Create engine and connect
            self.engine = create_engine(self._get_connection_url(connection_params))
            connection = self.engine.connect()

            # Get hierarchy
            hierarchy = self.config_loader.get_hierarchy()
            num_databases = hierarchy.get("records", 1)

            # Create databases
            for db_num in range(1, num_databases + 1):
                db_name = f"database_{db_num}"

                # Create database if it doesn't exist
                if self.source_type == "postgresql":
                    connection.execute(text("COMMIT"))
                    connection.execute(text(f"CREATE DATABASE {db_name}"))
                elif self.source_type == "mysql":
                    connection.execute(text(f"CREATE DATABASE IF NOT EXISTS {db_name}"))

                # Connect to the new database
                db_engine = create_engine(
                    self._get_connection_url(connection_params).replace(
                        connection_params["db_name"], db_name
                    )
                )
                db_connection = db_engine.connect()

                # Create schemas
                schemas = self.config_loader.get_table_children("databases")
                num_schemas = schemas[0].get("records", 1)

                for schema_num in range(1, num_schemas + 1):
                    schema_name = f"schema_{schema_num}"

                    # Create schema
                    if self.source_type == "postgresql":
                        db_connection.execute(
                            text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}")
                        )
                    elif self.source_type == "mysql":
                        db_connection.execute(
                            text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}")
                        )

                    # Create tables
                    tables = self.config_loader.get_table_children("schema")
                    for table_config in tables:
                        if table_config["name"] == "tables":
                            num_tables = table_config.get("records", 1)
                            for table_num in range(1, num_tables + 1):
                                table_name = f"table_{table_num}"
                                num_columns = self.config_loader.get_table_records(
                                    "columns"
                                )
                                self._create_table(
                                    table_name,
                                    schema_name,
                                    num_columns,
                                    db_connection,
                                )

                        # Create views
                        elif table_config["name"] == "views":
                            num_views = table_config.get("records", 1)
                            for view_num in range(1, num_views + 1):
                                view_name = f"view_{view_num}"
                                num_columns = self.config_loader.get_table_records(
                                    "columns"
                                )
                                self._create_view(
                                    view_name,
                                    schema_name,
                                    num_columns,
                                    db_connection,
                                )

                db_connection.close()
                db_engine.dispose()

            connection.close()

            # Return connection parameters for external use
            return connection_params

        finally:
            if self.engine:
                self.engine.dispose()

    def stop_container(self) -> None:
        """Stop and remove the test container."""
        if self.container:
            self.container.stop()
            self.container.remove()
