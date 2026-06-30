"""Meta-tests for the P-series SDR-readiness checks (P029–P030, DISTR-752).

P029 catches the MSSQL regression pattern: an SDR app whose manifest.json is
missing agent_json in dag.extract.inputs.args.  The SDR worker starts, the
workflow status is "success", but no credentials are routed so no assets move.

P030 catches apps that never call self.upload(): the ENABLE_ATLAN_UPLOAD gate
is structurally unreachable, so assets never land in the Atlan tenant bucket
even when the flag is true.

Both rules gate on self_deployed_runtime: true in atlan.yaml — non-SDR apps
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


def test_p026_rule_metadata() -> None:
    rule = get_rule("P029")
    assert rule.name == "SdrManifestMissingAgentJson"
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.9.0"
    assert rule.category == "sdr-readiness"


def test_p027_rule_metadata() -> None:
    rule = get_rule("P030")
    assert rule.name == "SdrUploadNotCalled"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.9.0"
    assert rule.category == "sdr-readiness"


# ── P029: manifest missing agent_json ───────────────────────────────────────


def test_p026_fires_on_manifest_without_agent_json(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_WITHOUT_AGENT_JSON,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = _run(tmp_path)
    p026 = [f for f in findings if f.rule_id == "P029"]
    assert len(p026) == 1
    assert "agent_json" in p026[0].message
    assert p026[0].line == 1
    assert p026[0].column == 1


def test_p026_silent_when_manifest_has_agent_json(tmp_path: Path) -> None:
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


def test_p026_silent_when_no_manifest(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "P029" for f in findings)


def test_p026_silent_on_non_sdr_app(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _NON_SDR_ATLAN_YAML,
            "app/generated/manifest.json": _MANIFEST_WITHOUT_AGENT_JSON,
        },
    )
    assert not _run(tmp_path)


def test_p026_silent_when_no_atlan_yaml(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app/generated/manifest.json": _MANIFEST_WITHOUT_AGENT_JSON,
        },
    )
    assert not _run(tmp_path)


def test_p026_fires_on_missing_args_key(tmp_path: Path) -> None:
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


def test_p026_fires_on_missing_inputs_key(tmp_path: Path) -> None:
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


def test_p026_fires_per_manifest_in_multi_ep(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/extract/manifest.json": _MANIFEST_WITHOUT_AGENT_JSON,
            "app/generated/profile/manifest.json": _MANIFEST_WITHOUT_AGENT_JSON,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = [f for f in _run(tmp_path) if f.rule_id == "P029"]
    assert len(findings) == 2


def test_p026_silent_on_valid_manifest_in_multi_ep(tmp_path: Path) -> None:
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


def test_p026_mixed_ep_one_fire_one_silent(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/generated/extract/manifest.json": _MANIFEST_WITHOUT_AGENT_JSON,
            "app/generated/profile/manifest.json": _MANIFEST_WITH_AGENT_JSON,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = [f for f in _run(tmp_path) if f.rule_id == "P029"]
    assert len(findings) == 1
    assert "extract" in findings[0].file


def test_p026_skips_invalid_json_manifest(tmp_path: Path) -> None:
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


def test_p027_fires_when_no_upload_call(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    findings = _run(tmp_path)
    p027 = [f for f in findings if f.rule_id == "P030"]
    assert len(p027) == 1
    assert "self.upload" in p027[0].message
    assert p027[0].file == "atlan.yaml"
    assert p027[0].line == 1


def test_p027_silent_when_upload_call_present(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        await self.upload('output')\n",
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "P030" for f in findings)


def test_p027_silent_on_non_sdr_app(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _NON_SDR_ATLAN_YAML,
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    assert not _run(tmp_path)


def test_p027_silent_when_no_atlan_yaml(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app/connector.py": "class Connector:\n    async def run(self):\n        pass\n",
        },
    )
    assert not _run(tmp_path)


def test_p027_fires_even_when_upload_only_in_tests(tmp_path: Path) -> None:
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


def test_p027_upload_call_in_any_source_file_satisfies(tmp_path: Path) -> None:
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


def test_p027_fires_when_source_dir_empty(tmp_path: Path) -> None:
    _write(tmp_path, {"atlan.yaml": _SDR_ATLAN_YAML})
    findings = _run(tmp_path)
    assert any(f.rule_id == "P030" for f in findings)


# ── scan_path no-op ──────────────────────────────────────────────────────────


def test_scan_path_is_noop(tmp_path: Path) -> None:
    f = tmp_path / "app.py"
    f.write_text("pass\n")
    assert scan_path(f, tmp_path) == []
