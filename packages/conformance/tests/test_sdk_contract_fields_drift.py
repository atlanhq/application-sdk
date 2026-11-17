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

import json
from pathlib import Path

import pytest

from conformance.suite.checks import _sdk_contract_mixins as mixins
from conformance.tools import generate_sdk_contract_fields as generator
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


# -- Loader: fail loud on a missing, unreadable or empty table -----------------


def _valid_table() -> dict[str, object]:
    return json.loads(mixins.DATA_PATH.read_text(encoding="utf-8"))


def test_loader_accepts_the_committed_table() -> None:
    table = mixins.load_table()
    assert set(table) == set(mixins._SECTIONS)


def test_loader_rejects_a_missing_table(tmp_path: Path) -> None:
    with pytest.raises(mixins.SdkContractTableError, match="missing or not valid"):
        mixins.load_table(tmp_path / "absent.json")


def test_loader_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(mixins.SdkContractTableError, match="missing or not valid"):
        mixins.load_table(path)


@pytest.mark.parametrize("section", mixins._SECTIONS)
def test_loader_rejects_an_empty_section(tmp_path: Path, section: str) -> None:
    table = _valid_table()
    table[section] = {}
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    with pytest.raises(mixins.SdkContractTableError, match=section):
        mixins.load_table(path)


def test_loader_rejects_a_table_missing_a_contract_root(tmp_path: Path) -> None:
    table = _valid_table()
    base_fields = table["contract_base_fields"]
    assert isinstance(base_fields, dict)
    del base_fields["PublishInputMixin"]
    path = tmp_path / "rootless.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    with pytest.raises(mixins.SdkContractTableError, match="PublishInputMixin"):
        mixins.load_table(path)


def test_loader_bootstrap_switch_yields_empty_registries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(mixins.BOOTSTRAP_ENV, "1")
    table = mixins.load_table(tmp_path / "absent.json")
    assert all(not table[section] for section in mixins._SECTIONS)


def test_generator_and_loader_agree_on_path_and_switch() -> None:
    # The generator duplicates both rather than importing them, because
    # importing the loader module validates the file the generator may need
    # to recreate.
    assert generator.DATA_PATH == mixins.DATA_PATH
    assert generator.BOOTSTRAP_ENV == mixins.BOOTSTRAP_ENV
