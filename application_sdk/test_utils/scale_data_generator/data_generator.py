import random
from pathlib import Path
from typing import Any, Dict

import faker
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from testcontainers.postgres import PostgresContainer

from application_sdk.test_utils.scale_data_generator.config_loader import (
    ConfigLoader,
    OutputFormat,
)
from application_sdk.test_utils.scale_data_generator.output_handler.csv_handler import (
    CsvFormatHandler,
)
from application_sdk.test_utils.scale_data_generator.output_handler.json_handler import (
    JsonFormatHandler,
)
from application_sdk.test_utils.scale_data_generator.output_handler.parquet_handler import (
    ParquetFormatHandler,
)


class DataGenerator:
    FORMAT_HANDLERS = {
        OutputFormat.JSON.value: JsonFormatHandler,
        OutputFormat.CSV.value: CsvFormatHandler,
        OutputFormat.PARQUET.value: ParquetFormatHandler,
    }

    def __init__(self, config_loader: ConfigLoader):
        self.config_loader = config_loader
        self.fake = faker.Faker()
        self.output_handler = None
        self.generator_config = config_loader.get_generator_config()
        self.postgres_container = None
        self.engines: Dict[str, Engine] = {}  # Store SQLAlchemy engines

    def _generate_value(
        self, field_type: str, field_config: Dict[str, Any] = None
    ) -> Any:
        """Generate a fake value based on the field type and configuration."""
        field_config = field_config or {}
        config = self.generator_config["data_distribution"]

        if field_type == "integer":
            return random.randint(*config["integer_range"])
        elif field_type == "string":
            length = random.choice(config["string_length_range"])
            return self.fake.text(max_nb_chars=length)
        elif field_type == "decimal":
            return round(random.uniform(*config["decimal_range"]), 2)
        elif field_type == "date":
            return self.fake.date_between(
                start_date=config["date_range"][0], end_date=config["date_range"][1]
            )
        elif field_type == "timestamp":
            return self.fake.date_time_between(
                start_date=config["date_range"][0], end_date=config["date_range"][1]
            )
        elif field_type == "boolean":
            return random.choice([True, False])
        return None

    def _generate_name(self, template: str, **kwargs) -> str:
        """Generate a name using the template and provided indices."""
        return template.format(**kwargs)

    def generate_data(self, output_format: OutputFormat, output_dir: str) -> None:
        """Generate and write data for all assets in the hierarchy."""
        handler_class = self.FORMAT_HANDLERS[output_format.value]
        self.output_handler = handler_class(output_dir)

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        hierarchy = self.config_loader.get_hierarchy()

        try:
            # Start PostgreSQL container using context manager
            with PostgresContainer("postgres:15") as postgres:
                self.postgres_container = postgres
                # Create admin engine for database creation
                admin_engine = create_engine(
                    f"postgresql://test:test@{postgres.get_container_host_ip()}:{postgres.get_exposed_port(5432)}/postgres"
                )
                self.engines["admin"] = admin_engine
                # Generate data
                self._generate_hierarchical_data(hierarchy)
        finally:
            if self.output_handler:
                self.output_handler.close_files()
            self._cleanup()

    def _create_database(self, db_name: str) -> None:
        """Create a new database in the PostgreSQL container."""
        # Create database using admin engine
        with self.engines["admin"].connect() as conn:
            # Disconnect all clients from the database before dropping
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            conn.commit()

        # Create a new engine for this database
        engine = create_engine(
            f"postgresql://test:test@{self.postgres_container.get_container_host_ip()}:{self.postgres_container.get_exposed_port(5432)}/{db_name}"
        )
        self.engines[db_name] = engine

    def _cleanup(self) -> None:
        """Cleanup all database engines."""
        # Close all database engines
        for engine in self.engines.values():
            engine.dispose()

    def _generate_hierarchical_data(
        self, node: Dict[str, Any], parent_indices: Dict[str, str] = None
    ) -> None:
        """Recursively generate data following the hierarchy."""
        parent_indices = parent_indices or {}
        node_type = node["name"]
        records = node.get("records", 1)

        template = self.config_loader.get_asset_template(node_type)
        definition = (
            self.config_loader.get_asset_definition(node_type)
            if node_type in ["tables", "views"]
            else None
        )

        for i in range(records):
            current_indices = {**parent_indices, f"{node_type}_index": i + 1}

            # Generate asset name
            asset_name = self._generate_name(
                template["name_template"], **current_indices
            )

            if node_type == "databases":
                current_indices["database_name"] = asset_name
                # Create new database
                self._create_database(asset_name)

            elif node_type == "schemata":
                # Create schema in database
                current_indices["schema_name"] = asset_name
                with self.engines[parent_indices["database_name"]].connect() as conn:
                    conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{asset_name}"'))
                    conn.commit()

            elif node_type in ["tables", "views"]:
                # Generate data and create table/view
                asset_data = self._generate_asset_data(
                    node_type, definition, current_indices
                )
                self._write_record(asset_name, asset_data, is_last=(i == records - 1))

                # Create table/view in database
                self._create_database_object(
                    node_type,
                    asset_name,
                    asset_data,
                    parent_indices["database_name"],
                    parent_indices["schema_name"],
                )

            # Process children
            if "children" in node:
                for child in node["children"]:
                    self._generate_hierarchical_data(child, current_indices)

    def _create_database_object(
        self,
        object_type: str,
        object_name: str,
        data: Dict[str, Any],
        database_name: str,
        schema_name: str,
    ) -> None:
        """Create a table or view in the database."""
        engine = self.engines[database_name]
        with engine.connect() as conn:
            if object_type == "tables":
                # Create table
                columns = [
                    f'"{k}" {self._get_postgres_type(v)}' for k, v in data.items()
                ]
                create_table_sql = text(f"""
                    CREATE TABLE IF NOT EXISTS "{schema_name}"."{object_name}" (
                        {", ".join(columns)}
                    )
                """)
                conn.execute(create_table_sql)

                # Insert data
                columns = list(data.keys())
                values = [data[col] for col in columns]
                insert_sql = text(f"""
                    INSERT INTO "{schema_name}"."{object_name}"
                    ({", ".join(f'"{col}"' for col in columns)})
                    VALUES ({", ".join([":val" + str(i) for i in range(len(columns))])})
                """)
                conn.execute(
                    insert_sql, {f"val{i}": val for i, val in enumerate(values)}
                )
                conn.commit()

            elif object_type == "views":
                # Create view
                view_definition = self._get_view_definition(
                    object_name, data, schema_name
                )
                create_view_sql = text(f"""
                    CREATE OR REPLACE VIEW "{schema_name}"."{object_name}" AS
                    {view_definition}
                """)
                conn.execute(create_view_sql)
                conn.commit()

    def _get_postgres_type(self, value: Any) -> str:
        """Convert Python type to PostgreSQL type."""
        if isinstance(value, bool):
            return "BOOLEAN"
        elif isinstance(value, int):
            return "INTEGER"
        elif isinstance(value, float):
            return "DECIMAL(15,2)"
        elif isinstance(value, str):
            return "TEXT"
        else:
            return "TEXT"

    def _get_view_definition(
        self, view_name: str, data: Dict[str, Any], schema_name: str
    ) -> str:
        """Generate a simple view definition based on the data."""
        # For simplicity, we'll create a view that selects from a related table
        columns = [f'"{col}"' for col in data.keys()]
        return f"""
            SELECT {", ".join(columns)}
            FROM "{schema_name}"."{view_name}_base"
        """

    def _write_record(
        self, asset_name: str, record: Dict[str, Any], is_last: bool = False
    ) -> None:
        """Write a single record using the configured output handler."""
        if self.output_handler:
            self.output_handler.write_record(asset_name, record, is_last)

    def _generate_asset_data(
        self, asset_type: str, definition: Dict[str, Any], indices: Dict[str, str]
    ) -> Dict[str, Any]:
        """Generate data for a table or view based on its definition."""
        data = {}

        if asset_type == "tables":
            for column in definition["columns"]:
                column_name = (
                    self._generate_name(column["name_template"], **indices)
                    if "name_template" in column
                    else column["name"]
                )

                data[column_name] = self._generate_value(
                    column["type"],
                    {k: v for k, v in column.items() if k not in ["name", "type"]},
                )

        elif asset_type == "views":
            # For views, we'll generate sample data based on the view definition
            for column in definition["columns"]:
                column_name = (
                    self._generate_name(column["name_template"], **indices)
                    if "name_template" in column
                    else column["name"]
                )

                data[column_name] = self._generate_value(column["type"])

        return data
