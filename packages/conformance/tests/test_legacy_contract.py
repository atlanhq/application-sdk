"""Tests for K001 ContractAmendsLegacyModule and K002 LegacyContractApi checks.

Each test builds a minimal temporary repo tree in ``tmp_path``, writes one or
more ``contract/**/*.pkl`` files, calls the scanner, and asserts on the
returned findings.

Naming conventions
------------------
* ``pkl_content``: the raw text to write to ``contract/app.pkl``
  (or any path under ``contract/``).
* Findings are filtered by ``rule_id`` for precise assertions.
* ``suppressed=True`` findings are returned by the scanner but carry the
  ``suppressed`` flag; the runner treats them as non-blocking.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from conformance.suite.checks.legacy_contract import discover, scan_path
from conformance.suite.rules import get_rule
from conformance.suite.schema.catalog import rule_help_uri
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_repo(tmp_path: Path, pkl_files: dict[str, str]) -> list[Path]:
    """Write ``contract/**/*.pkl`` files under *tmp_path*.

    Returns the list of paths actually written, mirroring the output of
    :func:`discover`.
    """
    paths: list[Path] = []
    for rel, content in pkl_files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        paths.append(p)
    return paths


def _scan_all(tmp_path: Path, pkl_files: dict[str, str]) -> list:
    """Write files and run scan_path on each, aggregating findings."""
    paths = _write_repo(tmp_path, pkl_files)
    findings = []
    for path in paths:
        findings.extend(scan_path(path, tmp_path))
    return findings


# ---------------------------------------------------------------------------
# Conformant contract — no findings
# ---------------------------------------------------------------------------


def test_conformant_app_pkl_no_findings(tmp_path: Path) -> None:
    """A contract that amends App.pkl and uses no legacy APIs → zero findings."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"

        name = "my-connector"
        connector = Connectors.MYSQL
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert findings == []


def test_no_contract_dir_no_findings(tmp_path: Path) -> None:
    """A repo with no ``contract/`` directory → discover returns [] → no findings."""
    paths = discover(tmp_path)
    assert paths == []


# ---------------------------------------------------------------------------
# K001 — amends legacy base module
# ---------------------------------------------------------------------------


