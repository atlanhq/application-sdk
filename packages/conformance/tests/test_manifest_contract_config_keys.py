"""Tests for K018 ManifestArgNotDeclaredOnInputContract and K019
FormKeyMissingFromManifestArgs.

Both rules guard the *inbound* config path — the direction that loses customer
configuration — where K006 guards the outbound one.

The two false-positive traps each rule must survive are pinned explicitly,
because getting either wrong makes the rule noisy enough that it never
graduates past WARN:

* K018 must accept a ``@model_validator(mode="before")`` in place of declared
  fields (the prescribed remedy folds flat keys into ``metadata`` without
  declaring them), and must handle the ``args.metadata.<key>`` envelope as well
  as flat args — the fleet runs both shapes simultaneously.
* K019 must not flag a form key whose placeholder appears on a *downstream*
  node, and must not flag SDK-injected placeholders that have no form key.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.manifest_contract import scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_py(tmp_path: Path, py_files: dict[str, str]) -> list[Path]:
    paths: list[Path] = []
    for name, src in py_files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        paths.append(p)
    return paths


def _write_manifest(path: Path, dag: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dag": dag}), encoding="utf-8")


def _extract_node(args: dict) -> dict:
    return {
        "activity_name": "execute_workflow",
        "app_name": "myapp",
        "inputs": {
            "workflow_type": "MyWorkflow",
            "app_name": "myapp",
            "task_queue": "q",
            "args": args,
        },
    }


def _only(findings: list, rule_id: str) -> list:
    return [f for f in findings if f.rule_id == rule_id]


def _unsuppressed(findings: list, rule_id: str) -> list:
    return [f for f in findings if f.rule_id == rule_id and not f.suppressed]


def _app_src(input_body: str, *, bases: str = "", decorators: str = "") -> str:
    """A single-entrypoint app whose Input contract is under test."""
    return (
        "from application_sdk.app import App, entrypoint\n"
        "from application_sdk.templates.contracts.sql_metadata import ExtractionInput\n"
        "from pydantic import model_validator\n"
        "\n"
        f"class ExtractInput{bases}:\n"
        f"{decorators}"
        f"{input_body}"
        "\n"
        "class ExtractOutput:\n"
        "    status: str\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def extract(self, input: ExtractInput) -> ExtractOutput:\n"
        "        pass\n"
    )


# The flat filter args contract-toolkit 0.9.0 moved to top level.
_FLAT_FILTER_ARGS = {
    "connection": "{{connection}}",
    "include_filter": "{{include-filter}}",
    "exclude_filter": "{{exclude-filter}}",
}


# ---------------------------------------------------------------------------
# K018 — rule metadata
# ---------------------------------------------------------------------------


def test_k018_rule_metadata() -> None:
    rule = get_rule("K018")
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"
    assert rule.rationale


# ---------------------------------------------------------------------------
# K018 — the motivating incident (CONNECT-1318)
# ---------------------------------------------------------------------------


def test_k018_fires_when_flat_arg_not_declared(tmp_path: Path) -> None:
    """The exact CONNECT-1318 shape: flat filter args, contract declares neither."""
    paths = _write_py(tmp_path, {"app.py": _app_src("    connection: str = ''\n")})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_FLAT_FILTER_ARGS)},
    )
    messages = {f.message for f in _only(scan_all(paths, tmp_path), "K018")}
    assert any("include_filter" in m for m in messages)
    assert any("exclude_filter" in m for m in messages)
    assert not any(
        "'connection'" in m for m in messages
    ), "connection is declared — must not be flagged"


def test_k018_silent_when_every_arg_declared(tmp_path: Path) -> None:
    src = _app_src(
        "    connection: str = ''\n"
        "    include_filter: str = ''\n"
        "    exclude_filter: str = ''\n"
    )
    paths = _write_py(tmp_path, {"app.py": src})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_FLAT_FILTER_ARGS)},
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


def test_k018_silent_when_fields_come_from_sdk_extraction_input(
    tmp_path: Path,
) -> None:
    """Inheriting ExtractionInput supplies the filter fields via the SDK registry.

    Without ``ExtractionInput`` in ``SDK_CONTRACT_BASE_FIELDS`` this case
    false-positives on every SQL connector in the fleet.
    """
    paths = _write_py(
        tmp_path, {"app.py": _app_src("    pass\n", bases="(ExtractionInput)")}
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_FLAT_FILTER_ARGS)},
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


# ---------------------------------------------------------------------------
# K018 — false-positive trap #1: the prescribed remedy declares nothing
# ---------------------------------------------------------------------------


def test_k018_silent_when_before_validator_folds_flat_keys(tmp_path: Path) -> None:
    """A mode='before' validator can fold any undeclared key — accept it.

    This is the prescribed fix for the flattening break, so flagging it would
    penalise exactly the apps that remediated correctly.
    """
    src = _app_src(
        "    metadata: dict = {}\n"
        "\n"
        "    @model_validator(mode='before')\n"
        "    @classmethod\n"
        "    def _fold_flat_config_into_metadata(cls, data):\n"
        "        return data\n"
    )
    paths = _write_py(tmp_path, {"app.py": src})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_FLAT_FILTER_ARGS)},
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


def test_k018_still_fires_for_after_mode_validator(tmp_path: Path) -> None:
    """An 'after' validator runs post-coercion — the keys are already gone."""
    src = _app_src(
        "    metadata: dict = {}\n"
        "\n"
        "    @model_validator(mode='after')\n"
        "    def _check(self):\n"
        "        return self\n"
    )
    paths = _write_py(tmp_path, {"app.py": src})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_FLAT_FILTER_ARGS)},
    )
    assert _unsuppressed(scan_all(paths, tmp_path), "K018")


def test_k018_allow_unbounded_fields_does_not_satisfy(tmp_path: Path) -> None:
    """``allow_unbounded_fields=True`` suppresses the error, not the key drop.

    It does not set Pydantic ``extra='allow'``, so undeclared keys are still
    dropped before ``model_dump()``. Treating it as protection is the original
    misreading behind CONNECT-1318.

    Uses the real declaration from the incident app — inheriting
    ``ExtractionInput`` (so the filter fields *are* covered) while an app-specific
    arg is not. Without the base the test would pass for the wrong reason.
    """
    paths = _write_py(
        tmp_path,
        {
            "app.py": _app_src(
                "    pass\n", bases="(ExtractionInput, allow_unbounded_fields=True)"
            )
        },
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {
            "extract": _extract_node(
                {**_FLAT_FILTER_ARGS, "fetch_partitions": "{{fetch-partitions}}"}
            )
        },
    )
    findings = _unsuppressed(scan_all(paths, tmp_path), "K018")
    messages = {f.message for f in findings}
    assert any("fetch_partitions" in m for m in messages)
    assert not any(
        "include_filter" in m for m in messages
    ), "include_filter comes from ExtractionInput — must not be flagged"


# ---------------------------------------------------------------------------
# K018 — false-positive traps found by the fleet sweep (34 apps, 2026-08-31)
# ---------------------------------------------------------------------------


def test_k018_silent_when_contract_sets_pydantic_extra_allow(tmp_path: Path) -> None:
    """Real ``extra="allow"`` keeps undeclared keys — unlike allow_unbounded_fields.

    Conflating the two produced 54 false positives on a single app before this
    condition existed.
    """
    src = _app_src(
        "    model_config = ConfigDict(extra='allow')\n",
        bases="(ExtractionInput)",
    ).replace(
        "from pydantic import model_validator",
        "from pydantic import ConfigDict, model_validator",
    )
    paths = _write_py(tmp_path, {"app.py": src})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({"anything_at_all": "{{anything-at-all}}"})},
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


def test_k018_silent_for_extra_allow_class_keyword(tmp_path: Path) -> None:
    paths = _write_py(
        tmp_path,
        {"app.py": _app_src("    pass\n", bases="(ExtractionInput, extra='allow')")},
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({"anything_at_all": "{{anything-at-all}}"})},
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


def test_k018_counts_fields_inherited_through_an_sdk_base(tmp_path: Path) -> None:
    """``app_name`` reaches the contract via ExtractionInput -> Input.

    The registry stores each SDK base's *own* fields, so without the parent map
    this reports ``app_name`` on every SQL connector in the fleet.
    """
    paths = _write_py(
        tmp_path, {"app.py": _app_src("    pass\n", bases="(ExtractionInput)")}
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({"app_name": "myapp", "workflow_id": "{{wf}}"})},
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


def test_k018_ignores_platform_injected_credential_arg(tmp_path: Path) -> None:
    """``credential`` is resolved by the SDK ingress path, not by the contract.

    Every fleet manifest carries it and no contract declares it, so reporting it
    would put an unactionable finding on every app.
    """
    paths = _write_py(
        tmp_path, {"app.py": _app_src("    pass\n", bases="(ExtractionInput)")}
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({"credential": "{{credential}}"})},
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


# ---------------------------------------------------------------------------
# K018 — false-positive trap: the fleet runs both arg shapes
# ---------------------------------------------------------------------------


def test_k018_fires_for_nested_metadata_envelope(tmp_path: Path) -> None:
    """Pre-0.9.0 apps still nest args under ``metadata`` — check that depth too."""
    paths = _write_py(tmp_path, {"app.py": _app_src("    connection: str = ''\n")})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {
            "extract": _extract_node(
                {
                    "connection": "{{connection}}",
                    "metadata": {"include_filter": "{{include-filter}}"},
                }
            )
        },
    )
    findings = _only(scan_all(paths, tmp_path), "K018")
    assert findings
    assert "args.metadata.include_filter" in findings[0].message


def test_k018_ignores_cross_node_wiring(tmp_path: Path) -> None:
    """``$.<node>.outputs.<field>`` slots are platform-filled, not caller input."""
    paths = _write_py(tmp_path, {"app.py": _app_src("    connection: str = ''\n")})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {
            "extract": _extract_node(
                {
                    "connection": "{{connection}}",
                    "upstream_prefix": "$.other.outputs.some_prefix",
                }
            )
        },
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


def test_k018_silent_when_ancestor_unresolvable(tmp_path: Path) -> None:
    """An unknown base means an incomplete picture — stay silent, don't guess."""
    paths = _write_py(
        tmp_path,
        {"app.py": _app_src("    pass\n", bases="(SomeUnknownThirdPartyBase)")},
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_FLAT_FILTER_ARGS)},
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


