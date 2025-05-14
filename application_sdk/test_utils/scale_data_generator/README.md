# Scale Data Generator

A powerful tool for generating scale synthetic data for SQL Source testing. This tool allows you to generate realistic test data based on your database schema and configuration.

## Features

- Generate synthetic data for multiple databases, schemas, and tables
- Support for various data types (int, float, string, date, email, name)
- Configurable output formats (CSV, JSON, Parquet)
- YAML-based configuration and schema definition
- Scalable data generation with customizable row counts

## Installation

```
$ uv sync --all-extras --all-groups
$ uv run python -m application_sdk.test_utils.scale_data_generator.driver --config-path application_sdk/test_utils/scale_data_generator/examples/config.yaml --output-format json --output-dir application_sdk/test_utils/scale_data_generator/output
```

## Usage

### Generate Data

- Review the configuration file `application_sdk/test_utils/scale_data_generator/config.yaml` to understand the data hierarchy and the data types.
- Run the tool by using the following command:

```bash
python application_sdk/test_utils/scale_data_generator/driver.py
```

### Development

- You can run the tool by using the following launch.json configuration in VSCode:

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Run Scale Data Generator",
            "type": "debugpy",
            "request": "launch",
            "program": "${workspaceFolder}/application_sdk/test_utils/scale_data_generator/driver.py",
            "cwd": "${workspaceFolder}",
            "justMyCode": false,
            "env": {
                "PYTHONPATH": "${workspaceFolder}",
            }
        }
    ]
}
```