def test_k001_nativeapp(tmp_path: Path) -> None:
    """K001 fires for ``amends "@app-contract-toolkit/NativeApp.pkl"``."""
    content = dedent("""\
        amends "@app-contract-toolkit/NativeApp.pkl"

        name = "my-connector"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1
    assert k001[0].line == 1
    assert "NativeApp.pkl" in k001[0].message
    assert "App.pkl" in k001[0].message
    assert not k001[0].suppressed


def test_k001_nativeappbundle(tmp_path: Path) -> None:
    """K001 fires for ``amends "@app-contract-toolkit/NativeAppBundle.pkl"``."""
    content = dedent("""\
        amends "@app-contract-toolkit/NativeAppBundle.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1
    assert "NativeAppBundle.pkl" in k001[0].message


def test_k001_relative_path(tmp_path: Path) -> None:
    """K001 also fires for relative-path amends (as used in toolkit examples)."""
    content = 'amends "../../src/NativeApp.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1


def test_k001_not_fired_for_app_pkl(tmp_path: Path) -> None:
    """K001 must NOT fire for ``amends "@app-contract-toolkit/App.pkl"``."""
    content = 'amends "@app-contract-toolkit/App.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert k001 == []


def test_k001_not_fired_for_commented_out_amends(tmp_path: Path) -> None:
    """A commented-out legacy amends line must NOT produce a K001 finding."""
    content = dedent("""\
        // amends "@app-contract-toolkit/NativeApp.pkl"
        amends "@app-contract-toolkit/App.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert k001 == []


def test_k001_not_fired_for_user_named_module(tmp_path: Path) -> None:
    """K001 must NOT fire for a user-named module that ends in 'NativeApp.pkl'.

    ``amends "@x/MyNativeApp.pkl"`` should not match — only exact legacy names
    (``NativeApp.pkl`` or ``NativeAppBundle.pkl``) immediately after a ``/``.
    """
    content = 'amends "@x/MyNativeApp.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert k001 == [], f"Unexpected K001 firing: {k001}"


def test_k001_not_fired_for_inline_trailing_comment(tmp_path: Path) -> None:
    """An amends target in an inline trailing comment must NOT fire K001.

    ``name = "foo"  // amends "@x/NativeApp.pkl"`` — the ``amends`` keyword
    appears only inside a ``//`` comment, so K001 must not fire.
    """
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        name = "foo"  // amends "@app-contract-toolkit/NativeApp.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert k001 == [], f"Unexpected K001 firing: {k001}"


def test_k001_not_fired_for_block_commented_amends(tmp_path: Path) -> None:
    """A block-comment-wrapped legacy amends must NOT produce a K001 finding."""
    content = dedent("""\
        /*
        amends "@app-contract-toolkit/NativeApp.pkl"
        */
        amends "@app-contract-toolkit/App.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert k001 == []


def test_k001_suppressed_by_directive_on_same_line(tmp_path: Path) -> None:
    """``// conformance: ignore[K001] reason`` on the amends line suppresses K001."""
    content = (
        'amends "@app-contract-toolkit/NativeApp.pkl" '
        "// conformance: ignore[K001] phased migration tracked in BLDX-9999\n"
    )
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1
    assert k001[0].suppressed
    assert "BLDX-9999" in (k001[0].suppression_justification or "")


def test_k001_suppressed_by_directive_on_line_above(tmp_path: Path) -> None:
    """A comment-only directive on the line directly above suppresses K001."""
    content = dedent("""\
        // conformance: ignore[K001] phased migration: BLDX-9999
        amends "@app-contract-toolkit/NativeApp.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1
    assert k001[0].suppressed


def test_k001_no_suppression_without_justification(tmp_path: Path) -> None:
    """A bare ``// conformance: ignore[K001]`` with no text must NOT suppress."""
    content = dedent("""\
        // conformance: ignore[K001]
        amends "@app-contract-toolkit/NativeApp.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 1
    assert not k001[0].suppressed


def test_k001_multiple_entrypoint_files(tmp_path: Path) -> None:
    """Each legacy contract file in a multi-entrypoint repo produces its own K001."""
    crawler = 'amends "@app-contract-toolkit/NativeApp.pkl"\nname = "crawler"\n'
    miner = 'amends "@app-contract-toolkit/NativeApp.pkl"\nname = "miner"\n'
    findings = _scan_all(
        tmp_path,
        {
            "contract/crawler.pkl": crawler,
            "contract/miner.pkl": miner,
        },
    )
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert len(k001) == 2
    files = {f.file for f in k001}
    assert any("crawler.pkl" in fn for fn in files)
    assert any("miner.pkl" in fn for fn in files)


# ---------------------------------------------------------------------------
# K002 — NativeApp-only properties
# ---------------------------------------------------------------------------


def test_k002_flat_manifest_args(tmp_path: Path) -> None:
    """K002 fires for ``flatManifestArgs``."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        flatManifestArgs = true
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "flatManifestArgs" in k002[0].message
    assert "always emits flat" in k002[0].message


def test_k002_manifest_metadata_args(tmp_path: Path) -> None:
    """K002 fires for ``manifestMetadataArgs``."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        manifestMetadataArgs = false
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "manifestMetadataArgs" in k002[0].message


def test_k002_workflow_type_override(tmp_path: Path) -> None:
    """K002 fires for ``workflowTypeOverride``."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        workflowTypeOverride = "my-custom-type"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "workflowTypeOverride" in k002[0].message


def test_k002_config_import(tmp_path: Path) -> None:
    """K002 fires for ``import "…Config.pkl"``."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        import "@app-contract-toolkit/Config.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "Config.pkl" in k002[0].message
    assert "re-exports widget types" in k002[0].message


def test_k002_connectors_import_is_legitimate(tmp_path: Path) -> None:
    """K002 must NOT fire for ``import "…Connectors.pkl"``.

    Unlike Config/Credential/Renderers, the Connectors registry is still imported
    explicitly by App.pkl consumers (App.pkl does not re-export its constants),
    so flagging it would be a false positive — verified against every current
    toolkit example, which imports it and uses ``Connectors.*``.
    """
    content = 'amends "@app-contract-toolkit/App.pkl"\nimport "@app-contract-toolkit/Connectors.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert k002 == [], f"Unexpected K002 firing on a legitimate import: {k002}"


def test_k002_credential_import(tmp_path: Path) -> None:
    """K002 fires for ``import "…Credential.pkl"`` (Argo-era)."""
    content = 'amends "@app-contract-toolkit/App.pkl"\nimport "@app-contract-toolkit/Credential.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "Argo-era" in k002[0].message


def test_k002_renderers_import(tmp_path: Path) -> None:
    """K002 fires for ``import "…Renderers.pkl"`` (Argo-era)."""
    content = 'amends "@app-contract-toolkit/App.pkl"\nimport "@app-contract-toolkit/Renderers.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "Argo-era" in k002[0].message


def test_k002_config_import_legitimate_when_amends_credential(
    tmp_path: Path,
) -> None:
    """K002 must NOT fire for ``import "…Config.pkl"`` in a credential contract.

    A contract that *amends* Credential.pkl is a credential-config sub-contract,
    not an App.pkl entrypoint.  Credential.pkl uses ``Config.*`` internally but,
    unlike App.pkl, does NOT re-export the widget types as unqualified
    typealiases, so the import is required for ``pkl eval`` to resolve
    ``Config.TextInput`` etc. — flagging it is a false positive.
    """
    content = dedent("""\
        amends "@app-contract-toolkit/Credential.pkl"
        import "@app-contract-toolkit/Config.pkl"

        name = "csa-connectors-objectstore"
        source = "Object Store"

        options {
          ["s3"] = new NestedCredentialInput {
            inputs {
              ["username"] = new Config.TextInput { title = "AWS access key" }
            }
          }
        }
    """)
    findings = _scan_all(tmp_path, {"contract/csa-connectors-objectstore.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert k002 == [], f"Unexpected K002 firing on a credential contract: {k002}"
    # The credential base itself is legacy: K001 reports the amends line once.
    assert [(f.rule_id, f.line) for f in findings if f.rule_id == "K001"] == [
        ("K001", 1)
    ]


def test_k002_import_fires_when_credential_amends_is_commented(
    tmp_path: Path,
) -> None:
    """The Credential.pkl exemption honours comment handling.

    A commented-out ``amends Credential.pkl`` line must NOT exempt an App.pkl
    contract from K002b — otherwise a stray comment could silence real findings.
    """
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        // amends "@app-contract-toolkit/Credential.pkl"
        import "@app-contract-toolkit/Config.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "Config.pkl" in k002[0].message


def test_k002_fires_for_renderers_import_on_credential_contract(
    tmp_path: Path,
) -> None:
    """The exemption is scoped to ``Config.pkl`` only.

    Credential.pkl imports and invokes ``Renderers.pkl`` itself, so an amending
    contract has no reason to import ``Renderers.pkl``.  Such an import is
    genuinely legacy, so K002b must still fire for it even on a credential base —
    only the legitimate ``Config.pkl`` import is exempt.
    """
    content = dedent("""\
        amends "@app-contract-toolkit/Credential.pkl"
        import "@app-contract-toolkit/Config.pkl"
        import "@app-contract-toolkit/Renderers.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/csa-connectors-objectstore.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1, f"Expected exactly the Renderers.pkl finding: {k002}"
    assert "Renderers.pkl" in k002[0].message


def test_k002_import_fires_when_credential_amends_is_block_commented(
    tmp_path: Path,
) -> None:
    """The Credential.pkl exemption honours block-comment handling.

    A block-comment-wrapped ``amends Credential.pkl`` must NOT exempt an App.pkl
    contract from K002b — mirrors ``test_k001_not_fired_for_block_commented_amends``
    for coverage symmetry (block comments are pre-stripped before
    ``base_is_credential`` is computed).
    """
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        /*
        amends "@app-contract-toolkit/Credential.pkl"
        */
        import "@app-contract-toolkit/Config.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert "Config.pkl" in k002[0].message


def test_k002_not_fired_for_user_named_import(tmp_path: Path) -> None:
    """K002 must NOT fire for a user-named module that ends in 'Config.pkl'.

    ``import "@x/AppConfig.pkl"`` should not match — only the exact legacy
    module names (``Config.pkl`` etc.) immediately after a ``/``.
    """
    content = 'amends "@app-contract-toolkit/App.pkl"\nimport "@x/AppConfig.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert k002 == [], f"Unexpected K002 firing: {k002}"


def test_k002_property_in_line_comment_not_flagged(tmp_path: Path) -> None:
    """A property name that appears only in a line comment must NOT fire K002."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        // flatManifestArgs is a NativeApp.pkl-only knob, not used here
        name = "my-app"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert k002 == []


def test_k002_suppressed(tmp_path: Path) -> None:
    """A ``// conformance: ignore[K002] reason`` directive suppresses K002."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        // conformance: ignore[K002] flatManifestArgs intentionally kept during migration
        flatManifestArgs = true
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k002) == 1
    assert k002[0].suppressed


# ---------------------------------------------------------------------------
# The six ways a contract reaches a legacy module (FND-5546)
# ---------------------------------------------------------------------------

_PKG = (
    "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@0.39.2"
)


def _rules(findings: list) -> list[tuple[str, int]]:
    return sorted((f.rule_id, f.line) for f in findings)


def test_shape1_amends_package_alias(tmp_path: Path) -> None:
    """Shape 1: ``amends "@app-contract-toolkit/NativeApp.pkl"`` → K001."""
    content = 'amends "@app-contract-toolkit/NativeApp.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 1)]


def test_shape2_amends_package_uri(tmp_path: Path) -> None:
    """Shape 2: the ``package://…#/NativeApp.pkl`` URI form → K001.

    The ``//`` inside ``package://`` used to be read as a line comment, which
    cut the line before the module name.
    """
    content = f'amends "{_PKG}#/NativeApp.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 1)]
    assert "'NativeApp.pkl'" in findings[0].message


def test_shape3_import_alias_amend_expression(tmp_path: Path) -> None:
    """Shape 3: ``import … as X`` then ``(X) { … }`` → K002 import + K001 per body."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        import "@app-contract-toolkit/NativeApp.pkl" as Legacy

        local first = (Legacy) {
          name = "first"
        }
        local second = (Legacy) { name = "second" }
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 4), ("K001", 7), ("K002", 2)]
    k001 = [f for f in findings if f.rule_id == "K001"]
    assert all("'NativeApp.pkl'" in f.message for f in k001)
    assert all("(Legacy)" in f.message for f in k001)


def test_shape3_default_import_name_and_new_expression(tmp_path: Path) -> None:
    """Without ``as``, Pkl binds the module's own name; ``new X {`` also counts.

    ``new NativeApp.FieldSpec {`` instantiates a nested class, not the module,
    and must not fire.
    """
    content = dedent("""\
        import "@app-contract-toolkit/NativeApp.pkl"

        local a: NativeApp = new NativeApp {
          fields { new NativeApp.FieldSpec { name = "x" } }
        }
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 3), ("K002", 1)]


def test_shape3_unrelated_parenthesised_amend_not_flagged(tmp_path: Path) -> None:
    """``(X) {`` on a name not bound to a legacy import is ordinary Pkl."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        import "@app-contract-toolkit/Widgets.pkl" as W

        local base = (W) { }
        local other = (NativeApp) { }
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert findings == []


def test_shape4_amends_credential(tmp_path: Path) -> None:
    """Shape 4: ``amends "…/Credential.pkl"`` → K001 with the credential hint."""
    content = 'amends "@app-contract-toolkit/Credential.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/creds.pkl": content})
    assert _rules(findings) == [("K001", 1)]
    assert "additionalCredentialConfigs" in findings[0].message


def test_shape5_extends_config(tmp_path: Path) -> None:
    """Shape 5: ``extends "…/Config.pkl"`` → K001."""
    content = 'extends "@app-contract-toolkit/Config.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 1)]
    assert "'Config.pkl'" in findings[0].message


def test_shape6_import_nativeappbundle(tmp_path: Path) -> None:
    """Shape 6: a bare ``import "…/NativeAppBundle.pkl"`` → K002."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        import "@app-contract-toolkit/NativeAppBundle.pkl"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K002", 2)]
    assert "NativeAppBundle.pkl is a legacy base" in findings[0].message