def test_k018_suppression(tmp_path: Path) -> None:
    src = _app_src(
        "    connection: str = ''\n",
        decorators="",
    ).replace(
        "class ExtractInput:",
        "# conformance: ignore[K018] filters intentionally deferred\nclass ExtractInput:",
    )
    paths = _write_py(tmp_path, {"app.py": src})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_FLAT_FILTER_ARGS)},
    )
    findings = _only(scan_all(paths, tmp_path), "K018")
    assert findings, "the finding should still be emitted, just suppressed"
    assert all(f.suppressed for f in findings)
    assert _unsuppressed(scan_all(paths, tmp_path), "K018") == []


# ---------------------------------------------------------------------------
# K018 — the real fleet shape: entrypoint inherited from an SDK template
# ---------------------------------------------------------------------------

# Most SQL connectors write no @entrypoint of their own: the app class extends
# BaseMetadataExtractor and inherits it. Without the ExtractionInput fallback
# K018 is silent on this entire family — including the incident app.
_INHERITED_ENTRYPOINT_APP = """\
from application_sdk.templates import BaseMetadataExtractor
from application_sdk.templates.contracts import ExtractionInput
from application_sdk.contracts.base import Input, Output
from pydantic import model_validator

class MyExtractionInput(ExtractionInput):
{body}

class TaskInput(Input):
    catalog: str = ""

class MyApp(BaseMetadataExtractor):
    async def extract(self, input: MyExtractionInput) -> Output:
        pass
"""

