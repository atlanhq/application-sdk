import argparse

from application_sdk.scale_data_generator.config_loader import ConfigLoader
from application_sdk.scale_data_generator.data_generator import DataGenerator


def main():
    parser = argparse.ArgumentParser(
        description="Generate scaled test data based on configuration"
    )
    parser.add_argument(
        "--config-path",
        help="Path to the YAML configuration file",
        default="tests/scale_data_generator/examples/config.yaml",
    )
    parser.add_argument(
        "--output-format",
        choices=["csv", "json", "parquet"],
        help="Output format for generated data",
        default="json",
    )
    parser.add_argument(
        "--output-dir",
        help="Directory to save generated files",
        default="tests/scale_data_generator/output",
    )

    args = parser.parse_args()

    try:
        config_loader = ConfigLoader(args.config_path)
        generator = DataGenerator(config_loader)
        generator.generate_data(args.output_format, args.output_dir)
        print(
            f"Data generated successfully in {args.output_format} format at {args.output_dir}"
        )

    except Exception as e:
        print(f"Error: {str(e)}")
        exit(1)


if __name__ == "__main__":
    main()
