"""Tests for K011 AppIdMissingFromContract, K012 GeneratePoeTaskMissing,
K014 ReleaseModelUndeclared and K028 ReleaseScheduleDeclared.

Both rules are cross-artifact release-readiness guards driven by ``scan_all``:

* K011 fires when a contract-driven app repo's generated ``atlan.yaml`` has no
  top-level ``app_id`` (the Global Marketplace identity the publish step POSTs).
* K012 fires when ``pyproject.toml`` defines no ``[tool.poe.tasks.generate]``
  task (the SDK Certify step runs ``uv run poe generate``).

Test helpers
------------
``_app_repo``: scaffolds a minimal app repo under ``tmp_path`` — a ``contract/``
dir (the discover guard), plus optional ``atlan.yaml`` / ``pyproject.toml``
bodies, an optional ``contract/app.pkl`` body, and optionally the generated
``app/generated/release_schedule.yaml`` — then returns the ``scan_all``
findings for it. ``scheduled=True`` is the K028-compliant shape: a contract
declaring a ``releaseSchedule`` plus its generated file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks.release_contract import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding

# A well-formed atlan.yaml fragment carrying the top-level app_id key.
_ATLAN_WITH_APP_ID = (
    "name: openapi\n"
    "app_id: 019d1f6b-6fea-7db3-96d8-e61e159d0351\n"
    "display_name: OpenAPI Spec Loader\n"
    "release_model: semver\n"
)
_ATLAN_WITHOUT_APP_ID = (
    "name: openapi\n" "display_name: OpenAPI Spec Loader\n" "release_model: semver\n"
)

_PYPROJECT_WITH_GENERATE = (
    "[tool.poe.tasks]\n"
    'start-deps = "echo none"\n'
    'generate = "pkl eval --project-dir contract -m . contract/app.pkl"\n'
)
_PYPROJECT_WITHOUT_GENERATE = '[tool.poe.tasks]\nstart-deps = "echo none"\n'


# A synthetic App.pkl contract with a release schedule, and the file
# contract-toolkit renders from it (the toolkit's exact output shape).
_APP_PKL_SCHEDULED = (
    'amends "@app-contract-toolkit/App.pkl"\n'
    "\n"
    'name = "x"\n'
    "metadata {\n"
    '  ["release_model"] = "semver"\n'
    "}\n"
    "releaseSchedule {\n"
    '  new { day = "mon"; slot = "12:00" }\n'
    "}\n"
)
_RELEASE_SCHEDULE_YAML = (
    "# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.\n"
    "# To regenerate: pkl eval -m . contract/app.pkl\n"
    "schema_version: 1\n"
    "timezone: IST   # fixed UTC+05:30\n"
    "releases:\n"
    "  - day: mon\n"
    '    slot: "12:00"\n'
)


def _app_repo(
    tmp_path: Path,
    *,
    contract: bool = True,
    atlan: str | None = None,
    pyproject: str | None = None,
    app_pkl: str | None = None,
    release_schedule: str | None = None,
    scheduled: bool = False,
) -> list[Finding]:
    """Scaffold a repo under *tmp_path* and return its ``scan_all`` findings."""
    if scheduled:
        app_pkl = _APP_PKL_SCHEDULED if app_pkl is None else app_pkl
        if release_schedule is None:
            release_schedule = _RELEASE_SCHEDULE_YAML
    if contract:
        (tmp_path / "contract").mkdir()
        (tmp_path / "contract" / "app.pkl").write_text(
            app_pkl if app_pkl is not None else 'name = "x"\n', encoding="utf-8"
        )
    if release_schedule is not None:
        generated = tmp_path / "app" / "generated"
        generated.mkdir(parents=True)
        (generated / "release_schedule.yaml").write_text(
            release_schedule, encoding="utf-8"
        )
    if atlan is not None:
        (tmp_path / "atlan.yaml").write_text(atlan, encoding="utf-8")
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return scan_all(discover(tmp_path), tmp_path)


def _ids(findings: list[Finding]) -> list[str]:
    """Rule IDs of unsuppressed findings (matching runner gate semantics)."""
    return [f.rule_id for f in findings if not f.suppressed]


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_k011_rule_metadata() -> None:
    rule = get_rule("K011")
    assert rule.name == "AppIdMissingFromContract"
    assert rule.tier is EnforcementTier.BLOCK
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


def test_k012_rule_metadata() -> None:
    rule = get_rule("K012")
    assert rule.name == "GeneratePoeTaskMissing"
    assert rule.tier is EnforcementTier.BLOCK
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


# ---------------------------------------------------------------------------
# discover — the "is this a contract-driven app repo?" guard
# ---------------------------------------------------------------------------


def test_discover_requires_contract_dir(tmp_path: Path) -> None:
    assert discover(tmp_path) == []  # no contract/ dir → not in scope
    (tmp_path / "contract").mkdir()
    assert discover(tmp_path) == [tmp_path]


def test_scan_all_noops_without_contract(tmp_path: Path) -> None:
    # Even with a broken atlan.yaml/pyproject present, no contract/ → no paths.
    (tmp_path / "atlan.yaml").write_text(_ATLAN_WITHOUT_APP_ID, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        _PYPROJECT_WITHOUT_GENERATE, encoding="utf-8"
    )
    assert scan_all(discover(tmp_path), tmp_path) == []


# ---------------------------------------------------------------------------
# K011 — app_id in atlan.yaml
# ---------------------------------------------------------------------------


def test_k011_fires_when_app_id_missing(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITHOUT_APP_ID)
    assert "K011" in _ids(findings)


def test_k011_silent_when_app_id_present(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITH_APP_ID)
    assert "K011" not in _ids(findings)


def test_k011_fires_when_top_level_app_id_is_absent(tmp_path: Path) -> None:
    atlan = "name: salesforce\nrelease_model: semver\n"
    findings = _app_repo(tmp_path, atlan=atlan)
    assert "K011" in _ids(findings)


def test_k011_silent_when_top_level_app_id_has_whitespace_before_colon(
    tmp_path: Path,
) -> None:
    atlan = (
        'app_id : "019e0353-57c9-7070-81d5-5a48921f1c9c"\n' "release_model: semver\n"
    )
    findings = _app_repo(tmp_path, atlan=atlan)
    assert "K011" not in _ids(findings)


def test_k011_anchored_in_atlan_yaml(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITHOUT_APP_ID)
    k011 = [f for f in findings if f.rule_id == "K011"]
    assert k011
    assert k011[0].file == "atlan.yaml"


def test_k011_silent_when_no_atlan_yaml(tmp_path: Path) -> None:
    # A missing generated output is K004's concern, not K011's — no double-flag.
    findings = _app_repo(tmp_path, atlan=None)
    assert "K011" not in _ids(findings)


def test_k011_nested_app_id_does_not_satisfy(tmp_path: Path) -> None:
    # An indented app_id under some other mapping is not the top-level identity.
    nested = "name: openapi\nmetadata:\n  app_id: nested\nrelease_model: semver\n"
    findings = _app_repo(tmp_path, atlan=nested)
    assert "K011" in _ids(findings)


def test_k011_fires_on_empty_or_null_value(tmp_path: Path) -> None:
    # A present app_id set to empty-quotes or a YAML null literal still POSTs an
    # empty/None identity and hits the same 404 — treat it as missing.
    for i, empty in enumerate(
        ('app_id: ""', "app_id: ''", "app_id: null", "app_id: ~")
    ):
        sub = tmp_path / f"case{i}"
        sub.mkdir()
        body = f"name: openapi\n{empty}\nrelease_model: semver\n"
        findings = _app_repo(sub, atlan=body)
        assert "K011" in _ids(findings), empty


def test_k011_suppressed_by_directive(tmp_path: Path) -> None:
    body = "# conformance: ignore[K011] non-published app\n" + _ATLAN_WITHOUT_APP_ID
    findings = _app_repo(tmp_path, atlan=body)
    k011 = [f for f in findings if f.rule_id == "K011"]
    assert k011
    assert k011[0].suppressed is True
    assert "K011" not in _ids(findings)


# ---------------------------------------------------------------------------
# K012 — generate poe task in pyproject.toml
# ---------------------------------------------------------------------------


def test_k012_fires_when_generate_task_missing(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, pyproject=_PYPROJECT_WITHOUT_GENERATE)
    assert "K012" in _ids(findings)


def test_k012_silent_when_generate_task_present(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, pyproject=_PYPROJECT_WITH_GENERATE)
    assert "K012" not in _ids(findings)


def test_k012_fires_when_no_poe_tasks_table(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, pyproject='[project]\nname = "openapi"\n')
    assert "K012" in _ids(findings)


def test_k012_anchored_on_poe_tasks_header(tmp_path: Path) -> None:
    body = '[project]\nname = "x"\n\n[tool.poe.tasks]\nstart-deps = "echo none"\n'
    findings = _app_repo(tmp_path, pyproject=body)
    k012 = [f for f in findings if f.rule_id == "K012"]
    assert k012
    assert k012[0].file == "pyproject.toml"
    assert k012[0].line == 4  # the [tool.poe.tasks] header line


def test_k012_fires_on_empty_generate_command(tmp_path: Path) -> None:
    # A present but empty generate task is not a runnable target — 'uv run poe
    # generate' still fails, so K012 must fire.
    body = '[tool.poe.tasks]\nstart-deps = "echo none"\ngenerate = ""\n'
    findings = _app_repo(tmp_path, pyproject=body)
    assert "K012" in _ids(findings)


def test_k012_fires_on_malformed_pyproject(tmp_path: Path) -> None:
    # A malformed pyproject.toml is treated as "no tasks declared": the check
    # neither crashes nor false-negatives, so K012 fires.
    findings = _app_repo(tmp_path, pyproject="[tool.poe.tasks\nbroken = \n")
    assert "K012" in _ids(findings)


def test_k012_silent_when_no_pyproject(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, pyproject=None)
    assert "K012" not in _ids(findings)


def test_k012_suppressed_by_directive(tmp_path: Path) -> None:
    body = "# conformance: ignore[K012] never published\n" + _PYPROJECT_WITHOUT_GENERATE
    findings = _app_repo(tmp_path, pyproject=body)
    k012 = [f for f in findings if f.rule_id == "K012"]
    assert k012
    assert k012[0].suppressed is True
    assert "K012" not in _ids(findings)


# ---------------------------------------------------------------------------
# Both together — the exact regression that broke the openapi releases
# ---------------------------------------------------------------------------


def test_both_fire_on_fully_broken_release_setup(tmp_path: Path) -> None:
    findings = _app_repo(
        tmp_path,
        atlan=_ATLAN_WITHOUT_APP_ID,
        pyproject=_PYPROJECT_WITHOUT_GENERATE,
        scheduled=True,
    )
    assert set(_ids(findings)) == {"K011", "K012"}


def test_clean_setup_produces_no_findings(tmp_path: Path) -> None:
    findings = _app_repo(
        tmp_path,
        atlan=_ATLAN_WITH_APP_ID,
        pyproject=_PYPROJECT_WITH_GENERATE,
        scheduled=True,
    )
    assert _ids(findings) == []


# ---------------------------------------------------------------------------
# K014 — release_model must be a declared, valid choice
# ---------------------------------------------------------------------------


def test_k014_rule_metadata() -> None:
    rule = get_rule("K014")
    assert rule.name == "ReleaseModelUndeclared"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


def test_k014_fires_when_release_model_absent(tmp_path: Path) -> None:
    """The fleet-wide default case: no key at all, so the app inherits 'cd'."""
    findings = _app_repo(tmp_path, atlan="name: openapi\napp_id: abc\n")
    assert "K014" in _ids(findings)


def test_k014_silent_on_explicit_semver(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITH_APP_ID)
    assert "K014" not in _ids(findings)


def test_k014_silent_on_explicit_cd(tmp_path: Path) -> None:
    """The rule wants a *declared* choice, not a particular one."""
    findings = _app_repo(
        tmp_path, atlan="name: openapi\napp_id: abc\nrelease_model: cd\n"
    )
    assert "K014" not in _ids(findings)


def test_k014_fires_on_bare_key(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan="name: openapi\napp_id: abc\nrelease_model:\n")
    assert "K014" in _ids(findings)


@pytest.mark.parametrize("null", ["null", "Null", "NULL", "~", '""', "''"])
def test_k014_fires_on_null_value(tmp_path: Path, null: str) -> None:
    findings = _app_repo(
        tmp_path, atlan=f"name: openapi\napp_id: abc\nrelease_model: {null}\n"
    )
    assert "K014" in _ids(findings)


def test_k014_fires_on_invalid_value(tmp_path: Path) -> None:
    findings = _app_repo(
        tmp_path, atlan="name: openapi\napp_id: abc\nrelease_model: rolling\n"
    )
    k014 = [f for f in findings if f.rule_id == "K014"]
    assert k014
    # The fix hint must not advertise the deprecated alias.
    assert "versioned" not in k014[0].message


def test_k014_fires_on_deprecated_versioned_alias(tmp_path: Path) -> None:
    findings = _app_repo(
        tmp_path, atlan="name: openapi\napp_id: abc\nrelease_model: versioned\n"
    )
    k014 = [f for f in findings if f.rule_id == "K014"]
    assert k014
    assert "deprecated" in k014[0].message


def test_k014_accepts_quoted_and_commented_value(tmp_path: Path) -> None:
    findings = _app_repo(
        tmp_path,
        atlan='name: openapi\napp_id: abc\nrelease_model: "semver"  # deliberate\n',
    )
    assert "K014" not in _ids(findings)


def test_k014_nested_release_model_does_not_satisfy(tmp_path: Path) -> None:
    """An indented key is not the manifest-level value the publish step reads.

    An indentation-blind match would report this app as compliant while the
    publish step still falls back to 'cd'.
    """
    findings = _app_repo(
        tmp_path,
        atlan="name: openapi\napp_id: abc\ndeploy:\n  release_model: semver\n",
    )
    assert "K014" in _ids(findings)


def test_k014_anchored_in_atlan_yaml(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan="name: openapi\napp_id: abc\n")
    k014 = [f for f in findings if f.rule_id == "K014"]
    assert k014
    assert k014[0].file == "atlan.yaml"


def test_k014_silent_when_no_atlan_yaml(tmp_path: Path) -> None:
    """A missing atlan.yaml is K004's regeneration concern, not this rule's."""
    findings = _app_repo(tmp_path, atlan=None)
    assert "K014" not in _ids(findings)


def test_k014_suppressed_by_directive(tmp_path: Path) -> None:
    body = "# conformance: ignore[K014] not published\nname: openapi\napp_id: abc\n"
    findings = _app_repo(tmp_path, atlan=body)
    k014 = [f for f in findings if f.rule_id == "K014"]
    assert k014
    assert k014[0].suppressed is True
    assert "K014" not in _ids(findings)


# ---------------------------------------------------------------------------
# K028 — a semver app declares its release-train schedule
# ---------------------------------------------------------------------------


def _k028(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.rule_id == "K028"]


def test_k028_rule_metadata() -> None:
    rule = get_rule("K028")
    assert rule.name == "ReleaseScheduleDeclared"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"
    # The owner picks the days and slot; no default can be applied for them.
    assert rule.autofixable is False
    assert "docs/standards/release-flow.md" in rule.full_description
    assert "releaseSchedule {" in rule.full_description


def test_k028_silent_when_declared_and_generated(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITH_APP_ID, scheduled=True)
    assert _k028(findings) == []


def test_k028_fires_when_block_missing(tmp_path: Path) -> None:
    findings = _app_repo(
        tmp_path,
        atlan=_ATLAN_WITH_APP_ID,
        app_pkl='amends "@app-contract-toolkit/App.pkl"\n\nname = "x"\n',
    )
    k028 = _k028(findings)
    assert len(k028) == 1
    assert k028[0].file == "contract/app.pkl"
    assert k028[0].line == 1  # the amends line
    assert "declares no releaseSchedule" in k028[0].message
    assert "K028" in _ids(findings)


def test_k028_fires_when_generated_file_missing(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITH_APP_ID, app_pkl=_APP_PKL_SCHEDULED)
    k028 = _k028(findings)
    assert len(k028) == 1
    assert "release_schedule.yaml is missing" in k028[0].message
    # Anchored on the declaration, which is where the regenerate starts.
    assert k028[0].line == 7


def test_k028_fires_when_block_missing_even_if_file_present(tmp_path: Path) -> None:
    """A stale generated file does not stand in for the declaration."""
    findings = _app_repo(
        tmp_path,
        atlan=_ATLAN_WITH_APP_ID,
        app_pkl='name = "x"\n',
        release_schedule=_RELEASE_SCHEDULE_YAML,
    )
    assert "declares no releaseSchedule" in _k028(findings)[0].message


@pytest.mark.parametrize(
    "atlan",
    [
        "name: openapi\napp_id: abc\nrelease_model: cd\n",
        "name: openapi\napp_id: abc\n",  # missing → cd (K014's finding)
        "name: openapi\napp_id: abc\nrelease_model:\n",
        "name: openapi\napp_id: abc\nrelease_model: rolling\n",
        "name: openapi\napp_id: abc\ndeploy:\n  release_model: semver\n",
    ],
)
def test_k028_not_applicable_to_non_semver_app(tmp_path: Path, atlan: str) -> None:
    findings = _app_repo(tmp_path, atlan=atlan)
    assert _k028(findings) == []


def test_k028_applies_to_deprecated_versioned_alias(tmp_path: Path) -> None:
    findings = _app_repo(
        tmp_path, atlan="name: openapi\napp_id: abc\nrelease_model: versioned\n"
    )
    assert "K028" in _ids(findings)


def test_k028_applies_to_quoted_and_commented_semver(tmp_path: Path) -> None:
    findings = _app_repo(
        tmp_path,
        atlan='name: openapi\napp_id: abc\nrelease_model: "semver"  # train\n',
    )
    assert "K028" in _ids(findings)


def test_k028_silent_when_no_atlan_yaml(tmp_path: Path) -> None:
    """No atlan.yaml means no readable release model — K004's concern."""
    findings = _app_repo(tmp_path, atlan=None)
    assert _k028(findings) == []