# Real athena args that ExtractionInput does not supply.
_APP_SPECIFIC_ARGS = {
    "include_filter": "{{include-filter}}",
    "fetch_partitions": "{{fetch-partitions}}",
    "advanced_config": "{{advanced-config}}",
}


def test_k018_fires_without_entrypoint_decorator(tmp_path: Path) -> None:
    """The BaseMetadataExtractor family must be covered, not skipped."""
    src = _INHERITED_ENTRYPOINT_APP.format(body="    pass\n")
    paths = _write_py(tmp_path, {"app.py": src})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_APP_SPECIFIC_ARGS)},
    )
    messages = {f.message for f in _unsuppressed(scan_all(paths, tmp_path), "K018")}
    assert any("fetch_partitions" in m for m in messages)
    assert any("advanced_config" in m for m in messages)
    assert not any(
        "include_filter" in m for m in messages
    ), "include_filter comes from ExtractionInput — must not be flagged"


def test_k018_inherited_entrypoint_silent_with_before_validator(
    tmp_path: Path,
) -> None:
    """Athena's actual post-remediation shape — a true negative, not a skip."""
    src = _INHERITED_ENTRYPOINT_APP.format(
        body=(
            "    @model_validator(mode='before')\n"
            "    @classmethod\n"
            "    def _fold_flat_config_into_metadata(cls, data):\n"
            "        return data\n"
        )
    )
    paths = _write_py(tmp_path, {"app.py": src})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_APP_SPECIFIC_ARGS)},
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


