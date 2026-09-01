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
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.9.0"
    assert rule.category == "sdr-readiness"


def test_p030_prose_no_longer_calls_a_working_bridge_a_false_positive() -> None:
    """P030 must not document the shape P042 now reports as a false positive."""
    rule = get_rule("P030")
    assert "documented false positive" not in rule.full_description.lower()
    assert "P042" in rule.full_description


def test_p030_prose_does_not_contradict_its_block_tier() -> None:
    """The generated doc renders tier and prose side by side — keep them agreed.

    The rule shipped as WARN and its prose argued for WARN in as many words; the
    tier flip has to take that paragraph with it or ``gen-rule-docs`` publishes a
    page whose tier column and body disagree.
    """
    rule = get_rule("P030")
    assert rule.tier == EnforcementTier.BLOCK
    assert "this is a warn" not in rule.full_description.lower()


def test_p042_rule_metadata() -> None:
    rule = get_rule("P042")
    assert rule.name == "SdrHandRolledUploadBridge"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.18.0"
    assert rule.category == "sdr-readiness"
    # The rule exists as an interim net for a shim the SDK removes in v4.0, so
    # it carries a retirement path rather than becoming permanent by default.
    assert rule.superseded_by == "sdk>=4.0.0"


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


def test_p030_silent_when_super_upload_call_present(tmp_path: Path) -> None:
    """`await super().upload(...)` is a real transfer path, not an absence.

    An app that overrides ``upload`` to add connector-specific logic and then
    defers to the SDK's ``App.upload()`` has a structurally reachable upload.
    Matching only ``self.upload`` false-positived P030 on exactly that shape —
    the same ``super()`` call T017 (``e2e_agent_spec``) already honours.
    """
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": (
                "from application_sdk.app import App\n\n"
                "class Connector(App):\n"
                "    async def run(self, workflow_args):\n"
                "        await super().upload(workflow_args)\n"
            ),
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
    assert rule.tier == EnforcementTier.BLOCK
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
    assert rule.tier == EnforcementTier.BLOCK
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


# ── P030: upload-bridge shapes (fleet-sweep hardening) ───────────────────────

_NOOP_BRIDGE = (
    "class Connector:\n"
    "    async def upload_to_atlan(self, prefix):\n"
    "        # publish owns the transfer\n"
    "        return None\n"
)

_REAL_BRIDGE = (
    "class Connector:\n"
    "    async def upload_to_atlan(self, prefix):\n"
    "        store = self._object_store\n"
    "        await store.upload_prefix(prefix, tier='retained')\n"
)


def test_p030_flags_noop_upload_bridge_stub(tmp_path: Path) -> None:
    """An upload_to_atlan whose body performs no storage-transfer call is a
    no-op stub — the mongodbatlas-class silent-zero-asset shape — and is
    flagged at the definition, not as a generic absence."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": _NOOP_BRIDGE,
        },
    )
    p030 = [f for f in _run(tmp_path) if f.rule_id == "P030"]
    assert len(p030) == 1
    assert p030[0].file == "app/connector.py"
    assert p030[0].line == 2
    assert "no-op stub" in p030[0].message
    assert "full-DAG e2e" in p030[0].message


def test_p030_real_bridge_is_p042_not_a_p030_absence(tmp_path: Path) -> None:
    """A transferring bridge is P042's shape, not P030's — and never silence.

    Bytes do move, so P030's silent-zero-asset message would be wrong. But the
    app has reimplemented an SDK contract on a symbol deprecated for removal in
    v4.0, so it stays visible under its own rule.
    """
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": _REAL_BRIDGE,
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "P030" for f in findings)
    p042 = [f for f in findings if f.rule_id == "P042"]
    assert len(p042) == 1
    assert p042[0].file == "app/connector.py"
    assert p042[0].line == 2
    assert "self.upload" in p042[0].message
    assert "v4.0.0" in p042[0].message


def test_p042_silent_when_self_upload_is_present(tmp_path: Path) -> None:
    """A bridge alongside a real self.upload() is redundant, not a substitution.

    P042 is about a bridge standing *in place of* the SDK path; with the SDK
    path present there is nothing being stood in for.
    """
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": _REAL_BRIDGE,
            "app/main.py": (
                "class App:\n"
                "    async def run(self):\n"
                "        await self.upload('output')\n"
            ),
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id in ("P030", "P042") for f in findings)


def test_p042_does_not_double_report_a_noop_stub(tmp_path: Path) -> None:
    """A no-op stub carries exactly one finding — the sharper P030 one.

    It is not a working bridge, so P042 must not add a second finding on the
    same method, and the app-level absence finding must not fire either.
    """
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": _NOOP_BRIDGE,
        },
    )
    findings = [f for f in _run(tmp_path) if f.rule_id in ("P030", "P042")]
    assert [f.rule_id for f in findings] == ["P030"]
    assert "no-op stub" in findings[0].message


def test_p042_reports_a_working_bridge_beside_a_noop_stub(tmp_path: Path) -> None:
    """Mixed shapes are graded per method, each under the rule that fits it."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/stub.py": _NOOP_BRIDGE,
            "app/connector.py": _REAL_BRIDGE,
        },
    )
    findings = [f for f in _run(tmp_path) if f.rule_id in ("P030", "P042")]
    assert {(f.rule_id, f.file) for f in findings} == {
        ("P030", "app/stub.py"),
        ("P042", "app/connector.py"),
    }