def test_k028_silent_without_contract_app_pkl(tmp_path: Path) -> None:
    (tmp_path / "contract").mkdir()
    (tmp_path / "atlan.yaml").write_text(_ATLAN_WITH_APP_ID, encoding="utf-8")
    assert _k028(scan_all(discover(tmp_path), tmp_path)) == []


@pytest.mark.parametrize(
    "decl",
    [
        "// releaseSchedule { }\n",
        "/// releaseSchedule is declared below — it is not\n",
        "myReleaseSchedule {\n}\n",
    ],
)
def test_k028_comment_or_lookalike_does_not_count(tmp_path: Path, decl: str) -> None:
    findings = _app_repo(
        tmp_path, atlan=_ATLAN_WITH_APP_ID, app_pkl='name = "x"\n' + decl
    )
    assert "declares no releaseSchedule" in _k028(findings)[0].message


def test_k028_accepts_assignment_form(tmp_path: Path) -> None:
    app_pkl = (
        'name = "x"\n'
        "releaseSchedule = new Listing {\n"
        '  new ReleaseSlot { day = "tue"; slot = "09:00" }\n'
        "}\n"
    )
    findings = _app_repo(
        tmp_path,
        atlan=_ATLAN_WITH_APP_ID,
        app_pkl=app_pkl,
        release_schedule=_RELEASE_SCHEDULE_YAML,
    )
    assert _k028(findings) == []