def test_k018_dead_generated_contract_does_not_block_resolution(
    tmp_path: Path,
) -> None:
    """Two ExtractionInput descendants, one unreferenced — pick the live one.

    Apps commonly carry a toolkit-generated ``AppInputContract`` beside a
    hand-written binding. On the incident app the generated one has zero
    references; treating that as ambiguity would silence the rule.
    """
    generated = (
        "from application_sdk.templates.contracts import ExtractionInput\n"
        "\n"
        "class AppInputContract(ExtractionInput):\n"
        "    pass\n"
    )
    paths = _write_py(
        tmp_path,
        {
            "app.py": _INHERITED_ENTRYPOINT_APP.format(body="    pass\n"),
            "app/contracts/_input.py": generated,
        },
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_APP_SPECIFIC_ARGS)},
    )
    assert _unsuppressed(
        scan_all(paths, tmp_path), "K018"
    ), "the unreferenced generated contract must not create false ambiguity"


def test_k018_silent_when_two_live_contracts_are_ambiguous(tmp_path: Path) -> None:
    """Two *referenced* descendants give no way to tell which one binds."""
    second = (
        "from application_sdk.templates.contracts import ExtractionInput\n"
        "\n"
        "class OtherInput(ExtractionInput):\n"
        "    pass\n"
        "\n"
        "USED: OtherInput = OtherInput()\n"
    )
    paths = _write_py(
        tmp_path,
        {
            "app.py": _INHERITED_ENTRYPOINT_APP.format(body="    pass\n"),
            "other.py": second,
        },
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(_APP_SPECIFIC_ARGS)},
    )
    assert _only(scan_all(paths, tmp_path), "K018") == []


# ---------------------------------------------------------------------------
# K019 — rule metadata
# ---------------------------------------------------------------------------


def test_k019_rule_metadata() -> None:
    rule = get_rule("K019")
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"
    assert rule.rationale


# ---------------------------------------------------------------------------
# K019 helpers
# ---------------------------------------------------------------------------


