"""Tests for the asset-mapper usage rules (P025, O002, O003 — BLDX-1492).

P025 lives in the prescriptions check package; O002/O003 in the optimizations
package.  Each is exercised through its package ``scan_text`` so the real wiring
(import collection, suppression handling) is covered, not just the bare detector.
"""

from __future__ import annotations

from conformance.suite.checks.optimizations import scan_text as o_scan
from conformance.suite.checks.prescriptions import scan_text as p_scan


def _p_ids(src: str) -> list[str]:
    return [f.rule_id for f in p_scan(src, "app/x.py") if not f.suppressed]


def _o_ids(src: str) -> list[str]:
    return [f.rule_id for f in o_scan(src, "app/x.py") if not f.suppressed]


# ── P025 LegacyPyatlanAssetImport ───────────────────────────────────────────────


def test_p025_fires_on_from_import() -> None:
    assert "P025" in _p_ids("from pyatlan.model.assets import Table\n")


def test_p025_fires_on_module_import() -> None:
    assert "P025" in _p_ids("import pyatlan.model.assets\n")


def test_p025_fires_on_submodule_from_import() -> None:
    assert "P025" in _p_ids("from pyatlan.model import assets\n")


def test_p025_silent_on_v9_import() -> None:
    assert "P025" not in _p_ids("from pyatlan_v9.model.assets import Table\n")


def test_p025_silent_on_non_asset_pyatlan_import() -> None:
    # The narrow scope: enums (no v9 equivalent) must not be flagged.
    assert "P025" not in _p_ids("from pyatlan.model.enums import AtlanConnectorType\n")


def test_p025_suppressed_inline() -> None:
    src = (
        "from pyatlan.model.assets import Table  "
        "# conformance: ignore[P025] legacy AtlasTransformer connector\n"
    )
    findings = [f for f in p_scan(src, "app/x.py") if f.rule_id == "P025"]
    assert len(findings) == 1
    assert findings[0].suppressed is True


# ── O002 LegacyAssetSerialization ───────────────────────────────────────────────


def test_o002_fires_on_dict_in_asset_module() -> None:
    src = (
        "from pyatlan_v9.model.assets import Table\n\n\n"
        "def serialize(asset):\n    return asset.dict()\n"
    )
    assert "O002" in _o_ids(src)


def test_o002_silent_when_module_does_not_import_assets() -> None:
    src = "def serialize(model):\n    return model.dict()\n"
    assert "O002" not in _o_ids(src)


def test_o002_silent_on_json_method() -> None:
    # .json() is overwhelmingly response.json() — intentionally not matched.
    src = (
        "from pyatlan_v9.model.assets import Table\n\n\n"
        "def fetch(client):\n    return client.get('/x').json()\n"
    )
    assert "O002" not in _o_ids(src)


def test_o002_suppressed_inline() -> None:
    src = (
        "from pyatlan_v9.model.assets import Table\n\n\n"
        "def serialize(asset):\n"
        "    return asset.dict()  # conformance: ignore[O002] non-asset payload\n"
    )
    findings = [f for f in o_scan(src, "app/x.py") if f.rule_id == "O002"]
    assert len(findings) == 1
    assert findings[0].suppressed is True


# ── O003 UntypedAssetMapperReturn ───────────────────────────────────────────────


def test_o003_fires_on_untyped_mapper() -> None:
    src = (
        "from pyatlan_v9.model.assets import Table\n\n\n"
        "def map_table(record, qn):\n"
        "    asset = Table(name=record.name, qualified_name=qn)\n"
        "    return asset\n"
    )
    assert "O003" in _o_ids(src)


def test_o003_silent_when_return_annotated() -> None:
    src = (
        "from pyatlan_v9.model.assets import Table\n\n\n"
        "def map_table(record, qn) -> Table:\n"
        "    asset = Table(name=record.name, qualified_name=qn)\n"
        "    return asset\n"
    )
    assert "O003" not in _o_ids(src)


def test_o003_silent_when_no_asset_constructed() -> None:
    src = (
        "from pyatlan_v9.model.assets import Table\n\n\n"
        "def map_config(record):\n    return {'name': record.name}\n"
    )
    assert "O003" not in _o_ids(src)


def test_o003_silent_when_function_returns_nothing() -> None:
    src = (
        "from pyatlan_v9.model.assets import Table\n\n\n"
        "def stamp(asset):\n    Table(name='x')\n    return\n"
    )
    assert "O003" not in _o_ids(src)


def test_o003_silent_when_only_nested_closure_returns() -> None:
    # The outer function builds an asset but returns nothing; only a nested
    # closure returns a value. Scope-aware walking must not mis-attribute the
    # inner return to the outer function.
    src = (
        "from pyatlan_v9.model.assets import Table\n\n\n"
        "def register(record, qn):\n"
        "    asset = Table(name=record.name, qualified_name=qn)\n"
        "    def _key():\n"
        "        return asset.qualified_name\n"
        "    _registry.append((_key, asset))\n"
    )
    assert "O003" not in _o_ids(src)


def test_o003_suppressed_inline() -> None:
    src = (
        "from pyatlan_v9.model.assets import Table\n\n\n"
        "def map_table(record, qn):  # conformance: ignore[O003] wide return on purpose\n"
        "    asset = Table(name=record.name, qualified_name=qn)\n"
        "    return asset\n"
    )
    findings = [f for f in o_scan(src, "app/x.py") if f.rule_id == "O003"]
    assert len(findings) == 1
    assert findings[0].suppressed is True
