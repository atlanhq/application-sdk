import json
from pathlib import Path
from typing import Any, Dict, Optional

import faker
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from tests.scale_data_generator.config_loader import ConfigLoader, OutputFormat


class DataGenerator:
    def __init__(self, config_loader: ConfigLoader):
        self.config_loader = config_loader
        self.fake = faker.Faker()
        self.output_format = OutputFormat.CSV
        self.output_dir = None
        self.file_handlers = {}
        self.unique_values = {}

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

        if field_type == "string":
            return fake_method.word()
        elif field_type == "integer":
            return fake_method.random_int(min=0, max=1000000)
        elif field_type == "float":
            return fake_method.pyfloat(left_digits=3, right_digits=2)
        elif field_type == "boolean":
            return fake_method.boolean()
        elif field_type == "date":
            return fake_method.date()
        elif field_type == "datetime":
            return fake_method.datetime()
        elif field_type == "email":
            return fake_method.email()
        elif field_type == "phone":
            return fake_method.phone_number()
        elif field_type == "address":
            return fake_method.address()
        elif field_type == "name":
            return fake_method.name()

        return fake_method.word()

    def _get_derived_value(
        self, derived_field: str, parent_data: Dict[str, Any]
    ) -> Any:
        """Get value from parent table for derived fields."""
        table_name, field_name = derived_field.split(".")
        if table_name not in self.file_handlers:
            raise ValueError(f"Parent table {table_name} not generated yet")

        parent_record = parent_data.get(table_name)
        if not parent_record or field_name not in parent_record:
            raise ValueError(f"Derived field {field_name} not found in {table_name}")

        return parent_record[field_name]

    def _initialize_output_file(self, table_name: str) -> None:
        """Initialize output file for a table based on format."""
        file_path = Path(self.output_dir) / f"{table_name}.{self.output_format}"

        if self.output_format == OutputFormat.JSON:
            # Open file in append mode
            self.file_handlers[table_name] = open(file_path, "w")

        elif self.output_format == OutputFormat.CSV:
            self.file_handlers[table_name] = open(file_path, "w")

        elif self.output_format == OutputFormat.PARQUET:
            # For parquet, we'll collect records in a list temporarily
            self.file_handlers[table_name] = {"records": [], "path": file_path}

    def _write_record(
        self, table_name: str, record: Dict[str, Any], is_last: bool = False
    ) -> None:
        """Write a single record to the output file."""
        if table_name not in self.file_handlers:
            self._initialize_output_file(table_name)

        if self.output_format == OutputFormat.JSON:
            self.file_handlers[table_name].write(json.dumps(record) + "\n")

        elif self.output_format == OutputFormat.CSV:
            df = pd.DataFrame([record])
            # Write header only if file is empty
            df.to_csv(
                self.file_handlers[table_name],
                header=self.file_handlers[table_name].tell() == 0,
                index=False,
            )

        elif self.output_format == OutputFormat.PARQUET:
            # Collect records and write in batches
            self.file_handlers[table_name]["records"].append(record)
            if len(self.file_handlers[table_name]["records"]) >= 1000 or is_last:
                self._write_parquet_batch(table_name)

    def _write_parquet_batch(self, table_name: str) -> None:
        """Write collected records as a parquet batch."""
        if self.file_handlers[table_name]["records"]:
            df = pd.DataFrame(self.file_handlers[table_name]["records"])
            table = pa.Table.from_pandas(df)
            if Path(self.file_handlers[table_name]["path"]).exists():
                pq.write_to_dataset(table, self.file_handlers[table_name]["path"])
            else:
                pq.write_table(table, self.file_handlers[table_name]["path"])
            self.file_handlers[table_name]["records"] = []

    def _close_files(self) -> None:
        """Close all open file handlers."""
        for table_name, handler in self.file_handlers.items():
            if self.output_format == OutputFormat.JSON:
                handler.close()
            elif self.output_format == OutputFormat.CSV:
                handler.close()
            elif self.output_format == OutputFormat.PARQUET:
                self._write_parquet_batch(table_name)

    def generate_data(self, output_format: OutputFormat, output_dir: str) -> None:
        """Generate and write data for all tables in the hierarchy."""
        self.output_format = output_format
        self.output_dir = output_dir
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        hierarchy = self.config_loader.get_hierarchy()
        try:
            self._generate_hierarchical_data(hierarchy, dict())
        finally:
            self._close_files()

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
