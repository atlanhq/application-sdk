"""Tests for .github/scripts/dataforge_lineage_targets.py (FND-5375).

The OIDC exchange, the DataForge PUT and the tenant key calls are injected, so
these drive the pool choice, minting, the per-cloud upsert and the
no-secret-in-output rule without a network.
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
            "client_id": "c",
            "client_secret": "s",
        },
        "aws": {
            "tenant": "sys-aws.example.invalid",
            "api_key": "ADMIN-AWS",
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
            "client_id": "c",
            "client_secret": "s",
        }
    }
)

Put = Callable[[str, str, str, str, str], None]


def _env(**extra: str) -> dict[str, str]:
    return {
        "DATAFORGE_RESOURCE_ID": "res-1",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        **extra,
    }


def _noop_put(base_url: str, token: str, resource_id: str, url: str, key: str) -> None:
    del base_url, token, resource_id, url, key


def _recorder() -> tuple[list[tuple[str, str, str, str, str]], Put]:
    calls: list[tuple[str, str, str, str, str]] = []

    def put(base_url: str, token: str, resource_id: str, url: str, key: str) -> None:
        calls.append((base_url, token, resource_id, url, key))

    return calls, put


def _mint(
    minted: list[tuple[str, str, str]] | None = None,
) -> Callable[[str, str, str], str]:
    def mint(tenant_url: str, harness_key: str, run_label: str) -> str:
        if minted is not None:
            minted.append((tenant_url, harness_key, run_label))
        return "LIN-" + tenant_url.removeprefix("https://").split(".")[0]

    return mint


def _no_purge(tenant_url: str, harness_key: str, now_ms: int) -> int:
    del tenant_url, harness_key, now_ms
    return 0


def _register(
    environ: dict[str, str],
    put: Put = _noop_put,
    mint: Callable[[str, str, str], str] | None = None,
    purge: Callable[[str, str, int], int] = _no_purge,
) -> list[str]:
    return register(
        environ,
        base_url="https://df.example.invalid",
        token_for=lambda: "tok",
        put=put,
        mint=mint or _mint(),
        purge=purge,
    )


def test_the_system_matrix_wins_and_each_tenant_gets_a_minted_key() -> None:
    calls, put = _recorder()
    minted: list[tuple[str, str, str]] = []
    clouds = _register(
        _env(E2E_SYSTEM_TENANT_MATRIX_JSON=_SYSTEM, E2E_TENANT_MATRIX_JSON=_CONNECTOR),
        put,
        mint=_mint(minted),
    )
    assert clouds == ["aws", "azure"]
    assert minted == [
        ("https://sys-aws.example.invalid", "ADMIN-AWS", "42-1"),
        ("https://sys-az.example.invalid", "ADMIN-AZ", "42-1"),
    ]
    assert [(c[3], c[4]) for c in calls] == [
        ("https://sys-aws.example.invalid", "LIN-sys-aws"),
        ("https://sys-az.example.invalid", "LIN-sys-az"),
    ]
    assert {c[1] for c in calls} == {"tok"} and {c[2] for c in calls} == {"res-1"}


def test_the_harness_key_is_never_registered() -> None:
    calls, put = _recorder()
    _register(_env(E2E_SYSTEM_TENANT_MATRIX_JSON=_SYSTEM), put)
    assert calls and all(not c[4].startswith("ADMIN-") for c in calls)


def test_the_connector_matrix_is_used_without_a_system_one() -> None:
    calls, put = _recorder()
    _register(_env(E2E_TENANT_MATRIX_JSON=_CONNECTOR), put)
    assert [c[3] for c in calls] == ["https://con-aws.example.invalid"]


def test_no_matrix_is_an_error() -> None:
    with pytest.raises(LineageTargetError, match="neither"):
        _register(_env())


def test_no_resource_id_is_an_error() -> None:
    with pytest.raises(LineageTargetError, match="DATAFORGE_RESOURCE_ID"):
        _register({"E2E_TENANT_MATRIX_JSON": _CONNECTOR})


def test_an_entry_without_a_tenant_names_the_cloud_not_a_value() -> None:
    bad = json.dumps({"gcp": {"tenant": "", "api_key": "SECRET-VALUE"}})
    with pytest.raises(LineageTargetError) as exc:
        tenants_from_matrix(bad, "E2E_TENANT_MATRIX_JSON")
    assert "gcp" in str(exc.value) and "tenant" in str(exc.value)
    assert "SECRET-VALUE" not in str(exc.value)


def test_a_failed_cloud_fails_the_step_but_the_others_are_still_tried(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def mint(tenant_url: str, harness_key: str, run_label: str) -> str:
        if "sys-aws" in tenant_url:
            raise LineageTargetError(
                "tenant POST /api/service/apikeys failed: HTTP 403"
            )
        return "LIN-ok"

    calls, put = _recorder()
    with pytest.raises(LineageTargetError, match="1 of 2"):
        _register(_env(E2E_SYSTEM_TENANT_MATRIX_JSON=_SYSTEM), put, mint=mint)
    assert [c[3] for c in calls] == ["https://sys-az.example.invalid"]
    assert "::error::lineage target for cloud aws" in capsys.readouterr().out


def test_keys_and_hosts_are_masked_and_never_printed_raw(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register(_env(E2E_SYSTEM_TENANT_MATRIX_JSON=_SYSTEM))
    lines = capsys.readouterr().out.splitlines()
    masked = {
        line.removeprefix("::add-mask::")
        for line in lines
        if line.startswith("::add-mask::")
    }
    assert {
        "ADMIN-AWS",
        "ADMIN-AZ",
        "LIN-sys-aws",
        "LIN-sys-az",
        "sys-aws.example.invalid",
    } <= masked
    unmasked = [line for line in lines if not line.startswith("::add-mask::")]
    assert not any(
        t in line for line in unmasked for t in ("ADMIN-", "LIN-", "example.invalid")
    )


def test_mint_sends_a_persona_less_short_lived_key_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, str, dict[str, object] | None]] = []

    def call(
        method: str, url: str, harness_key: str, body: dict[str, object] | None = None
    ) -> object:
        seen.append((method, url, harness_key, body))
        return {"attributes": {"accessToken": "NEW-TOKEN"}}

    monkeypatch.setattr(dlt, "_tenant_call", call)
    assert (
        dlt.mint_lineage_key("https://t.example.invalid", "ADMIN", "42-1")
        == "NEW-TOKEN"
    )
    method, url, key, body = seen[0]
    assert (method, url, key) == (
        "POST",
        "https://t.example.invalid/api/service/apikeys",
        "ADMIN",
    )
    assert body is not None
    assert body["displayName"] == "dataforge-lineage-e2e-42-1"
    assert body["personas"] == [] and body["personaQualifiedNames"] == []
    assert body["validitySeconds"] == dlt.LINEAGE_KEY_VALIDITY_SECONDS


def test_mint_without_a_token_in_the_answer_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dlt, "_tenant_call", lambda *a, **k: {"attributes": {}})
    with pytest.raises(LineageTargetError, match="no token"):
        dlt.mint_lineage_key("https://t.example.invalid", "ADMIN", "1")


def test_purge_deletes_only_this_steps_expired_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 10_000_000_000
    old = now - dlt.LINEAGE_KEY_VALIDITY_SECONDS * 1000 - 1
    fresh = now - 1000
    listing = {
        "records": [
            {
                "guid": "g-old",
                "displayName": "dataforge-lineage-e2e-1-1",
                "attributes": {"createdAt": old},
            },
            {
                "guid": "g-fresh",
                "displayName": "dataforge-lineage-e2e-2-1",
                "attributes": {"createdAt": fresh},
            },
            {
                "guid": "g-other",
                "displayName": "someone-elses-key",
                "attributes": {"createdAt": old},
            },
        ]
    }
    deleted: list[str] = []

    def call(
        method: str, url: str, harness_key: str, body: dict[str, object] | None = None
    ) -> object:
        if method == "GET":
            return listing
        deleted.append(url.rsplit("/", 1)[1])
        return {}

    monkeypatch.setattr(dlt, "_tenant_call", call)
    assert (
        dlt.purge_expired_lineage_keys("https://t.example.invalid", "ADMIN", now) == 1
    )
    assert deleted == ["g-old"]


def test_purge_never_fails_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    def call(*a: object, **k: object) -> object:
        raise LineageTargetError("tenant GET /api/service/apikeys failed: HTTP 500")

    monkeypatch.setattr(dlt, "_tenant_call", call)
    assert dlt.purge_expired_lineage_keys("https://t.example.invalid", "ADMIN", 0) == 0


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
    assert {"E2E_TENANT_MATRIX_JSON", "E2E_SYSTEM_TENANT_MATRIX_JSON"} <= set(
        step["env"]
    )
    assert "dataforge_lineage_targets.py" in step["run"]
