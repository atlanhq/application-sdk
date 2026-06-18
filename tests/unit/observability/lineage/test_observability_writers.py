import json

from application_sdk.observability.lineage import (
    create_asset_details_handler,
    write_coverage_json,
)


def test_write_coverage_json_creates_file(tmp_path):
    output_path = str(tmp_path / "sub" / "lineage-coverage.json")
    data = {"totals": {"totalAssets": 10}}
    write_coverage_json(data, output_path)

    with open(output_path) as f:
        loaded = json.load(f)
    assert loaded == data


def test_create_asset_details_handler_default_suffix(tmp_path):
    handler = create_asset_details_handler(str(tmp_path / "out-"))
    handler.write({"key": "value"})
    handler.close()

    output_file = tmp_path / "out-lineage-asset-details.json"
    assert output_file.exists()
    line = output_file.read_text().strip()
    assert json.loads(line)["key"] == "value"


def test_create_asset_details_handler_custom_suffix(tmp_path):
    handler = create_asset_details_handler(
        str(tmp_path / "out-"), suffix="custom-details"
    )
    handler.write({"a": 1})
    handler.close()

    output_file = tmp_path / "out-custom-details.json"
    assert output_file.exists()