def test_p042_silent_on_non_sdr_app(tmp_path: Path) -> None:
    """Gated on self_deployed_runtime, like the rest of the P-series SDR rules."""
    _write(
        tmp_path,
        {
            "atlan.yaml": "self_deployed_runtime: false\n",
            "app/connector.py": _REAL_BRIDGE,
        },
    )
    assert not any(f.rule_id == "P042" for f in _run(tmp_path))


def test_p042_silent_when_app_has_no_publish_stage(tmp_path: Path) -> None:
    """Shares P030's exemption: an extract-only app hands assets off to nothing."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_NO_PUBLISH,
            "app/connector.py": _REAL_BRIDGE,
        },
    )
    assert not any(f.rule_id == "P042" for f in _run(tmp_path))


def test_p030_absence_message_no_longer_offers_a_bridge_as_a_fix(
    tmp_path: Path,
) -> None:
    """The absence remediation points at self.upload() only.

    Naming a hand-rolled bridge as an alternative fix would prescribe the very
    shape P042 now reports.
    """
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": "class App:\n    pass\n",
        },
    )
    p030 = [f for f in _run(tmp_path) if f.rule_id == "P030"]
    assert len(p030) == 1
    assert "upload_to_atlan bridge into" not in p030[0].message
    assert "await self.upload(...)" in p030[0].message


def test_p030_noop_stub_flagged_even_with_self_upload_elsewhere(
    tmp_path: Path,
) -> None:
    """A dead no-op stub alongside a real self.upload() still fires — it masks
    the real transfer path."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": _NOOP_BRIDGE,
            "app/main.py": (
                "class App:\n"
                "    async def run(self):\n"
                "        await self.upload('output')\n"
            ),
        },
    )
    p030 = [f for f in _run(tmp_path) if f.rule_id == "P030"]
    assert len(p030) == 1
    assert "no-op stub" in p030[0].message