def test_amends_renderers_flagged(tmp_path: Path) -> None:
    """All five legacy modules count as a legacy base, Renderers.pkl included."""
    content = 'amends "@app-contract-toolkit/Renderers.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 1)]


def test_current_modules_never_flagged(tmp_path: Path) -> None:
    """Widgets, Connectors, Deployment, Inputs, InputContract are current modules."""
    content = dedent("""\
        amends "@app-contract-toolkit/App.pkl"
        import "@app-contract-toolkit/Widgets.pkl"
        import "@app-contract-toolkit/Connectors.pkl"
        import "@app-contract-toolkit/Deployment.pkl" as D
        import "@app-contract-toolkit/Inputs.pkl"
        import "@app-contract-toolkit/InputContract.pkl"

        local x = (D) { }
        local y = new Widgets { }
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert findings == []


def test_amends_inputs_module_not_flagged(tmp_path: Path) -> None:
    """A generated-style ``amends "…/Inputs.pkl"`` is not a legacy base."""
    content = f'amends "{_PKG}#/Inputs.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert findings == []


def test_import_alias_suppressed_on_amend_line(tmp_path: Path) -> None:
    """The ``(X) {`` K001 finding honours a directive on the line above."""
    content = dedent("""\
        import "@app-contract-toolkit/NativeApp.pkl" as N  // conformance: ignore[K002] phased migration
        // conformance: ignore[K001] phased migration tracked separately
        local c = (N) { }
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 3), ("K002", 1)]
    assert all(f.suppressed for f in findings)