_MINIMAL_APP = (
    "from application_sdk.app import App, entrypoint\n"
    "\n"
    "class ExtractInput:\n"
    "    pass\n"
    "\n"
    "class ExtractOutput:\n"
    "    status: str\n"
    "\n"
    "class MyApp(App):\n"
    "    @entrypoint\n"
    "    async def extract(self, input: ExtractInput) -> ExtractOutput:\n"
    "        pass\n"
)


def _write_pkl(tmp_path: Path, widgets: str, *, prefix: str = "") -> None:
    """Write a contract/app.pkl whose uiConfig declares *widgets*."""
    pkl = (
        'amends "@app-contract-toolkit/App.pkl"\n'
        "\n"
        'name = "myapp"\n'
        "\n"
        "uiConfig {\n"
        "  tasks {\n"
        '    ["Configuration"] {\n'
        "      inputs {\n"
        f"{prefix}{widgets}"
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    p = tmp_path / "contract" / "app.pkl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(pkl, encoding="utf-8")


_TWO_WIDGETS = (
    '        ["include-filter"] = new Config.SqlTree {\n'
    '          title = "Include"\n'
    "        }\n"
    '        ["include-database-regex"] = new Config.TextInput {\n'
    '          title = "Include by regex"\n'
    "        }\n"
)


# ---------------------------------------------------------------------------
# K019 — the motivating incident (WARE-1323)
# ---------------------------------------------------------------------------


def test_k019_fires_when_form_key_has_no_placeholder(tmp_path: Path) -> None:
    """The WARE-1323 shape: a regex form key exists but was never plumbed."""
    paths = _write_py(tmp_path, {"app.py": _MINIMAL_APP})
    _write_pkl(tmp_path, _TWO_WIDGETS)
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({"include_filter": "{{include-filter}}"})},
    )
    findings = _only(scan_all(paths, tmp_path), "K019")
    assert len(findings) == 1
    assert "include-database-regex" in findings[0].message
    assert findings[0].file == "contract/app.pkl"


def test_k019_silent_when_every_form_key_is_wired(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": _MINIMAL_APP})
    _write_pkl(tmp_path, _TWO_WIDGETS)
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {
            "extract": _extract_node(
                {
                    "include_filter": "{{include-filter}}",
                    "include_database_regex": "{{include-database-regex}}",
                }
            )
        },
    )
    assert _only(scan_all(paths, tmp_path), "K019") == []


def test_k019_accepts_placeholder_on_downstream_node(tmp_path: Path) -> None:
    """A form key may be threaded into a downstream node instead of extract.

    athena wires ``{{connection}}`` into the publish node's ``connection_entity``;
    reporting that as missing would be a false positive.
    """
    paths = _write_py(tmp_path, {"app.py": _MINIMAL_APP})
    _write_pkl(
        tmp_path,
        '        ["connection"] = new Config.ConnectionInput {\n'
        '          title = "Connection"\n'
        "        }\n",
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {
            "extract": _extract_node({}),
            "publish": {
                "activity_name": "execute_workflow",
                "app_name": "publish",
                "inputs": {"args": {"connection_entity": "{{connection}}"}},
            },
        },
    )
    assert _only(scan_all(paths, tmp_path), "K019") == []


def test_k019_ignores_system_placeholders_without_form_keys(tmp_path: Path) -> None:
    """``{{credential}}`` / ``{{agent-json}}`` are SDK-injected, not form fields."""
    paths = _write_py(tmp_path, {"app.py": _MINIMAL_APP})
    _write_pkl(
        tmp_path,
        '        ["include-filter"] = new Config.SqlTree {\n'
        '          title = "Include"\n'
        "        }\n",
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {
            "extract": _extract_node(
                {
                    "include_filter": "{{include-filter}}",
                    "credential": "{{credential}}",
                    "agent_json": "{{agent-json}}",
                }
            )
        },
    )
    assert _only(scan_all(paths, tmp_path), "K019") == []


