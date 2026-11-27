"""Tests for .github/scripts/dataforge_lineage_targets.py (FND-5375).

The OIDC exchange and the PUT are injected, so these drive the pool choice, the
per-cloud upsert and the no-secret-in-output rule without a network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import dataforge_lineage_targets as dlt
import pytest
import yaml
from dataforge_lineage_targets import LineageTargetError, register, tenants_from_matrix

_SYSTEM = json.dumps(
    {
        "azure": {
            "tenant": "sys-az.example.invalid",
            "api_key": "ADMIN-AZ",
            "lineage_api_key": "KEY-AZ",
            "client_id": "c",
            "client_secret": "s",
        },
        "aws": {
            "tenant": "sys-aws.example.invalid",
            "api_key": "ADMIN-AWS",
            "lineage_api_key": "KEY-AWS",
            "client_id": "c",
            "client_secret": "s",
        },
    }
)
_CONNECTOR = json.dumps(
    {
        "aws": {
            "tenant": "con-aws.example.invalid",
            "api_key": "ADMIN-CON",
            "lineage_api_key": "KEY-CON",
            "client_id": "c",
            "client_secret": "s",
        }
    }
)


def _env(**extra: str) -> dict[str, str]:
    return {"DATAFORGE_RESOURCE_ID": "res-1", **extra}


Put = Callable[[str, str, str, str, str], None]


def _noop_put(base_url: str, token: str, resource_id: str, url: str, key: str) -> None:
    del base_url, token, resource_id, url, key


def _recorder() -> tuple[list[tuple[str, str, str, str, str]], Put]:
    calls: list[tuple[str, str, str, str, str]] = []

    def put(base_url: str, token: str, resource_id: str, url: str, key: str) -> None:
        calls.append((base_url, token, resource_id, url, key))

    return calls, put


def test_the_system_matrix_wins_when_both_are_shared() -> None:
    calls, put = _recorder()
    clouds = register(
        _env(E2E_SYSTEM_TENANT_MATRIX_JSON=_SYSTEM, E2E_TENANT_MATRIX_JSON=_CONNECTOR),
        base_url="https://df.example.invalid",
        token_for=lambda: "tok",
        put=put,
    )
    assert clouds == ["aws", "azure"]
    assert [(c[3], c[4]) for c in calls] == [
        ("https://sys-aws.example.invalid", "KEY-AWS"),
        ("https://sys-az.example.invalid", "KEY-AZ"),
    ]
    assert {c[2] for c in calls} == {"res-1"}
    assert {c[1] for c in calls} == {"tok"}


def test_the_connector_matrix_is_used_without_a_system_one() -> None:
    calls, put = _recorder()
    register(
        _env(E2E_TENANT_MATRIX_JSON=_CONNECTOR),
        base_url="b",
        token_for=lambda: "t",
        put=put,
    )
    assert [c[3] for c in calls] == ["https://con-aws.example.invalid"]


def test_no_matrix_is_an_error() -> None:
    with pytest.raises(LineageTargetError, match="neither"):
        register(_env(), base_url="b", token_for=lambda: "t", put=_noop_put)


def test_no_resource_id_is_an_error() -> None:
    with pytest.raises(LineageTargetError, match="DATAFORGE_RESOURCE_ID"):
        register(
            {"E2E_TENANT_MATRIX_JSON": _CONNECTOR},
            base_url="b",
            token_for=lambda: "t",
            put=_noop_put,
        )


def test_an_entry_without_a_key_names_the_cloud_not_the_value() -> None:
    bad = json.dumps({"gcp": {"tenant": "", "lineage_api_key": "K"}})
    with pytest.raises(LineageTargetError) as exc:
        tenants_from_matrix(bad, "E2E_TENANT_MATRIX_JSON")
    assert "gcp" in str(exc.value) and "tenant" in str(exc.value)
    assert "K" not in str(exc.value).replace("E2E_TENANT_MATRIX_JSON", "")


def test_one_failed_cloud_fails_the_step_but_the_others_are_still_tried(
    capsys: pytest.CaptureFixture[str],
) -> None:
    tried: list[str] = []

    def put(base_url: str, token: str, resource_id: str, url: str, key: str) -> None:
        tried.append(url)
        if "sys-aws" in url:
            raise LineageTargetError(
                "PUT lineage-targets failed: HTTP 400 (bad request)"
            )

    with pytest.raises(LineageTargetError, match="1 of 2"):
        register(
            _env(E2E_SYSTEM_TENANT_MATRIX_JSON=_SYSTEM),
            base_url="b",
            token_for=lambda: "t",
            put=put,
        )
    assert len(tried) == 2
    out = capsys.readouterr().out
    assert "::error::lineage target for cloud aws" in out


def test_keys_and_hosts_are_masked_and_never_printed_raw(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, put = _recorder()
    register(
        _env(E2E_SYSTEM_TENANT_MATRIX_JSON=_SYSTEM),
        base_url="b",
        token_for=lambda: "t",
        put=put,
    )
    lines = capsys.readouterr().out.splitlines()
    masked = {
        line.removeprefix("::add-mask::")
        for line in lines
        if line.startswith("::add-mask::")
    }
    assert {
        "KEY-AWS",
        "KEY-AZ",
        "sys-aws.example.invalid",
        "https://sys-az.example.invalid",
    } <= masked
    unmasked = [line for line in lines if not line.startswith("::add-mask::")]
    assert not any("KEY-" in line or "example.invalid" in line for line in unmasked)


def test_main_reports_failure_without_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dlt.os, "environ", _env())
    assert dlt.main(["--base-url", "https://df.example.invalid/"]) == 1
    assert "::error::neither" in capsys.readouterr().out


def test_the_wake_job_registers_targets_after_the_wake_and_only_on_opt_in() -> None:
    workflow = Path(__file__).resolve().parents[2] / "workflows" / "tests-reusable.yaml"
    doc = yaml.safe_load(workflow.read_text())
    inputs = doc[True]["workflow_call"]["inputs"]
    assert inputs["dataforge-lineage-targets"]["default"] is False
    steps = doc["jobs"]["wake-dataforge-source"]["steps"]
    names = [s.get("name", "") for s in steps]
    wake = next(
        i
        for i, n in enumerate(names)
        if n.startswith("Wake the pinned dataforge source")
    )
    reg = next(
        i for i, n in enumerate(names) if n.startswith("Register this run's tenants")
    )
    assert reg > wake
    step = steps[reg]
    assert step["if"].startswith("inputs.dataforge-lineage-targets")
    assert set(step["env"]) == {
        "E2E_TENANT_MATRIX_JSON",
        "E2E_SYSTEM_TENANT_MATRIX_JSON",
    }
    assert "dataforge_lineage_targets.py" in step["run"]


def test_the_harness_api_key_is_never_registered(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls, put = _recorder()
    register(
        _env(E2E_SYSTEM_TENANT_MATRIX_JSON=_SYSTEM),
        base_url="b",
        token_for=lambda: "t",
        put=put,
    )
    assert all(not key.startswith("ADMIN-") for *_, key in calls)


def test_a_cloud_without_a_lineage_key_is_skipped_with_a_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    matrix = json.dumps(
        {
            "aws": {
                "tenant": "a.example.invalid",
                "api_key": "ADMIN",
                "lineage_api_key": "L",
            },
            "gcp": {"tenant": "g.example.invalid", "api_key": "ADMIN"},
        }
    )
    calls, put = _recorder()
    assert register(
        _env(E2E_TENANT_MATRIX_JSON=matrix),
        base_url="b",
        token_for=lambda: "t",
        put=put,
    ) == ["aws"]
    assert [c[4] for c in calls] == ["L"]
    out = capsys.readouterr().out
    assert "::warning::E2E_TENANT_MATRIX_JSON['gcp'] has no lineage_api_key" in out


def test_no_lineage_key_anywhere_is_an_error() -> None:
    matrix = json.dumps({"aws": {"tenant": "a.example.invalid", "api_key": "ADMIN"}})
    with pytest.raises(LineageTargetError, match="no cloud .* has a lineage_api_key"):
        register(
            _env(E2E_TENANT_MATRIX_JSON=matrix),
            base_url="b",
            token_for=lambda: "t",
            put=_noop_put,
        )