def test_p030_absence_message_covers_sqlapp_run_delegation(tmp_path: Path) -> None:
    """The absence finding explains that delegating to SqlApp.run() does not
    satisfy the rule (deployment store only) and demands e2e evidence before a
    false-positive call — the clickhouse-class shape."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/main.py": (
                "from application_sdk.templates import SqlApp\n"
                "app = SqlApp(name='c')\n"
                "app.run()\n"
            ),
        },
    )
    p030 = [f for f in _run(tmp_path) if f.rule_id == "P030"]
    assert len(p030) == 1
    assert "SqlApp.run()" in p030[0].message
    assert "full-DAG e2e" in p030[0].message


def test_p030_absence_not_cleared_by_a_comment_mentioning_self_upload(
    tmp_path: Path,
) -> None:
    """A textual mention is indistinguishable from a call under a substring test.

    The population this rule targets is precisely where such a comment is
    likely — fleet remediation found a stub whose comment claimed the publish
    stage owned the transfer.
    """
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": (
                "class Connector:\n"
                "    async def run(self):\n"
                "        # publish owns the transfer, we do not call self.upload(...)\n"
                "        pass\n"
            ),
        },
    )
    assert any(f.rule_id == "P030" for f in _run(tmp_path))


def test_p030_absence_not_cleared_by_a_docstring_mentioning_self_upload(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": (
                "class Connector:\n"
                "    async def run(self):\n"
                '        """TODO: wire this to self.upload(prefix)."""\n'
                "        pass\n"
            ),
        },
    )
    assert any(f.rule_id == "P030" for f in _run(tmp_path))


def test_p030_abstract_bridge_declaration_is_not_a_noop_stub(tmp_path: Path) -> None:
    """`raise NotImplementedError` declares a contract for a subclass.

    Grading it as a stub put a permanent spurious finding on every connector
    using the declare-abstract/implement-in-subclass idiom.
    """
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": (
                "class BaseConnector:\n"
                "    async def upload_to_atlan(self, prefix):\n"
                '        """Subclasses implement the transfer."""\n'
                "        raise NotImplementedError\n"
                "\n\n"
                "class RealConnector(BaseConnector):\n"
                "    async def upload_to_atlan(self, prefix):\n"
                "        await self._store.upload_prefix(prefix)\n"
            ),
        },
    )
    assert not any(f.rule_id == "P030" for f in _run(tmp_path))


def test_p030_bridge_delegating_to_a_helper_is_not_a_noop_stub(
    tmp_path: Path,
) -> None:
    """Correctness must not rest on whether the helper's *name* contains a verb."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": (
                "class Connector:\n"
                "    async def upload_to_atlan(self, prefix):\n"
                "        await self._relay_to_tenant_bucket(prefix)\n"
                "\n"
                "    async def _relay_to_tenant_bucket(self, prefix):\n"
                "        await self._object_store.upload_prefix(prefix)\n"
            ),
        },
    )
    assert not any(f.rule_id == "P030" for f in _run(tmp_path))


def test_p030_ordinary_method_names_do_not_count_as_transfers(
    tmp_path: Path,
) -> None:
    """`put` is a substring of compute/output/inputs — a stub containing any of
    them evaded detection entirely under bare-substring matching."""
    for body in (
        "self._metrics.put('upload_attempted', 1)",
        "buffer.copy()",
        "requests.put(url)",
        "self.compute_summary()",
        "self.output_stats()",
        "self.get_inputs()",
    ):
        root = tmp_path / body[:12].replace(".", "_").replace("(", "_")
        _write(
            root,
            {
                "atlan.yaml": _SDR_ATLAN_YAML,
                "app/connector.py": (
                    "class Connector:\n"
                    "    async def upload_to_atlan(self, prefix):\n"
                    f"        {body}\n"
                ),
            },
        )
        p030 = [f for f in _run(root) if f.rule_id == "P030"]
        assert p030, f"{body} should NOT clear the no-op-stub finding"
        assert "no-op stub" in p030[0].message


def test_p030_store_receiver_verbs_count_as_transfers(tmp_path: Path) -> None:
    """`self._object_store.sync(...)` / `self._store.push(...)` are real bridges —
    misreporting them tells an author their working upload moves no bytes."""
    for body in ("self._object_store.sync(prefix)", "self._store.push(prefix)"):
        root = tmp_path / body[5:16].replace(".", "_").replace("(", "_")
        _write(
            root,
            {
                "atlan.yaml": _SDR_ATLAN_YAML,
                "app/connector.py": (
                    "class Connector:\n"
                    "    async def upload_to_atlan(self, prefix):\n"
                    f"        await {body}\n"
                ),
            },
        )
        assert not any(f.rule_id == "P030" for f in _run(root)), body


