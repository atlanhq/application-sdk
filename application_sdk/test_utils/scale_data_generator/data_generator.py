from pathlib import Path
from typing import Any, Callable, Dict, Optional

import duckdb
import faker

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

    def _generate_value(
        self, field_type: str, field_config: Dict[str, Any] = None
    ) -> Any:
        """Generate a fake value based on the field type and configuration.

        Args:
            field_type: The type of field to generate
            field_config: Additional configuration including uniqueness and enum options
        """
        field_config = field_config or {}

        # Handle enum type
        if "enum" in field_config and field_config["enum"] is not None:
            return self.fake.random_element(elements=field_config["enum"])

        return self._generate_basic_value(field_type, field_config.get("unique", False))

    def _generate_basic_value(self, field_type: str, unique: bool = False) -> Any:
        """Generate a basic value based on the field type."""

        fake_method = self.fake
        if unique:
            fake_method = self.fake.unique

        type_mapping: Dict[str, Callable[[], Any]] = {
            "string": fake_method.word,
            "integer": fake_method.random_int,
            "float": fake_method.pyfloat,
            "boolean": fake_method.boolean,
            "date": fake_method.date,
            "datetime": fake_method.date_time,
            "email": fake_method.email,
            "phone": fake_method.phone_number,
            "address": fake_method.address,
            "name": fake_method.name,
            "null": lambda: None,
        }

        return type_mapping.get(field_type, lambda: None)()

    def _get_derived_value(
        self, derived_field: Any, parent_data: Dict[str, Any]
    ) -> Any:
        """Get value from parent table for derived fields."""
        if isinstance(derived_field, list):
            for field in derived_field:
                derived_value = self._get_derived_value(field, parent_data)
                if derived_value is not None:
                    return derived_value
        elif isinstance(derived_field, str):
            table_name, field_name = derived_field.split(".")
            return parent_data.get(table_name, {}).get(field_name)
        else:
            return None

    def generate_data(self, output_format: OutputFormat, output_dir: str) -> None:
        """Generate and write data for all tables in the hierarchy."""
        handler_class = self.FORMAT_HANDLERS[output_format]
        self.output_handler = handler_class(output_dir)

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        hierarchy = self.config_loader.get_hierarchy()

        try:
            self._generate_hierarchical_data(hierarchy, dict())
        finally:
            self.output_handler.close_files()

    def _write_record(
        self, table_name: str, record: Dict[str, Any], is_last: bool = False
    ) -> None:
        """Write a single record using the configured output handler."""
        self.output_handler.write_record(table_name, record, is_last)

    def _generate_hierarchical_data(
        self, hierarchy: Dict[str, Any], parent_data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Recursively generate data following the hierarchy and write directly to files."""
        table_name = hierarchy["name"]
        records_count = hierarchy.get("records", 1)
        schema = self.config_loader.get_table_schema(table_name)

        for i in range(records_count):
            record = {}
            for field in schema["table_schema"]:
                if "derived" in field:
                    record[field["name"]] = self._get_derived_value(
                        field["derived"], parent_data
                    )
                else:
                    record[field["name"]] = self._generate_value(
                        field["type"],
                        field_config={
                            "unique": field.get("unique", False),
                            "enum": field.get("values", None),
                        },
                    )

            is_last = i == records_count - 1 and "children" not in hierarchy
            self._write_record(table_name, record, is_last)

            if "children" in hierarchy:
                parent_data[table_name] = record
                for child in hierarchy["children"]:
                    self._generate_hierarchical_data(child, parent_data)
                parent_data.pop(table_name)

    def generate_duckdb_tables(self, output_dir: str) -> None:
        """Generate DuckDB tables from the generated data."""

        # the duckDB filename should be the database name:
        database_config = self.config_loader.get_database()

        database_name = database_config["name"]
        duckdb_filename = f"{database_name}.duckdb"
        duckdb_path = Path(output_dir) / duckdb_filename

        connection = duckdb.connect(duckdb_path)

        schema_config = database_config["schema"][0]
        schema_name = schema_config["name"]

        connection.sql(f"CREATE SCHEMA IF NOT EXISTS {database_name}.{schema_name}")

        for schema in schema_config["tables"]:
            table_name = schema["name"]

            connection.sql(f"""
                CREATE VIEW IF NOT EXISTS {database_name}.{schema_name}.{table_name}
                AS
                SELECT * FROM read_json("{output_dir}/{table_name}.json", auto_detect=true)
                """)

        connection.close()
