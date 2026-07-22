"""Meta-tests for the P-series SDR-readiness checks (P029/P030, P037/P038/P039).

P029 catches the MSSQL regression pattern: an SDR app whose manifest.json is
missing agent_json in dag.extract.inputs.args.  The SDR worker starts, the
workflow status is "success", but no credentials are routed so no assets move.

P030 catches apps that never call self.upload(): the ENABLE_ATLAN_UPLOAD gate
is structurally unreachable, so assets never land in the Atlan tenant bucket
even when the flag is true.

P037 catches the custom-GUID-resolution pattern: an app that resolves credentials
by credential_guid only (custom vault read + resolve_credential_raw, or a bare
CredentialRef(credential_guid=...)) and never routes through an agent-aware
resolver (CredentialRef.resolve / CredentialRef.from_workflow_args), so
agent_json is ignored and agent-mode credentials never resolve.

P038 catches the mis-rooted-prefix pattern: an app that roots its object-store
output prefix ('artifacts/apps/{...}') from a workflow-input application_name
field (contract default '') instead of APPLICATION_NAME, so artifacts land under
a mis-rooted path (empty app segment) and 0 assets publish.

P039 catches the dropped-agent_json pattern: a manifest that declares
{{agent-json}} (P029 passes) but a generated extract-input contract
(AppInputContract) that subclasses the bare Input base with no agent_json field
and no extra-allow, so Pydantic silently drops the forwarded agent_json and
credentials never resolve.

All rules gate on self_deployed_runtime: true in atlan.yaml — non-SDR apps
are always skipped.  Tests cover the fire path, the silent path, and the
non-SDR skip path for each rule.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.sdr import discover, scan_all, scan_path
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ── helpers ─────────────────────────────────────────────────────────────────


_SDR_ATLAN_YAML = "self_deployed_runtime: true\nname: my-connector\n"
_NON_SDR_ATLAN_YAML = "self_deployed_runtime: false\nname: my-connector\n"

_MANIFEST_WITH_AGENT_JSON = json.dumps(
    {
        "dag": {
            "extract": {
                "inputs": {
                    "args": {
                        "agent_json": "{{agent-json}}",
                        "extraction_method": "{{extraction-method}}",
                        "host": "{{host}}",
                    }
                }
            }
        }
    },
    indent=2,
)

# Agent-capable (carries the {{agent-json}} placeholder) but the routing fields
# are nested ONLY under args.metadata — the atlan-tableau-app / snowflake shape.
_MANIFEST_AGENT_NESTED_ONLY = json.dumps(
    {
        "dag": {
            "extract": {
                "inputs": {
                    "args": {
                        "host": "{{host}}",
                        "metadata": {
                            "agent_json": "{{agent-json}}",
                            "extraction_method": "{{extraction-method}}",
                        },
                    }
                }
            }
        }
    },
    indent=2,
)

# Agent-capable, agent_json at top level, but extraction_method missing (partial).
_MANIFEST_MISSING_EXTRACTION_METHOD = json.dumps(
    {
        "dag": {
            "extract": {
                "inputs": {"args": {"agent_json": "{{agent-json}}", "host": "{{host}}"}}
            }
        }
    },
    indent=2,
)

# A non-agent entrypoint (miner/QI, clean): no {{agent-json}} placeholder → exempt.
_MANIFEST_NON_AGENT = json.dumps(
    {
        "dag": {
            "extract": {
                "inputs": {
                    "args": {
                        "extraction_method": "{{extraction-method}}",
                        "host": "{{host}}",
                    }
                }
            }
        }
    },
    indent=2,
)

_MANIFEST_WITHOUT_AGENT_JSON = json.dumps(
    {
        "dag": {
            "extract": {
                "inputs": {
                    "args": {
                        "host": "{{host}}",
                        "port": "{{port}}",
                    }
                }
            }
        }
    },
    indent=2,
)

_MANIFEST_NO_ARGS = json.dumps(
    {"dag": {"extract": {"inputs": {}}}},
    indent=2,
)

_MANIFEST_NO_INPUTS = json.dumps(
    {"dag": {"extract": {}}},
    indent=2,
)

_AGENT_ARGS = {
    "agent_json": "{{agent-json}}",
    "extraction_method": "{{extraction-method}}",
}

_MANIFEST_NO_PUBLISH = json.dumps(
    {"dag": {"extract": {"inputs": {"args": _AGENT_ARGS}}}},
    indent=2,
)

_MANIFEST_WITH_PUBLISH = json.dumps(
    {
        "dag": {
            "extract": {"inputs": {"args": _AGENT_ARGS}},
            "publish": {"activity_name": "publish_assets"},
        }
    },
    indent=2,
)


def _write(tmp_path: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _run(tmp_path: Path) -> list:
    paths = discover(tmp_path)
    return scan_all(paths, tmp_path)


def _rule_ids(findings: list) -> list[str]:
    return [f.rule_id for f in findings]


# ── Rule metadata ────────────────────────────────────────────────────────────


def test_p029_rule_metadata() -> None:
    rule = get_rule("P029")
    assert rule.name == "SdrManifestMissingAgentJson"
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.9.0"
    assert rule.category == "sdr-readiness"


def test_p030_rule_metadata() -> None:
    rule = get_rule("P030")
    assert rule.name == "SdrUploadNotCalled"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.9.0"
    assert rule.category == "sdr-readiness"


# ── P029: manifest missing agent_json ───────────────────────────────────────


def test_p029_fires_on_manifest_without_agent_json(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_WITHOUT_AGENT_JSON,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = _run(tmp_path)
    p029 = [f for f in findings if f.rule_id == "P029"]
    assert len(p029) == 1
    assert "agent_json" in p029[0].message
    assert p029[0].line == 1
    assert p029[0].column == 1


def test_p029_silent_when_manifest_has_agent_json(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_WITH_AGENT_JSON,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "P029" for f in findings)


def test_p029_silent_when_no_manifest(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "P029" for f in findings)


def test_p029_silent_on_non_sdr_app(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _NON_SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_WITHOUT_AGENT_JSON,
        },
    )
    assert not _run(tmp_path)


def test_p029_silent_when_no_atlan_yaml(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app/generated/manifest.json": _MANIFEST_WITHOUT_AGENT_JSON,
        },
    )
    assert not _run(tmp_path)


def test_p029_fires_on_missing_args_key(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_NO_ARGS,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = _run(tmp_path)
    assert any(f.rule_id == "P029" for f in findings)


def test_p029_fires_on_missing_inputs_key(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_NO_INPUTS,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = _run(tmp_path)
    assert any(f.rule_id == "P029" for f in findings)


def test_p029_fires_per_agent_capable_manifest_in_multi_ep(tmp_path: Path) -> None:
    # Two agent-capable manifests that both nest the routing fields → one
    # per-manifest finding each.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/extract/manifest.json": _MANIFEST_AGENT_NESTED_ONLY,
            "app/generated/profile/manifest.json": _MANIFEST_AGENT_NESTED_ONLY,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = [f for f in _run(tmp_path) if f.rule_id == "P029"]
    assert len(findings) == 2


def test_p029_silent_on_valid_manifest_in_multi_ep(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/extract/manifest.json": _MANIFEST_WITH_AGENT_JSON,
            "app/generated/profile/manifest.json": _MANIFEST_WITH_AGENT_JSON,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    assert not any(f.rule_id == "P029" for f in _run(tmp_path))


def test_p029_mixed_ep_one_fire_one_silent(tmp_path: Path) -> None:
    # A nested (broken) agent manifest fires; a valid one alongside it does not.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/extract/manifest.json": _MANIFEST_AGENT_NESTED_ONLY,
            "app/generated/profile/manifest.json": _MANIFEST_WITH_AGENT_JSON,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = [f for f in _run(tmp_path) if f.rule_id == "P029"]
    assert len(findings) == 1
    assert "extract" in findings[0].file


def test_p029_fires_on_nested_only_agent_routing(tmp_path: Path) -> None:
    # Tableau/Snowflake shape: agent-capable but fields nested only under metadata.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_AGENT_NESTED_ONLY,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    p029 = [f for f in _run(tmp_path) if f.rule_id == "P029"]
    assert len(p029) == 1
    assert "top" in p029[0].message.lower()
    assert "agent_json" in p029[0].message
    assert "extraction_method" in p029[0].message


def test_p029_fires_on_missing_extraction_method(tmp_path: Path) -> None:
    # agent_json present at top level but extraction_method absent → partial.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_MISSING_EXTRACTION_METHOD,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    p029 = [f for f in _run(tmp_path) if f.rule_id == "P029"]
    assert len(p029) == 1
    assert "extraction_method" in p029[0].message
    # Only the actually-missing field is listed (agent_json is present here).
    assert "'agent_json'" not in p029[0].message


def test_p029_exempts_non_agent_entrypoint(tmp_path: Path) -> None:
    # A miner/clean entrypoint (no {{agent-json}}) is exempt; the valid agent
    # crawler alongside it satisfies the app-level requirement → silent.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/crawler/manifest.json": _MANIFEST_WITH_AGENT_JSON,
            "app/generated/miner/manifest.json": _MANIFEST_NON_AGENT,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    assert not any(f.rule_id == "P029" for f in _run(tmp_path))


def test_p029_skips_invalid_json_manifest(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": "not json {{{",
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    # No crash — invalid JSON is silently skipped
    findings = _run(tmp_path)
    assert not any(f.rule_id == "P029" for f in findings)


# ── P030: upload call absent ─────────────────────────────────────────────────


def test_p030_fires_when_no_upload_call(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    findings = _run(tmp_path)
    p030 = [f for f in findings if f.rule_id == "P030"]
    assert len(p030) == 1
    assert "self.upload" in p030[0].message
    assert p030[0].file == "atlan.yaml"
    assert p030[0].line == 1


def test_p030_silent_when_upload_call_present(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "P030" for f in findings)


def test_p030_silent_on_non_sdr_app(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _NON_SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    assert not _run(tmp_path)


def test_p030_silent_when_no_atlan_yaml(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    assert not _run(tmp_path)


def test_p030_fires_even_when_upload_only_in_tests(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
            "tests/test_connector.py": "# calls self.upload() in test\nresult = 'self.upload()'\n",
        },
    )
    # discover() excludes tests/ — upload call in test files does NOT satisfy P030
    findings = _run(tmp_path)
    assert any(f.rule_id == "P030" for f in findings)


def test_p030_upload_call_in_any_source_file_satisfies(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/base.py": "class Base:\n    pass\n",
            "app/upload_helper.py": "async def do_upload(self):\n    await self.upload('out')\n",
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "P030" for f in findings)


def test_p030_fires_when_source_dir_empty(tmp_path: Path) -> None:
    _write(tmp_path, {"atlan.yaml": _SDR_ATLAN_YAML})
    findings = _run(tmp_path)
    assert any(f.rule_id == "P030" for f in findings)


# ── P030: publish-stage opt-out exemption ───────────────────────────────────


def test_p030_silent_when_manifest_has_no_publish_stage(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_NO_PUBLISH,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    # contract/app.pkl's `pipeline.publish = null` compiles to a manifest
    # with no dag.publish node — nowhere for self.upload() to hand off to.
    findings = _run(tmp_path)
    assert not any(f.rule_id == "P030" for f in findings)


def test_p030_fires_when_manifest_has_publish_stage(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_WITH_PUBLISH,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    findings = _run(tmp_path)
    assert any(f.rule_id == "P030" for f in findings)


def test_p030_fires_when_no_manifest_at_all(tmp_path: Path) -> None:
    # No manifest means we cannot establish the opt-out — default to firing
    # rather than silently exempting an app we can't actually inspect.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    findings = _run(tmp_path)
    assert any(f.rule_id == "P030" for f in findings)


def test_p030_fires_when_manifest_unparseable(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": "not json {{{",
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    findings = _run(tmp_path)
    assert any(f.rule_id == "P030" for f in findings)


def test_p030_multi_ep_fires_if_any_manifest_has_publish(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/extract/manifest.json": _MANIFEST_NO_PUBLISH,
            "app/generated/profile/manifest.json": _MANIFEST_WITH_PUBLISH,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    findings = _run(tmp_path)
    assert any(f.rule_id == "P030" for f in findings)


def test_p030_multi_ep_silent_when_no_manifest_has_publish(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/extract/manifest.json": _MANIFEST_NO_PUBLISH,
            "app/generated/profile/manifest.json": _MANIFEST_NO_PUBLISH,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "P030" for f in findings)


# ── P037: agent_json ignored by a custom GUID-only credential path ──────────

# GUID-only resolution: custom ref + resolve_credential_raw, no agent-aware entry.
_CREDS_GUID_ONLY = (
    "from application_sdk.credentials.ref import CredentialRef\n"
    "\n"
    "async def _resolve(context, guid):\n"
    "    ref = CredentialRef(name=guid, credential_guid=guid)\n"
    "    return await context.resolve_credential_raw(ref)\n"
)

# Agent-aware via CredentialRef.resolve(input) (postgres/alloydb/bigquery shape).
_CREDS_RESOLVE = (
    "from application_sdk.credentials.ref import CredentialRef\n"
    "\n"
    "def _resolve(input_obj):\n"
    "    return CredentialRef.resolve(input_obj)\n"
)

# Agent-aware via from_workflow_args, even with a GUID fallback (mongodb shape).
_CREDS_FROM_WORKFLOW_ARGS = (
    "from application_sdk.credentials import CredentialRef\n"
    "\n"
    "async def _resolve(context, workflow_args):\n"
    "    ref = CredentialRef.from_workflow_args(workflow_args)\n"
    "    return await context.resolve_credential_raw(ref)\n"
)

# Direct construction WITH an agent_spec kwarg is agent-aware.
_CREDS_AGENT_SPEC = (
    "from application_sdk.credentials.ref import CredentialRef\n"
    "\n"
    "def _make(agent):\n"
    "    return CredentialRef(agent_spec=agent)\n"
)

# No custom credential resolution at all (mysql/mssql — rely on SDK base).
_NO_CREDS = "class Connector:\n    async def run(self):\n        return None\n"

# A docstring mentioning CredentialRef(...) must NOT register as a real call.
_CREDS_ONLY_IN_DOCSTRING = (
    "def helper():\n"
    '    """The SDK wraps a guid as CredentialRef(credential_guid=guid)."""\n'
    "    return None\n"
)


def test_p037_rule_metadata() -> None:
    rule = get_rule("P037")
    assert rule.name == "SdrAgentJsonNotConsumed"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.16.0"
    assert rule.category == "sdr-readiness"


def test_p037_fires_on_guid_only_resolution(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _CREDS_GUID_ONLY},
    )
    p037 = [f for f in _run(tmp_path) if f.rule_id == "P037"]
    assert len(p037) == 1
    assert "credential_guid" in p037[0].message
    assert p037[0].column == 1


def test_p037_silent_when_credential_ref_resolve_used(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _CREDS_RESOLVE},
    )
    assert not any(f.rule_id == "P037" for f in _run(tmp_path))


def test_p037_silent_when_from_workflow_args_used(tmp_path: Path) -> None:
    # mongodb shape: agent-aware factory present alongside a GUID fallback → exempt.
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _CREDS_FROM_WORKFLOW_ARGS},
    )
    assert not any(f.rule_id == "P037" for f in _run(tmp_path))


def test_p037_silent_when_agent_spec_construction(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _CREDS_AGENT_SPEC},
    )
    assert not any(f.rule_id == "P037" for f in _run(tmp_path))


def test_p037_silent_when_no_custom_resolution(tmp_path: Path) -> None:
    # No CredentialRef / resolve_credential_raw at all → not gated in.
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _NO_CREDS},
    )
    assert not any(f.rule_id == "P037" for f in _run(tmp_path))


def test_p037_ignores_docstring_only_mention(tmp_path: Path) -> None:
    # A CredentialRef(...) mention inside a docstring is not a real call.
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _CREDS_ONLY_IN_DOCSTRING},
    )
    assert not any(f.rule_id == "P037" for f in _run(tmp_path))


def test_p037_agent_aware_in_any_file_exempts(tmp_path: Path) -> None:
    # GUID-only in one file, agent-aware resolve in another → app-level exempt.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/guid.py": _CREDS_GUID_ONLY,
            "app/resolve.py": _CREDS_RESOLVE,
        },
    )
    assert not any(f.rule_id == "P037" for f in _run(tmp_path))


def test_p037_silent_on_non_sdr_app(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _NON_SDR_ATLAN_YAML, "app/connector.py": _CREDS_GUID_ONLY},
    )
    assert not _run(tmp_path)


# ── P038: object-store prefix mis-rooted from an empty-defaulting input ──────

# app_name pulled from the input, then interpolated into an artifacts/apps path.
_PATH_MISROOTED_VIA_VAR = (
    "from application_sdk.constants import APPLICATION_NAME\n"
    "\n"
    "def _paths(input_data):\n"
    '    app_name = input_data.get("application_name", APPLICATION_NAME)\n'
    '    workflow_id = input_data.get("workflow_id", "local")\n'
    '    return f"artifacts/apps/{app_name}/workflows/{workflow_id}"\n'
)

# input.application_name interpolated directly into the path f-string.
_PATH_MISROOTED_DIRECT = (
    "def _paths(input):\n"
    '    return f"artifacts/apps/{input.application_name}/workflows/x"\n'
)

# Correct: rooted from the APPLICATION_NAME constant.
_PATH_CORRECT_CONST = (
    "from application_sdk.constants import APPLICATION_NAME\n"
    "\n"
    "def _paths():\n"
    '    return f"artifacts/apps/{APPLICATION_NAME}/workflows/x"\n'
)

# Correct (iceberg-style): persistent-artifacts/apps/{APPLICATION_NAME} — the
# literal contains the "artifacts/apps" substring but interpolates the constant,
# not an input field, so it must NOT fire.
_PATH_CORRECT_PERSISTENT = (
    "from application_sdk.constants import APPLICATION_NAME\n"
    "\n"
    "def _vault(guid):\n"
    '    return f"persistent-artifacts/apps/{APPLICATION_NAME}/credentials/{guid}"\n'
)

# Correct: application_name read from input but used as a DB/connection param,
# not in an artifacts/apps path → no finding.
_PATH_APP_NAME_NOT_IN_PATH = (
    "def _client_params(input_data):\n"
    '    return {"application_name": input_data.get("application_name", "Atlan")}\n'
)


def test_p038_rule_metadata() -> None:
    rule = get_rule("P038")
    assert rule.name == "SdrArtifactMisrooted"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.16.0"
    assert rule.category == "sdr-readiness"


def test_p038_fires_when_prefix_rooted_from_input_var(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _PATH_MISROOTED_VIA_VAR},
    )
    p038 = [f for f in _run(tmp_path) if f.rule_id == "P038"]
    assert len(p038) == 1
    assert "application_name" in p038[0].message
    assert p038[0].column == 1


def test_p038_fires_when_prefix_rooted_from_input_directly(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _PATH_MISROOTED_DIRECT},
    )
    assert any(f.rule_id == "P038" for f in _run(tmp_path))


def test_p038_silent_when_rooted_from_constant(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _PATH_CORRECT_CONST},
    )
    assert not any(f.rule_id == "P038" for f in _run(tmp_path))


def test_p038_silent_on_persistent_artifacts_with_constant(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _PATH_CORRECT_PERSISTENT},
    )
    assert not any(f.rule_id == "P038" for f in _run(tmp_path))


def test_p038_silent_when_app_name_not_in_object_store_path(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "app/connector.py": _PATH_APP_NAME_NOT_IN_PATH},
    )
    assert not any(f.rule_id == "P038" for f in _run(tmp_path))


def test_p038_silent_on_non_sdr_app(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _NON_SDR_ATLAN_YAML,
            "app/connector.py": _PATH_MISROOTED_VIA_VAR,
        },
    )
    assert not _run(tmp_path)


# ── P039: agent_json dropped by a closed generated input contract ───────────

# Generated extract-input contract on the bare Input base, no agent_json field,
# no extra-allow → drops the forwarded agent_json (the sigma failing shape).
_INPUT_BARE_CLOSED = (
    "from application_sdk.contracts.base import Input\n"
    "\n"
    "class AppInputContract(Input):\n"
    "    extraction_method: str = 'direct'\n"
    "    credential_guid: str = ''\n"
)

# Bare Input but opts out of payload safety → agent_json passes through.
_INPUT_BARE_UNBOUNDED = (
    "from application_sdk.contracts.base import Input\n"
    "\n"
    "class AppInputContract(Input, allow_unbounded_fields=True):\n"
    "    extraction_method: str = 'direct'\n"
)

# Bare Input but declares agent_json explicitly → safe.
_INPUT_BARE_WITH_AGENT_JSON = (
    "from typing import Any\n"
    "from application_sdk.contracts.base import Input\n"
    "\n"
    "class AppInputContract(Input):\n"
    "    extraction_method: str = 'direct'\n"
    "    agent_json: dict[str, Any] | None = None\n"
)

# Subclasses the SDK ExtractionInput family (which declares agent_json) → safe.
_INPUT_EXTRACTION_FAMILY = (
    "from application_sdk.templates.contracts import ExtractionInput\n"
    "\n"
    "class AppInputContract(ExtractionInput):\n"
    "    preflight_check: str = ''\n"
)

# Bare Input, closed, but with model_config extra='allow' → safe.
_INPUT_MODEL_CONFIG_ALLOW = (
    "from pydantic import ConfigDict\n"
    "from application_sdk.contracts.base import Input\n"
    "\n"
    "class AppInputContract(Input):\n"
    "    model_config = ConfigDict(extra='allow')\n"
    "    extraction_method: str = 'direct'\n"
)

_MANIFEST_AGENT_TOPLEVEL = _MANIFEST_WITH_AGENT_JSON  # carries {{agent-json}}


def test_p039_rule_metadata() -> None:
    rule = get_rule("P039")
    assert rule.name == "SdrAgentJsonDroppedByInputContract"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.16.0"
    assert rule.category == "sdr-readiness"


def test_p039_fires_on_closed_bare_input_contract(tmp_path: Path) -> None:
    # The dropped-agent_json failing shape: manifest declares {{agent-json}}, but
    # the generated contract is a closed bare-Input subclass with no agent_json.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_AGENT_TOPLEVEL,
            "app/generated/_input.py": _INPUT_BARE_CLOSED,
            "app/connector.py": "class C:\n    async def run(self):\n        await self.upload('o')\n",
        },
    )
    p039 = [f for f in _run(tmp_path) if f.rule_id == "P039"]
    assert len(p039) == 1
    assert "agent_json" in p039[0].message
    assert p039[0].file == "app/generated/_input.py"


def test_p039_silent_when_contract_allows_unbounded(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_AGENT_TOPLEVEL,
            "app/generated/_input.py": _INPUT_BARE_UNBOUNDED,
        },
    )
    assert not any(f.rule_id == "P039" for f in _run(tmp_path))


def test_p039_silent_when_contract_declares_agent_json(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_AGENT_TOPLEVEL,
            "app/generated/_input.py": _INPUT_BARE_WITH_AGENT_JSON,
        },
    )
    assert not any(f.rule_id == "P039" for f in _run(tmp_path))


def test_p039_silent_when_contract_extends_extraction_family(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_AGENT_TOPLEVEL,
            "app/generated/_input.py": _INPUT_EXTRACTION_FAMILY,
        },
    )
    assert not any(f.rule_id == "P039" for f in _run(tmp_path))


def test_p039_silent_when_model_config_allows_extra(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_AGENT_TOPLEVEL,
            "app/generated/_input.py": _INPUT_MODEL_CONFIG_ALLOW,
        },
    )
    assert not any(f.rule_id == "P039" for f in _run(tmp_path))


def test_p039_silent_when_manifest_has_no_agent_routing(tmp_path: Path) -> None:
    # No {{agent-json}} in the manifest → precondition false (P029's domain,
    # not this rule) even though the contract is a closed bare-Input subclass.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_NON_AGENT,
            "app/generated/_input.py": _INPUT_BARE_CLOSED,
        },
    )
    assert not any(f.rule_id == "P039" for f in _run(tmp_path))


def test_p039_fires_per_agent_entrypoint_sibling_input(tmp_path: Path) -> None:
    # Multi-entrypoint: the agent crawler's sibling _input.py is closed → fires;
    # a non-agent miner entrypoint alongside it is not considered.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/crawler/manifest.json": _MANIFEST_AGENT_TOPLEVEL,
            "app/generated/crawler/_input.py": _INPUT_BARE_CLOSED,
            "app/generated/miner/manifest.json": _MANIFEST_NON_AGENT,
            "app/generated/miner/_input.py": _INPUT_BARE_CLOSED,
        },
    )
    p039 = [f for f in _run(tmp_path) if f.rule_id == "P039"]
    assert len(p039) == 1
    assert "crawler" in p039[0].file


def test_p039_silent_on_non_sdr_app(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _NON_SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_AGENT_TOPLEVEL,
            "app/generated/_input.py": _INPUT_BARE_CLOSED,
        },
    )
    assert not _run(tmp_path)


# ── scan_path no-op ──────────────────────────────────────────────────────────


def test_scan_path_is_noop(tmp_path: Path) -> None:
    f = tmp_path / "app.py"
    f.write_text("pass\n")
    assert scan_path(f, tmp_path) == []