def test_k028_points_a_legacy_contract_at_k001(tmp_path: Path) -> None:
    """NativeApp.pkl has no releaseSchedule, so a toolkit bump cannot fix it."""
    findings = _app_repo(
        tmp_path,
        atlan=_ATLAN_WITH_APP_ID,
        app_pkl='amends "@app-contract-toolkit/NativeApp.pkl"\nname = "x"\n',
    )
    assert "migrate it to App.pkl first (K001)" in _k028(findings)[0].message


def test_k028_asks_for_contract_release_model_when_absent(tmp_path: Path) -> None:
    """semver only in a hand-owned atlan.yaml: App.pkl would refuse the schedule."""
    findings = _app_repo(
        tmp_path,
        atlan=_ATLAN_WITH_APP_ID,
        app_pkl='amends "@app-contract-toolkit/App.pkl"\nname = "x"\n',
    )
    assert 'declare metadata { ["release_model"] = "semver" }' in (
        _k028(findings)[0].message
    )


def test_k028_no_release_model_hint_when_contract_declares_it(tmp_path: Path) -> None:
    app_pkl = (
        'amends "@app-contract-toolkit/App.pkl"\n'
        'name = "x"\n'
        'metadata { ["release_model"] = "semver" }\n'
    )
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITH_APP_ID, app_pkl=app_pkl)
    message = _k028(findings)[0].message
    assert "declares no releaseSchedule" in message
    assert "Also declare metadata" not in message
    assert "K001" not in message


