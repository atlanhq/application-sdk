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


# ── O001 exact-bytes carve-out ──────────────────────────────────────────────────


def test_o001_terminal_state_licenses_stdlib_only_for_a_named_external_consumer() -> (
    None
):
    """orjson cannot reproduce stdlib ``json.dumps``'s default bytes.

    It has no separators option, no ``ensure_ascii`` option and no >64-bit
    ints. So a ``dumps`` whose string is published as an asset attribute, hashed
    or byte-compared outside the app has no compliant swap: the only end state
    is a justified directive, and the reason must name that consumer. Without a
    ``terminal_state`` saying so, a remediation lane strips the directive and
    re-applies a swap that churns every published asset (FND-2509).
    """
    terminal_state = " ".join(get_rule("O001").terminal_state.split())
    for needle in (
        "# conformance: ignore[O001] <reason>",
        "one attribute or field value",
        "hashes or byte-compares that value as text",
        "outside the app",
        "names the attribute key or field and the location (repo and file:line)",
        "serializes a whole entity or document does not qualify",
        "parses the document before it diffs it",
    ):
        assert (
            needle in terminal_state
        ), f"O001's terminal_state does not state {needle!r}"


def test_o001_terminal_state_sends_a_byte_identical_call_to_the_swap() -> None:
    """A ``dumps`` already passing ``separators=(",", ":")`` and
    ``ensure_ascii=False`` is byte-identical under orjson, so an external
    text hash is unchanged and the site has a compliant swap. The terminal
    state must not license stdlib for it just because its consumer is external.
    """
    terminal_state = " ".join(get_rule("O001").terminal_state.split())
    for needle in (
        '`separators=(",", ":")`',
        "`ensure_ascii=False`",
        "integer above 64 bits",
        "byte-identical to `orjson.dumps(...).decode()` and makes the swap",
    ):
        assert (
            needle in terminal_state
        ), f"O001's terminal_state does not state {needle!r}"


def test_o001_exact_bytes_site_is_still_reported_and_suppressed_only_with_directive(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.py").write_text(
        "import json\n\n\n"
        "def published(fields):\n"
        "    return json.dumps(fields)  # conformance: ignore[O001] rawDataTypeDefinition, "
        "hashed as text at example-consumer/diff.py:22\n\n\n"
        "def plain(fields):\n"
        "    return json.dumps(fields)\n"
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
    results = report.runs[0].results
    assert [r.rule_id for r in results] == ["O001", "O001"]
    assert [derive_disposition(r) for r in results] == [
        Disposition.SUPPRESSED,
        Disposition.WARNING,
    ]