# ---------------------------------------------------------------------------
# String-aware comment stripping
# ---------------------------------------------------------------------------


def test_line_comment_marker_inside_string_is_code(tmp_path: Path) -> None:
    """``//`` inside a string literal does not start a comment."""
    content = f'amends "{_PKG}#/NativeAppBundle.pkl" // trailing note\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 1)]


def test_block_comment_marker_inside_string_is_code(tmp_path: Path) -> None:
    """``/*`` inside a string must not open a block comment that hides code."""
    content = dedent("""\
        prefix = "s3://bucket/*"
        amends "@app-contract-toolkit/NativeApp.pkl"
        suffix = "*/"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 2)]


def test_multiline_string_does_not_hide_or_leak(tmp_path: Path) -> None:
    """A ``\"\"\"`` string spanning lines is skipped as one literal.

    A ``//`` and a quote inside it neither start a comment nor end the string,
    and the code after it is still scanned on the right line.
    """
    content = dedent('''\
        description = """
          see https://example.com/docs "quoted" text
          """
        amends "@app-contract-toolkit/NativeApp.pkl"
    ''')
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 4)]


def test_custom_delimiter_string_is_skipped(tmp_path: Path) -> None:
    """``#"…"#`` raw strings can hold a bare quote without ending early."""
    content = dedent("""\
        note = #"a "quoted" // not a comment"#
        amends "@app-contract-toolkit/NativeApp.pkl" // a real comment
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 2)]


def test_block_comment_columns_preserved(tmp_path: Path) -> None:
    """Blanking a block comment keeps the column of the code after it."""
    content = '/* x */ amends "@app-contract-toolkit/NativeApp.pkl"\n'
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    assert _rules(findings) == [("K001", 1)]
    assert findings[0].column == 9


# ---------------------------------------------------------------------------
# Combined K001 + K002 (typical legacy contract)
# ---------------------------------------------------------------------------


def test_k001_and_k002_together(tmp_path: Path) -> None:
    """A typical legacy NativeApp.pkl contract fires K001 + multiple K002."""
    content = dedent("""\
        amends "@app-contract-toolkit/NativeApp.pkl"

        import "@app-contract-toolkit/Config.pkl"
        import "@app-contract-toolkit/Connectors.pkl"

        name = "my-connector"
        connector = Connectors.MYSQL
        flatManifestArgs = true
        workflowTypeOverride = "MyConnector"
    """)
    findings = _scan_all(tmp_path, {"contract/app.pkl": content})
    k001 = [f for f in findings if f.rule_id == "K001"]
    k002 = [f for f in findings if f.rule_id == "K002"]
    assert len(k001) == 1
    # flatManifestArgs + workflowTypeOverride + Config.pkl import (Connectors.pkl
    # is a legitimate import and must NOT fire).
    assert len(k002) == 3
    # All unsuppressed
    assert all(not f.suppressed for f in k001 + k002)


# ---------------------------------------------------------------------------
# discover() and scan_path()
# ---------------------------------------------------------------------------


def test_discover_finds_pkl_files(tmp_path: Path) -> None:
    """discover() returns all .pkl files under contract/."""
    _write_repo(
        tmp_path,
        {
            "contract/app.pkl": "amends ...\n",
            "contract/sub/entry.pkl": "amends ...\n",
            "app/generated/manifest.json": "{}",  # not a .pkl, not in contract/
        },
    )
    paths = discover(tmp_path)
    assert len(paths) == 2
    assert all(p.suffix == ".pkl" for p in paths)
    assert all("contract" in str(p) for p in paths)


_TOOLKIT_PKLPROJECT = dedent("""\
    amends "pkl:Project"

    dependencies {
      ["app-contract-toolkit"] {
        uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@0.39.2"
      }
    }
""")


def test_discover_walks_toolkit_project_outside_contract(tmp_path: Path) -> None:
    """A second Pkl project anywhere in the repo is walked when it uses the toolkit."""
    _write_repo(
        tmp_path,
        {
            "contract/app.pkl": 'amends "@app-contract-toolkit/App.pkl"\n',
            "docs/v1/providers/example/PklProject": _TOOLKIT_PKLPROJECT,
            "docs/v1/providers/example/app.pkl": (
                'amends "@app-contract-toolkit/NativeAppBundle.pkl"\n'
            ),
            "docs/v1/providers/example/sub/entry.pkl": "x = 1\n",
        },
    )
    rels = [p.relative_to(tmp_path).as_posix() for p in discover(tmp_path)]
    assert rels == [
        "contract/app.pkl",
        "docs/v1/providers/example/app.pkl",
        "docs/v1/providers/example/sub/entry.pkl",
    ]


def test_discover_detects_archived_package_uri(tmp_path: Path) -> None:
    """A dependency on the archived toolkit package URI also counts."""
    project = (
        'amends "pkl:Project"\ndependencies {\n  ["toolkit"] {\n'
        '    uri = "package://atlanhq.github.io/app-contract-toolkit/'
        'app-contract-toolkit@0.9.6"\n  }\n}\n'
    )
    _write_repo(tmp_path, {"ref/PklProject": project, "ref/app.pkl": "x = 1\n"})
    rels = [p.relative_to(tmp_path).as_posix() for p in discover(tmp_path)]
    assert rels == ["ref/app.pkl"]


def test_discover_skips_non_toolkit_project(tmp_path: Path) -> None:
    """A PklProject that does not depend on the toolkit is not walked."""
    project = 'amends "pkl:Project"\n// app-contract-toolkit is not used here\n'
    _write_repo(
        tmp_path,
        {
            "tools/PklProject": project,
            "tools/thing.pkl": 'amends "@app-contract-toolkit/NativeApp.pkl"\n',
        },
    )
    assert discover(tmp_path) == []


def test_discover_skips_toolkit_package_itself(tmp_path: Path) -> None:
    """The toolkit's own PklProject declares a package, not a dependency on it."""
    project = dedent("""\
        amends "pkl:Project"
        package {
          name = "app-contract-toolkit"
          baseUri = "package://atlanhq.github.io/application-sdk/contracts/\\(name)"
        }
    """)
    _write_repo(
        tmp_path,
        {"src/PklProject": project, "src/NativeApp.pkl": "open module NativeApp\n"},
    )
    assert discover(tmp_path) == []


def test_discover_skips_generated_and_hidden_dirs(tmp_path: Path) -> None:
    """``generated/``, hidden dirs and ``node_modules`` are never walked."""
    _write_repo(
        tmp_path,
        {
            "contract/PklProject": _TOOLKIT_PKLPROJECT,
            "contract/app.pkl": "x = 1\n",
            "contract/generated/input.pkl": "x = 1\n",
            ".venv/lib/PklProject": _TOOLKIT_PKLPROJECT,
            ".venv/lib/a.pkl": "x = 1\n",
            "node_modules/pkg/PklProject": _TOOLKIT_PKLPROJECT,
            "node_modules/pkg/b.pkl": "x = 1\n",
        },
    )
    rels = [p.relative_to(tmp_path).as_posix() for p in discover(tmp_path)]
    assert rels == ["contract/app.pkl"]


def test_discover_returns_empty_when_no_contract_dir(tmp_path: Path) -> None:
    """discover() returns [] when no contract/ directory exists."""
    assert discover(tmp_path) == []


def test_scan_path_reads_file(tmp_path: Path) -> None:
    """scan_path reads the file and delegates to scan_text."""
    p = tmp_path / "contract" / "app.pkl"
    p.parent.mkdir(parents=True)
    p.write_text('amends "@app-contract-toolkit/NativeApp.pkl"\n', encoding="utf-8")
    findings = scan_path(p, tmp_path)
    assert any(f.rule_id == "K001" for f in findings)


def test_scan_path_graceful_on_missing_file(tmp_path: Path) -> None:
    """scan_path returns [] for a non-existent file (never raises OSError)."""
    absent = tmp_path / "contract" / "app.pkl"
    # Do NOT create the directory or file
    findings = scan_path(absent, tmp_path)
    assert findings == []


# ---------------------------------------------------------------------------
# Rule-metadata assertions
# ---------------------------------------------------------------------------


def test_k001_rule_metadata() -> None:
    """K001 has the correct tier, scope, mechanism, and orthogonal_gate."""
    rule = get_rule("K001")
    assert rule.name == "ContractAmendsLegacyModule"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.orthogonal_gate == "pkl-eval"
    assert rule.help_uri == rule_help_uri("K001")


def test_k002_rule_metadata() -> None:
    """K002 has the correct tier, scope, mechanism, and orthogonal_gate."""
    rule = get_rule("K002")
    assert rule.name == "LegacyContractApi"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.orthogonal_gate == "pkl-eval"
    assert rule.help_uri == rule_help_uri("K002")