def test_k028_suppressed_on_amends_line(tmp_path: Path) -> None:
    app_pkl = (
        "// conformance: ignore[K028] kept off the train while the app is sunset\n"
        'amends "@app-contract-toolkit/App.pkl"\n'
        'name = "x"\n'
    )
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITH_APP_ID, app_pkl=app_pkl)
    k028 = _k028(findings)
    assert k028 and k028[0].suppressed is True
    assert "K028" not in _ids(findings)


# ---------------------------------------------------------------------------
# K028 mutation check — a synthetic repo shaped like a canonical App.pkl app
# ---------------------------------------------------------------------------
#
# The generated file comes from the toolkit's own release-schedule example when
# the SDK checkout sits alongside the package, so it is the toolkit's real output
# rather than a hand-copied one. Each mutation removes exactly one thing the rule
# reads, and the rule must notice every one — a clean result on the compliant
# repo proves nothing on its own.


def _toolkit_example() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        example = parent / "contract-toolkit" / "examples" / "release-schedule"
        if (example / "app.pkl").is_file():
            return example
    return None


def _canonical_shaped_repo(tmp_path: Path) -> Path:
    """A synthetic semver app: atlan.yaml, PklProject, an App.pkl contract with
    a releaseSchedule, and the generated release_schedule.yaml."""
    example = _toolkit_example()
    if example is not None:
        schedule = (example / "app/generated/release_schedule.yaml").read_text(
            encoding="utf-8"
        )
    else:
        schedule = _RELEASE_SCHEDULE_YAML
    (tmp_path / "contract").mkdir()
    (tmp_path / "contract" / "PklProject").write_text(
        'amends "pkl:Project"\n', encoding="utf-8"
    )
    (tmp_path / "contract" / "app.pkl").write_text(
        'amends "@app-contract-toolkit/App.pkl"\n'
        "\n"
        'name = "example-semver-app"\n'
        'displayName = "Example Semver App"\n'
        "\n"
        "metadata {\n"
        '  ["release_model"] = "semver"\n'
        '  ["app_id"] = "00000000-0000-0000-0000-000000000000"\n'
        "}\n"
        "\n"
        "releaseSchedule {\n"
        '  new { day = "mon"; slot = "12:00" }\n'
        '  new { day = "wed"; slot = "18:00" }\n'
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "atlan.yaml").write_text(
        "# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.\n"
        "# To regenerate: pkl eval -m . contract/app.pkl\n"
        "name: example-semver-app\n"
        "display_name: Example Semver App\n"
        "release_model: semver\n"
        "app_id: 00000000-0000-0000-0000-000000000000\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT_WITH_GENERATE, encoding="utf-8")
    generated = tmp_path / "app" / "generated"
    generated.mkdir(parents=True)
    (generated / "release_schedule.yaml").write_text(schedule, encoding="utf-8")
    return tmp_path


def _scan(root: Path) -> list[str]:
    return _ids(scan_all(discover(root), root))


def test_k028_example_output_is_the_shape_the_fixtures_assume() -> None:
    """The toolkit example's generated file matches the synthetic fixture shape."""
    example = _toolkit_example()
    if example is None:
        pytest.skip("contract-toolkit/examples/release-schedule not alongside package")
    text = (example / "app/generated/release_schedule.yaml").read_text(encoding="utf-8")
    assert text.startswith(_RELEASE_SCHEDULE_YAML.split("releases:\n")[0])
    assert 'releases:\n  - day: mon\n    slot: "12:00"\n' in text


def test_k028_mutations_are_each_detected(tmp_path: Path) -> None:
    root = _canonical_shaped_repo(tmp_path)
    assert _scan(root) == [], "the compliant canonical-shaped repo must be clean"

    app_pkl = root / "contract" / "app.pkl"
    schedule = root / "app" / "generated" / "release_schedule.yaml"
    atlan = root / "atlan.yaml"
    original_pkl = app_pkl.read_text(encoding="utf-8")
    original_schedule = schedule.read_text(encoding="utf-8")
    original_atlan = atlan.read_text(encoding="utf-8")

    # Mutation 1: drop the releaseSchedule block.
    start = original_pkl.index("releaseSchedule {")
    end = original_pkl.index("}\n", original_pkl.index("wed")) + 2
    app_pkl.write_text(original_pkl[:start] + original_pkl[end:], encoding="utf-8")
    assert _scan(root) == ["K028"]
    app_pkl.write_text(original_pkl, encoding="utf-8")

    # Mutation 2: delete the generated file.
    schedule.unlink()
    assert _scan(root) == ["K028"]
    schedule.write_text(original_schedule, encoding="utf-8")

    # Mutation 3: switch the app to cd — the rule no longer applies, even with
    # the block and the file removed.
    atlan.write_text(
        original_atlan.replace("release_model: semver", "release_model: cd"),
        encoding="utf-8",
    )
    schedule.unlink()
    app_pkl.write_text('amends "@app-contract-toolkit/App.pkl"\n', encoding="utf-8")
    assert "K028" not in _scan(root)

    # Restored, it is clean again.
    atlan.write_text(original_atlan, encoding="utf-8")
    app_pkl.write_text(original_pkl, encoding="utf-8")
    schedule.write_text(original_schedule, encoding="utf-8")
    assert _scan(root) == []
