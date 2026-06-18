"""Meta-tests for the O-series optimisation checks (O001).

These checks are shipped in the conformance package and fanned out across the
fleet — a buggy check false-positives across hundreds of apps and triggers
spurious remediations (BLDX-1394).  So each rule is tested to fire *exactly*
when it should and stay silent otherwise: both false positives and false
negatives are guarded.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.optimizations import (
    _collect_json_bindings,
    main,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema import SarifReport, derive_disposition, validate_sarif
from conformance.suite.schema.disposition import Disposition, EnforcementTier


def _ids(src: str) -> list[str]:
    return [f.rule_id for f in scan_text(src, "x.py")]


# ── O001 OrjsonOverStdlibJson ──────────────────────────────────────────────────


def test_o001_fires_on_json_dumps_and_loads() -> None:
    src = "import json\n\ndef f(s):\n    return json.dumps(json.loads(s))\n"
    assert _ids(src) == ["O001", "O001"]


def test_o001_fires_on_aliased_import() -> None:
    src = "import json as j\n\ndef f():\n    return j.dumps({})\n"
    assert _ids(src) == ["O001"]


def test_o001_fires_on_from_import() -> None:
    src = "from json import loads as jloads\n\ndef f(s):\n    return jloads(s)\n"
    assert _ids(src) == ["O001"]


def test_o001_silent_on_orjson() -> None:
    src = "import orjson\n\ndef f():\n    return orjson.dumps({})\n"
    assert _ids(src) == []


def test_o001_silent_on_response_json_method() -> None:
    # resp.json() is an attribute call on an arbitrary object, not stdlib json.
    src = "def f(resp):\n    return resp.json()\n"
    assert _ids(src) == []


def test_o001_silent_without_json_import() -> None:
    # A bare `json.dumps` with no `import json` binding must not fire (the name
    # could be any object). Import resolution is required.
    src = "def f(json):\n    return json.dumps({})\n"
    assert _ids(src) == []


def test_o001_silent_on_dump_load_file_apis() -> None:
    # orjson has no dump()/load() file-object equivalent — out of scope.
    src = "import json\n\ndef f(fp, obj):\n    json.dump(obj, fp)\n    return json.load(fp)\n"
    assert _ids(src) == []


def test_o001_silent_on_json_decode_error() -> None:
    src = (
        "import json\n\n"
        "def f(s):\n"
        "    try:\n"
        "        return s\n"
        "    except json.JSONDecodeError:\n"
        "        return None\n"
    )
    assert _ids(src) == []


def test_o001_suppressed_by_trailing_directive() -> None:
    src = "import json\n\ndef f():\n    return json.dumps({})  # conformance: ignore[O001] one-off\n"
    findings = scan_text(src, "x.py")
    assert len(findings) == 1
    assert findings[0].suppressed is True


# ── _collect_json_bindings ──────────────────────────────────────────────────────


def test_collect_bindings_module_and_func() -> None:
    import ast

    tree = ast.parse(
        "import json as j\nfrom json import dumps, loads as ld\nfrom json import JSONDecodeError\n"
    )
    module_names, func_names = _collect_json_bindings(tree)
    assert module_names == frozenset({"j"})
    assert func_names == frozenset({"dumps", "ld"})


def test_collect_bindings_function_local_import() -> None:
    import ast

    tree = ast.parse("def f():\n    import json\n    return json.dumps({})\n")
    module_names, _ = _collect_json_bindings(tree)
    assert module_names == frozenset({"json"})


# ── tier / disposition / gate ───────────────────────────────────────────────────


def test_o001_is_warn_tier() -> None:
    assert get_rule("O001").tier is EnforcementTier.WARN


def test_o001_warn_findings_do_not_fail_the_gate(tmp_path: Path) -> None:
    """O001 is WARN — main() exits 0 (non-blocking)."""
    (tmp_path / "m.py").write_text(
        "import json\n\n\ndef f():\n    return json.dumps({})\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 0


def test_o001_result_is_warning_disposition(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "import json\n\n\ndef f():\n    return json.dumps({})\n"
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            str(tmp_path / "m.py"),
            "--sarif-output",
            str(sarif_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    dispositions = [derive_disposition(r) for r in report.runs[0].results]
    assert dispositions == [Disposition.WARNING]


def test_o001_sarif_output_validates(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "import json\n\n\ndef f():\n    return json.dumps({})\n"
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            str(tmp_path / "m.py"),
            "--sarif-output",
            str(sarif_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    validate_sarif(report)
