import pandas as pd

from application_sdk.storage.formats.utils import (
    PARQUET_FILE_EXTENSION,
    estimate_dataframe_record_size,
)


def test_parquet_record_size_uses_snappy_sample_without_extra_factor() -> None:
    dataframe = pd.DataFrame(
        {
            "id": range(25),
            "query_text": [f"select * from table_{i}" for i in range(25)],
        }
    )
    sample = dataframe.head(10)
    expected_size = int(
        len(sample.to_parquet(index=False, compression="snappy")) / len(sample)
    )

    assert (
        estimate_dataframe_record_size(dataframe, PARQUET_FILE_EXTENSION)
        == expected_size
    )
