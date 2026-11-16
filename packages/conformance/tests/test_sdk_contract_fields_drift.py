"""Drift guard for the SDK contract-field table ``_sdk_contract_mixins`` loads.

The committed ``conformance/data/sdk_contract_fields.json`` is how a consumer
app's scan resolves fields its contracts inherit from SDK bases and templates,
inside a repo that never has ``application_sdk/`` source installed. For that to
stay true the committed file must equal a fresh build from the SDK.

This test makes adding, removing or retyping an SDK contract field, or adding a
template contract, fail CI until the table is regenerated in the same PR:

    uv run atlan-application-sdk-conformance gen-sdk-contract-fields
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conformance.tools.generate_sdk_contract_fields import find_sdk_package, main

_requires_sdk = pytest.mark.skipif(
    find_sdk_package() is None,
    reason="atlan-application-sdk (test extra) is not installed",
)


@_requires_sdk
def test_committed_table_matches_sdk_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        main(["--check"])
    except SystemExit as exc:
        pytest.fail(
            f"gen-sdk-contract-fields --check exited {exc.code}:\n"
            + capsys.readouterr().err
        )


@_requires_sdk
def test_check_reports_stale_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "sdk_contract_fields.json"
    stale.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main(["--check", "--outfile", str(stale)])

    assert excinfo.value.code == 1
    assert "STALE" in capsys.readouterr().err