def test_p030_real_fleet_bridge_shapes_are_not_flagged(tmp_path: Path) -> None:
    """The call names green connectors actually use (postgres/snowflake/glue)."""
    for body in (
        "await storage_upload_file(key, tmp, store=upstream_store)",
        "await upload_file(dest_key, tmp_path, store=upstream_store)",
        "await AtlanStorage.migrate_from_objectstore_to_atlan(prefix=p)",
    ):
        root = tmp_path / str(abs(hash(body)))
        _write(
            root,
            {
                "atlan.yaml": _SDR_ATLAN_YAML,
                "app/connector.py": (
                    "class Connector:\n"
                    "    async def upload_to_atlan(self, prefix):\n"
                    f"        {body}\n"
                ),
            },
        )
        assert not any(f.rule_id == "P030" for f in _run(root)), body


def test_sdr_scan_survives_non_utf8_files(tmp_path: Path) -> None:
    """_is_sdr_app gates the SDR rules — one bad byte must not take them out."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    (tmp_path / "app" / "bad.py").write_bytes(b"\xff\xfe class X: pass\n")
    (tmp_path / "app" / "generated").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "generated" / "manifest.json").write_bytes(b"\xff\xfe{}")
    assert any(f.rule_id == "P030" for f in _run(tmp_path))


def test_p030_compound_non_storage_names_do_not_clear_the_stub(
    tmp_path: Path,
) -> None:
    """A compound name carrying a verb token is not evidence of a transfer.

    The receiver check that makes the bare-verb path sound must apply at every
    token count, or these six clear the finding while moving no bytes.
    """
    for i, body in enumerate(
        (
            "self._metrics.put_metric_data(x)",
            "self.migrate_schema()",
            "self._cache.copy_on_write()",
            "self._clipboard.copy_paste()",
            "self._client.transfer_encoding()",
            "self._queue.push_job(x)",
        )
    ):
        root = tmp_path / f"case{i}"
        _write(
            root,
            {
                "atlan.yaml": _SDR_ATLAN_YAML,
                "app/connector.py": (
                    "class Connector:\n"
                    "    async def upload_to_atlan(self, prefix):\n"
                    f"        {body}\n"
                ),
            },
        )
        p030 = [f for f in _run(root) if f.rule_id == "P030"]
        assert p030, f"{body} should NOT clear the no-op-stub finding"
        assert "no-op stub" in p030[0].message


def test_p030_write_on_a_store_receiver_is_a_transfer(tmp_path: Path) -> None:
    """A write()-only real bridge must not be reported as a no-op stub."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": (
                "class Connector:\n"
                "    async def upload_to_atlan(self, prefix):\n"
                "        await self._object_store.write(prefix, data)\n"
            ),
        },
    )
    assert not any(f.rule_id == "P030" for f in _run(tmp_path))


def test_p030_two_hop_same_class_delegation_is_not_a_stub(tmp_path: Path) -> None:
    """Delegation resolves transitively within the class, not just one hop."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": (
                "class Connector:\n"
                "    async def upload_to_atlan(self, prefix):\n"
                "        await self._helper1(prefix)\n"
                "\n"
                "    async def _helper1(self, prefix):\n"
                "        await self._helper2(prefix)\n"
                "\n"
                "    async def _helper2(self, prefix):\n"
                "        await self._object_store.upload_prefix(prefix)\n"
            ),
        },
    )
    assert not any(f.rule_id == "P030" for f in _run(tmp_path))


def test_p030_compound_upload_names_still_need_a_store(tmp_path: Path) -> None:
    """`upload` as one token of a compound name proves nothing on its own.

    Only the bare `upload(...)` / `self.upload(...)` clears without a store.
    """
    for i, body in enumerate(
        ("self._log.upload_metrics(x=1)", "self._telemetry.upload_stats()")
    ):
        root = tmp_path / f"c{i}"
        _write(
            root,
            {
                "atlan.yaml": _SDR_ATLAN_YAML,
                "app/connector.py": (
                    "class Connector:\n"
                    "    async def upload_to_atlan(self, prefix):\n"
                    f"        {body}\n"
                ),
            },
        )
        assert [f.rule_id for f in _run(root) if f.rule_id == "P030"] == ["P030"], body


def test_p030_registry_named_store_is_not_a_store_receiver(tmp_path: Path) -> None:
    """`_store_of_names` merely contains the word — substring matching accepted it."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": (
                "class Connector:\n"
                "    async def upload_to_atlan(self, prefix):\n"
                "        self._store_of_names.put('x')\n"
            ),
        },
    )
    assert any(f.rule_id == "P030" for f in _run(tmp_path))