def test_k019_ignores_ui_rule_references(tmp_path: Path) -> None:
    """``rules { whenInputs { ["x"] = "y" } }`` names keys but declares no widget."""
    paths = _write_py(tmp_path, {"app.py": _MINIMAL_APP})
    pkl = (
        'amends "@app-contract-toolkit/App.pkl"\n'
        "uiConfig {\n"
        "  tasks {\n"
        '    ["Configuration"] {\n'
        "      inputs {\n"
        '        ["include-filter"] = new Config.SqlTree {\n'
        '          title = "Include"\n'
        "        }\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "  rules {\n"
        "    new Config.UIRule {\n"
        '      whenInputs { ["advanced-config"] = "custom" }\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
    p = tmp_path / "contract" / "app.pkl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(pkl, encoding="utf-8")
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({"include_filter": "{{include-filter}}"})},
    )
    assert _only(scan_all(paths, tmp_path), "K019") == []


def test_k019_ignores_non_value_widgets(tmp_path: Path) -> None:
    """Presentational and preflight widgets hold nothing the workflow needs.

    ``InfoBanner`` is pure display. ``Sage``/``SageV2`` run preflight checks in
    the UI against a single canonical ``preflight_check`` arg, so apps declare
    several UIRule-selected variants that share it. Counting either as unwired
    produced 8 by-design findings across the fleet.
    """
    paths = _write_py(tmp_path, {"app.py": _MINIMAL_APP})
    _write_pkl(
        tmp_path,
        '        ["sql-connection-info-note"] = new Config.InfoBanner {\n'
        '          content = "**Note**"\n'
        "        }\n"
        '        ["preflight-check-with-tags"] = new Config.SageV2 {\n'
        '          title = ""\n'
        "        }\n"
        '        ["preflight-check"] = new Config.Sage {\n'
        '          title = ""\n'
        "        }\n",
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({})},
    )
    assert _only(scan_all(paths, tmp_path), "K019") == []


def test_k019_still_fires_for_real_value_widgets(tmp_path: Path) -> None:
    """The exclusion must not swallow ordinary config controls beside them."""
    paths = _write_py(tmp_path, {"app.py": _MINIMAL_APP})
    _write_pkl(
        tmp_path,
        '        ["sql-connection-info-note"] = new Config.InfoBanner {\n'
        '          content = "**Note**"\n'
        "        }\n"
        '        ["use-source-schema-filtering"] = new Config.Radio {\n'
        '          title = "Schema filtering"\n'
        "        }\n",
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({})},
    )
    findings = _only(scan_all(paths, tmp_path), "K019")
    assert [f.discriminator for f in findings] == ["use-source-schema-filtering"]


def test_k019_silent_without_ui_config(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": _MINIMAL_APP})
    p = tmp_path / "contract" / "app.pkl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('amends "@app-contract-toolkit/App.pkl"\nname = "myapp"\n', "utf-8")
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({})},
    )
    assert _only(scan_all(paths, tmp_path), "K019") == []


def test_k019_suppression(tmp_path: Path) -> None:
    paths = _write_py(tmp_path, {"app.py": _MINIMAL_APP})
    _write_pkl(
        tmp_path,
        "        // conformance: ignore[K019] frontend-only control\n"
        '        ["include-database-regex"] = new Config.TextInput {\n'
        '          title = "Include by regex"\n'
        "        }\n",
    )
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({})},
    )
    findings = _only(scan_all(paths, tmp_path), "K019")
    assert findings, "the finding should still be emitted, just suppressed"
    assert all(f.suppressed for f in findings)
    assert findings[0].suppression_justification == "frontend-only control"


def test_k019_finding_carries_discriminator(tmp_path: Path) -> None:
    """Multiple missing keys must stay distinct for per-key suppression."""
    paths = _write_py(tmp_path, {"app.py": _MINIMAL_APP})
    _write_pkl(tmp_path, _TWO_WIDGETS)
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node({})},
    )
    findings = _only(scan_all(paths, tmp_path), "K019")
    assert {f.discriminator for f in findings} == {
        "include-filter",
        "include-database-regex",
    }
