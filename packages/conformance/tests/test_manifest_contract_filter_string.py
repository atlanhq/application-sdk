"""Tests for K021 FilterFieldRejectsAeString (CONNECT-1333 / CONNECT-1389).

The sibling of K018. K018 checks only that an ``include_*`` / ``exclude_*`` arg
is *declared* somewhere on the entrypoint's Input contract; it deliberately does
not look at the field's *type*. K021 covers exactly that gap: since
contract-toolkit >= 0.9.0 the Automation Engine renders filters as top-level
flat JSON *strings* (``'{}'``, ``'{"x": []}'``), so a field typed as a strict
``dict`` with no string-acceptance path REJECTS the payload and the workflow
crashes at validation — the real offenders being atlan-looker-app and
atlan-fabric-app.

A filter field is **safe** if ANY of these holds, and each is pinned here:

* **str union** — the annotation unions with ``str`` (``FilterMap | str``,
  ``dict[str, list[str]] | str``, ``str | dict[...]``, ``Annotated[... | str]``).
  A bare ``ExtractionInput`` subclass with no in-repo filter ``AnnAssign`` is
  this path (inherited ``FilterMap | str``). A redeclared strict ``dict`` is
  **not** — the live SDK coercer opts out when the type override drops
  ``json_schema_extra``.
* **own before-validator** — a ``@field_validator("<field>", mode="before")``
  (or a ``mode="before"`` ``@model_validator``) on the app's own resolved chain
  targets the field (the Sigma/Qlik app-local fix pattern).

A finding is emitted only when the field IS a (possibly ``Annotated``) ``dict``
with NO str union and NO before-validator targeting it. Assert on
``finding.discriminator`` (the field name), never on message substrings — the
message names ``ExtractionInput`` and ``str`` as remediation advice.
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


def _flagged(findings: list, rule_id: str) -> set[str]:
    """The filter field names actually reported, read off the discriminator."""
    return {f.discriminator for f in findings if f.rule_id == rule_id}


def _app_src(input_body: str, *, bases: str = "(Input)") -> str:
    """A single-entrypoint app whose Input contract is under test."""
    return (
        "from application_sdk.app import App, entrypoint\n"
        "from application_sdk.contracts.base import Input, Output\n"
        "from application_sdk.templates.contracts.sql_metadata import "
        "ExtractionInput, FilterMap\n"
        "from pydantic import field_validator, model_validator\n"
        "from typing import Annotated, Any\n"
        "\n"
        f"class ExtractInput{bases}:\n"
        f"{input_body}"
        "\n"
        "class ExtractOutput(Output):\n"
        "    status: str = ''\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def extract(self, input: ExtractInput) -> ExtractOutput:\n"
        "        pass\n"
    )


def _run(tmp_path: Path, src: str, *, args: dict | None = None) -> list:
    """Write the app + a minimal single-mode manifest, then scan."""
    paths = _write_py(tmp_path, {"app.py": src})
    _write_manifest(
        tmp_path / "app" / "generated" / "manifest.json",
        {"extract": _extract_node(args if args is not None else {})},
    )
    return scan_all(paths, tmp_path)


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_k021_rule_metadata() -> None:
    rule = get_rule("K021")
    assert rule.name == "FilterFieldRejectsAeString"
    assert rule.tier is EnforcementTier.WARN  # advisory — must not block the fleet
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"
    assert rule.rationale


# ---------------------------------------------------------------------------
# RED — the offender: a strict dict filter with no string-acceptance path
# ---------------------------------------------------------------------------


def test_k021_fires_on_strict_dict_filter(tmp_path: Path) -> None:
    """``include_filter: dict[str, Any]`` on a plain Input base — the AE string
    ('{}') is rejected and the workflow crashes at validation."""
    findings = _run(tmp_path, _app_src("    include_filter: dict[str, Any]\n"))
    assert _flagged(findings, "K021") == {"include_filter"}


def test_k021_fires_on_annotated_dict_filter(tmp_path: Path) -> None:
    """An ``Annotated[dict[...], MaxLen]`` is still a strict dict underneath."""
    src = _app_src("    include_filter: Annotated[dict[str, list[str]], 'meta']\n")
    assert _flagged(_run(tmp_path, src), "K021") == {"include_filter"}


def test_k021_fires_on_exclude_filter_too(tmp_path: Path) -> None:
    """Both the include_* and exclude_* prefixes are in scope."""
    src = _app_src(
        "    include_filter: dict[str, Any]\n    exclude_filter: dict[str, Any]\n"
    )
    assert _flagged(_run(tmp_path, src), "K021") == {"include_filter", "exclude_filter"}


def test_k021_finding_anchored_on_contract_file(tmp_path: Path) -> None:
    findings = _unsuppressed(
        _run(tmp_path, _app_src("    include_filter: dict[str, Any]\n")), "K021"
    )
    assert len(findings) == 1
    assert findings[0].file == "app.py"


# ---------------------------------------------------------------------------
# GREEN — str union (acceptance #1)
# ---------------------------------------------------------------------------


def test_k021_silent_on_filtermap_str_union(tmp_path: Path) -> None:
    src = _app_src("    include_filter: FilterMap | str = ''\n")
    assert _only(_run(tmp_path, src), "K021") == []


def test_k021_silent_on_dict_str_union(tmp_path: Path) -> None:
    src = _app_src("    include_filter: dict[str, list[str]] | str = ''\n")
    assert _only(_run(tmp_path, src), "K021") == []


def test_k021_silent_on_str_dict_union_either_order(tmp_path: Path) -> None:
    """``str | dict[...]`` — str on the left — is equally safe."""
    src = _app_src("    include_filter: str | dict[str, Any] = ''\n")
    assert _only(_run(tmp_path, src), "K021") == []


def test_k021_silent_on_annotated_str_union(tmp_path: Path) -> None:
    src = _app_src("    include_filter: Annotated[dict[str, Any] | str, 'm'] = ''\n")
    assert _only(_run(tmp_path, src), "K021") == []


# ---------------------------------------------------------------------------
# GREEN — inherited ExtractionInput annotation (acceptance #1, no AnnAssign)
# ---------------------------------------------------------------------------


def test_k021_silent_when_chain_reaches_extraction_input(tmp_path: Path) -> None:
    """A bare ExtractionInput subclass inherits ``include_filter: FilterMap | str``."""
    assert (
        _only(_run(tmp_path, _app_src("    pass\n", bases="(ExtractionInput)")), "K021")
        == []
    )


def test_k021_fires_when_extraction_input_subclass_redeclares_strict_dict(
    tmp_path: Path,
) -> None:
    """A redeclared strict ``dict`` under ExtractionInput is the CONNECT-1333
    shape: the type override drops ``json_schema_extra``, so ``_coerce_filter``
    returns the AE string unchanged and Pydantic rejects it."""
    src = _app_src("    include_filter: dict[str, Any]\n", bases="(ExtractionInput)")
    assert _flagged(_run(tmp_path, src), "K021") == {"include_filter"}


def test_k021_fires_for_in_repo_base_that_reaches_extraction_input(
    tmp_path: Path,
) -> None:
    """An in-repo intermediate base does not exempt a redeclared strict dict."""
    src = (
        "from application_sdk.app import App, entrypoint\n"
        "from application_sdk.contracts.base import Output\n"
        "from application_sdk.templates.contracts.sql_metadata import ExtractionInput\n"
        "from typing import Any\n"
        "\n"
        "class MyBase(ExtractionInput):\n"
        "    pass\n"
        "\n"
        "class ExtractInput(MyBase):\n"
        "    include_filter: dict[str, Any]\n"
        "\n"
        "class ExtractOutput(Output):\n"
        "    status: str = ''\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def extract(self, input: ExtractInput) -> ExtractOutput:\n"
        "        pass\n"
    )
    assert _flagged(_run(tmp_path, src), "K021") == {"include_filter"}


# ---------------------------------------------------------------------------
# GREEN — own before-validator (acceptance #2)
# ---------------------------------------------------------------------------


def test_k021_silent_with_before_field_validator(tmp_path: Path) -> None:
    """A ``@field_validator(..., mode='before')`` on the field coerces the string."""
    src = _app_src(
        "    include_filter: dict[str, Any] = {}\n"
        "\n"
        "    @field_validator('include_filter', mode='before')\n"
        "    @classmethod\n"
        "    def _coerce(cls, v):\n"
        "        return v\n"
    )
    assert _only(_run(tmp_path, src), "K021") == []


def test_k021_silent_with_before_model_validator(tmp_path: Path) -> None:
    """A ``mode='before'`` model_validator runs before field validation for all fields."""
    src = _app_src(
        "    include_filter: dict[str, Any] = {}\n"
        "\n"
        "    @model_validator(mode='before')\n"
        "    @classmethod\n"
        "    def _coerce(cls, data):\n"
        "        return data\n"
    )
    assert _only(_run(tmp_path, src), "K021") == []


def test_k021_still_fires_for_after_mode_field_validator(tmp_path: Path) -> None:
    """An ``after`` validator runs post-coercion — the string was already rejected."""
    src = _app_src(
        "    include_filter: dict[str, Any] = {}\n"
        "\n"
        "    @field_validator('include_filter', mode='after')\n"
        "    @classmethod\n"
        "    def _check(cls, v):\n"
        "        return v\n"
    )
    assert _flagged(_run(tmp_path, src), "K021") == {"include_filter"}


def test_k021_before_validator_for_other_field_does_not_exempt(tmp_path: Path) -> None:
    """A before-validator targeting a *different* field does not cover this one."""
    src = _app_src(
        "    include_filter: dict[str, Any] = {}\n"
        "    exclude_filter: dict[str, Any] = {}\n"
        "\n"
        "    @field_validator('exclude_filter', mode='before')\n"
        "    @classmethod\n"
        "    def _coerce(cls, v):\n"
        "        return v\n"
    )
    assert _flagged(_run(tmp_path, src), "K021") == {"include_filter"}


# ---------------------------------------------------------------------------
# GREEN — nothing to flag / no-op philosophy
# ---------------------------------------------------------------------------


def test_k021_silent_when_no_filter_fields(tmp_path: Path) -> None:
    assert _only(_run(tmp_path, _app_src("    connection: str = ''\n")), "K021") == []


def test_k021_silent_when_non_dict_filter_field(tmp_path: Path) -> None:
    """A prefix match that is not a dict (e.g. a bool toggle) cannot reject a
    string in the way this rule is about."""
    src = _app_src("    include_archived: bool = False\n")
    assert _only(_run(tmp_path, src), "K021") == []


def test_k021_noop_when_generated_dir_absent(tmp_path: Path) -> None:
    """No ``app/generated/`` → not a contract-toolkit app → stay silent."""
    paths = _write_py(
        tmp_path, {"app.py": _app_src("    include_filter: dict[str, Any]\n")}
    )
    assert _only(scan_all(paths, tmp_path), "K021") == []


def test_k021_silent_when_ancestor_unresolvable(tmp_path: Path) -> None:
    """An unknown third-party base is an incomplete picture — don't guess."""
    src = _app_src("    include_filter: dict[str, Any]\n", bases="(SomeUnknownBase)")
    assert _only(_run(tmp_path, src), "K021") == []


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def test_k021_suppression(tmp_path: Path) -> None:
    src = _app_src("    include_filter: dict[str, Any]\n").replace(
        "class ExtractInput(Input):",
        "# conformance: ignore[K021] filter coercion handled downstream\n"
        "class ExtractInput(Input):",
    )
    findings = _only(_run(tmp_path, src), "K021")
    assert findings, "the finding should still be emitted, just suppressed"
    assert all(f.suppressed for f in findings)
    assert _unsuppressed(_run(tmp_path, src), "K021") == []