def test_p030_bare_sdk_storage_helpers_are_transfers(tmp_path: Path) -> None:
    """The green fleet bridges call SDK helpers as bare functions, no receiver."""
    for i, body in enumerate(
        (
            "await storage_upload_file(key, tmp, store=upstream_store)",
            "await upload_file(dest_key, tmp_path, store=upstream_store)",
        )
    ):
        root = tmp_path / f"b{i}"
        _write(
            root,
            {
                "atlan.yaml": _SDR_ATLAN_YAML,
                "app/connector.py": (
                    "class Connector:\n"
                    "    async def upload_to_atlan(self, prefix):\n"
                    f"        {body}\n"
                ),
            },
        )
        assert not any(f.rule_id == "P030" for f in _run(root)), body


def test_p030_positional_store_argument_is_a_transfer(tmp_path: Path) -> None:
    """The SDK's own callers pass the store positionally."""
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": (
                "class Connector:\n"
                "    async def upload_to_atlan(self, prefix):\n"
                "        await transfer_directory(src, dst, upstream_store)\n"
            ),
        },
    )
    assert not any(f.rule_id == "P030" for f in _run(tmp_path))


# ── P051: SDR app below the interactive-setup SDK floor ─────────────────────


def _uv_lock(sdk_version: str | None) -> str:
    """A minimal uv.lock. ``None`` omits the atlan-application-sdk entry."""
    other = (
        "[[package]]\n"
        'name = "some-dep"\n'
        'version = "1.2.3"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    if sdk_version is None:
        return other
    sdk = (
        "[[package]]\n"
        'name = "atlan-application-sdk"\n'
        f'version = "{sdk_version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    return f"{other}\n{sdk}"


def test_p051_rule_metadata() -> None:
    rule = get_rule("P051")
    assert rule.name == "SdrPreflightUnavailable"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.24.0"
    assert rule.category == "sdr-readiness"


def test_p051_fires_when_locked_sdk_below_floor(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "uv.lock": _uv_lock("3.29.0"),
        },
    )
    findings = [f for f in _run(tmp_path) if f.rule_id == "P051"]
    assert len(findings) == 1
    assert findings[0].file == "uv.lock"
    assert "3.30.0" in findings[0].message


def test_p051_fires_on_far_below_floor(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "uv.lock": _uv_lock("3.25.0")},
    )
    assert any(f.rule_id == "P051" for f in _run(tmp_path))


def test_p051_silent_at_exactly_the_floor(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "uv.lock": _uv_lock("3.30.0")},
    )
    assert not any(f.rule_id == "P051" for f in _run(tmp_path))


def test_p051_silent_above_the_floor(tmp_path: Path) -> None:
    for version in ("3.30.1", "3.31.0", "4.0.0"):
        _write(
            tmp_path,
            {"atlan.yaml": _SDR_ATLAN_YAML, "uv.lock": _uv_lock(version)},
        )
        assert not any(
            f.rule_id == "P051" for f in _run(tmp_path)
        ), f"should be silent at {version}"


def test_p051_silent_on_non_sdr_app(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _NON_SDR_ATLAN_YAML, "uv.lock": _uv_lock("3.25.0")},
    )
    assert not any(f.rule_id == "P051" for f in _run(tmp_path))


def test_p051_silent_when_no_lock(tmp_path: Path) -> None:
    # Can't confirm below the floor without a lock — stay silent (D-series owns
    # a missing/unbounded SDK declaration).
    _write(tmp_path, {"atlan.yaml": _SDR_ATLAN_YAML})
    assert not any(f.rule_id == "P051" for f in _run(tmp_path))


def test_p051_silent_when_sdk_absent_from_lock(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "uv.lock": _uv_lock(None)},
    )
    assert not any(f.rule_id == "P051" for f in _run(tmp_path))


def test_p051_silent_on_unparseable_lock(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {"atlan.yaml": _SDR_ATLAN_YAML, "uv.lock": "this is : not valid = toml ["},
    )
    assert not any(f.rule_id == "P051" for f in _run(tmp_path))
