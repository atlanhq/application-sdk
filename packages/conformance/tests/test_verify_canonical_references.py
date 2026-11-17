"""Tests for ``conformance.tools.verify_canonical_references`` (FND-4665).

The tool runs on a schedule against live checkouts of the three reference
apps; here each app is a small tree under ``tmp_path`` so every way a reference
can go stale is pinned without the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conformance.suite.schema import load_catalog
from conformance.tools import verify_canonical_references as vcr


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for name in (*vcr.REFERENCE_APPS, vcr.SDK):
        out[name] = tmp_path / name
        out[name].mkdir()
    mysql = out["atlan-mysql-app"]
    _write(mysql, "app/handler.py", "def fetch_metadata():\n    pass\n" * 3)
    _write(mysql, "app/client.py", "class SQLClient:\n    pass\n")
    _write(mysql, "pyproject.toml", "[project]\n")
    _write(mysql, "tests/unit/test_client.py", "def test_x():\n    assert 1\n")
    _write(
        out["atlan-metabase-app"], "app/credentials.py", "def parse_creds():\n    ...\n"
    )
    sdk = out[vcr.SDK]
    _write(sdk, "application_sdk/__init__.py")
    _write(sdk, "application_sdk/clients/__init__.py")
    _write(sdk, "application_sdk/clients/sql.py", "class BaseSQLClient:\n    ...\n")
    return out


def _failures(
    reference: str, roots: dict[str, Path], rule_id: str = "X001"
) -> list[str]:
    _, failures = vcr.verify_reference(rule_id, reference, roots)
    return [f"{f.claim.kind}:{f.claim.value}" for f in failures]


def _claims(reference: str, roots: dict[str, Path]) -> list[str]:
    claims, _ = vcr.verify_reference("X001", reference, roots)
    return [f"{c.repo}:{c.kind}:{c.value}" for c in claims]


# ── Paths ────────────────────────────────────────────────────────────────────


def test_existing_path_and_symbol_pass(roots: dict[str, Path]) -> None:
    ref = "atlan-mysql-app app/handler.py — `fetch_metadata` re-raises."
    assert _failures(ref, roots) == []
    assert _claims(ref, roots) == [
        "atlan-mysql-app:path:app/handler.py",
        "atlan-mysql-app:symbol:fetch_metadata",
    ]


def test_missing_path_fails(roots: dict[str, Path]) -> None:
    ref = "atlan-mysql-app app/failures.py — every leaf subclasses an SDK category."
    assert _failures(ref, roots) == ["path:app/failures.py"]


def test_path_is_attributed_to_the_nearest_preceding_repo(
    roots: dict[str, Path],
) -> None:
    # app/credentials.py exists only in metabase, app/client.py only in mysql.
    ref = (
        "atlan-metabase-app app/credentials.py — parses. atlan-mysql-app "
        "app/client.py shows the other shape."
    )
    assert _failures(ref, roots) == []
    swapped = "atlan-mysql-app app/credentials.py; atlan-metabase-app app/client.py."
    assert _failures(swapped, roots) == [
        "path:app/credentials.py",
        "path:app/client.py",
    ]


def test_pr_reference_does_not_switch_repo(roots: dict[str, Path]) -> None:
    ref = "atlan-metabase-app app/credentials.py — as atlan-mysql-app#729 did; app/credentials.py."
    assert _failures(ref, roots) == []


def test_file_resolves_under_a_directory_the_reference_cited(
    roots: dict[str, Path],
) -> None:
    ref = "atlan-mysql-app tests/unit/ — unit modules covering the client (test_client.py)."
    assert _failures(ref, roots) == []


def test_bare_filename_after_a_cited_directory_must_be_in_it(
    roots: dict[str, Path],
) -> None:
    mysql = roots["atlan-mysql-app"]
    ref = "atlan-mysql-app tests/unit/ — the client tests (test_client.py)."
    assert _failures(ref, roots) == []
    # The directory loses the file; a root-level homonym must not answer for it.
    (mysql / "tests/unit/test_client.py").unlink()
    _write(mysql, "test_client.py", "def test_x():\n    assert 1\n")
    assert _failures(ref, roots) == ["path:test_client.py"]


def test_bare_filename_resolves_anywhere_in_the_repo(roots: dict[str, Path]) -> None:
    assert _failures("atlan-mysql-app — see test_client.py.", roots) == []
    assert _failures("atlan-mysql-app — see test_gone.py.", roots) == [
        "path:test_gone.py"
    ]


def test_sdk_path_resolves_against_the_sdk_in_any_segment(
    roots: dict[str, Path],
) -> None:
    ref = "atlan-mysql-app app/client.py — the seam is application_sdk/clients/sql.py."
    assert (
        _claims(ref, roots)[1] == "application_sdk:path:application_sdk/clients/sql.py"
    )
    assert _failures(ref, roots) == []
    gone = "No reference app does this; the seam is application_sdk/clients/gone.py."
    assert _failures(gone, roots) == ["path:application_sdk/clients/gone.py"]


@pytest.mark.parametrize(
    "prose",
    [
        "try/except",
        "Input/Output",
        "include/exclude",
        "I/O",
        "registry.atlan.com/public/app-runtime-base:3",
        "`.venv/`",
        "`tests/sdr/`",
        "`.../generated/<ep>/manifest.json`",
        "tests/**",
        "residual/",
        "atlanhq/application-sdk/.github/workflows/checks-reusable.yaml@main",
        "application_sdk.clients.sql",
    ],
)
def test_prose_and_literals_are_not_path_claims(
    prose: str, roots: dict[str, Path]
) -> None:
    ref = f"atlan-mysql-app pyproject.toml — {prose} is mentioned here."
    paths = [c for c in _claims(ref, roots) if ":path:" in c]
    assert paths == ["atlan-mysql-app:path:pyproject.toml"]


def test_directory_needs_its_trailing_slash(roots: dict[str, Path]) -> None:
    ref = "atlan-mysql-app tests/unit/ and tests/unit/. and tests/unit"
    paths = [c for c in _claims(ref, roots) if ":path:" in c]
    assert paths == [
        "atlan-mysql-app:path:tests/unit/",
        "atlan-mysql-app:path:tests/unit/",
    ]


def test_path_named_before_any_repo_is_not_a_claim(roots: dict[str, Path]) -> None:
    assert _claims("No reference app has main.py doing this.", roots) == []


# ── Line claims ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "span,stale",
    [(":6", False), (":2-6", False), (":7", True), (":4-9", True), (":0", True)],
)
def test_line_claim_must_fall_inside_the_file(
    span: str, stale: bool, roots: dict[str, Path]
) -> None:
    ref = f"atlan-mysql-app app/handler.py{span} — the re-raise."
    expected = [f"lines:app/handler.py{span}"] if stale else []
    assert _failures(ref, roots) == expected


# ── Symbols ──────────────────────────────────────────────────────────────────


def test_renamed_symbol_fails_although_the_file_remains(roots: dict[str, Path]) -> None:
    # The FND-4665 failure mode: metabase dropped build_credential_ref but kept
    # app/credentials.py, so a path-only check stayed green.
    ref = "atlan-metabase-app app/credentials.py — `build_credential_ref` routes."
    assert _failures(ref, roots) == ["symbol:build_credential_ref"]


def test_symbol_may_live_in_any_cited_file(roots: dict[str, Path]) -> None:
    ref = (
        "atlan-mysql-app app/handler.py — `SQLClient` is built in app/client.py, "
        "and `fetch_metadata` uses it."
    )
    assert _failures(ref, roots) == []


@pytest.mark.parametrize(
    "span",
    [
        "`fetch_metadata()`",
        "`fetch_metadata(...)`",
        "`self.fetch_metadata`",
        "`None`",
        "`pass`",
    ],
)
def test_symbol_forms_that_pass(span: str, roots: dict[str, Path]) -> None:
    assert _failures(f"atlan-mysql-app app/handler.py — {span}.", roots) == []


def test_non_identifier_code_spans_are_not_symbols(roots: dict[str, Path]) -> None:
    ref = 'atlan-mysql-app pyproject.toml — `requires-python = ">=3.11"`, `[sql]`.'
    assert [c for c in _claims(ref, roots) if ":symbol:" in c] == []


def test_no_symbol_claims_without_a_cited_file(roots: dict[str, Path]) -> None:
    assert _claims("No reference app calls `gather`.", roots) == []


# ── SDK modules ──────────────────────────────────────────────────────────────


def test_sdk_module_and_attribute(roots: dict[str, Path]) -> None:
    assert (
        _failures("Through ``application_sdk.clients.sql.BaseSQLClient``.", roots) == []
    )
    assert _failures("Through application_sdk.clients.sql.Gone.", roots) == [
        "module:application_sdk.clients.sql.Gone"
    ]
    assert _failures("Through application_sdk.clients.nosql.", roots) == [
        "module:application_sdk.clients.nosql"
    ]
    assert _failures("`application_sdk.__file__`", roots) == []


# ── NOT_CHECKED / NAMED_ABSENT ────────────────────────────────────────────────


def test_not_checked_skips_only_for_its_own_rule(roots: dict[str, Path]) -> None:
    ref = "atlan-openapi-app app/connector.py — writes openapi_metadata.json."
    _write(roots["atlan-openapi-app"], "app/connector.py")
    assert _failures(ref, roots, rule_id="K017") == []
    assert _failures(ref, roots, rule_id="K018") == ["path:openapi_metadata.json"]


@pytest.mark.parametrize(
    "key", [*vcr.NOT_CHECKED, *vcr.NAMED_ABSENT], ids=lambda k: f"{k[0]}:{k[1]}"
)
def test_every_exemption_names_a_token_its_rule_still_cites(
    key: tuple[str, str],
) -> None:
    """An entry whose rule no longer says the token is a dead exemption."""
    references = {r.id: r.canonical_reference for r in load_catalog()}
    assert key[1] in references.get(key[0], ""), f"{key} is no longer cited"


def test_every_named_absence_is_checked() -> None:
    """Each NAMED_ABSENT entry turns into an inverted claim, not a skip."""
    roots = {name: Path("/nonexistent") for name in (*vcr.REFERENCE_APPS, vcr.SDK)}
    references = dict(vcr._catalog_references())
    for rule_id, token in vcr.NAMED_ABSENT:
        claims, _ = vcr.verify_reference(rule_id, references[rule_id], roots)
        kinds = {c.kind for c in claims if c.value == token}
        assert kinds and all(k.startswith("absent-") for k in kinds), (rule_id, token)


def test_absent_path_fails_once_it_appears(roots: dict[str, Path]) -> None:
    # T013 says none of the three has a tests/sdr/; metabase gaining one makes
    # the sentence false wherever the reference attributes it.
    ref = (
        "atlan-metabase-app tests/ — everything sits under unit/. None of the "
        "three reference apps has a tests/sdr/ or a tests/full_dag/."
    )
    _write(roots["atlan-metabase-app"], "tests/unit/test_x.py")
    assert _failures(ref, roots, rule_id="T013") == []
    _write(roots["atlan-mysql-app"], "tests/sdr/test_y.py")
    assert _failures(ref, roots, rule_id="T013") == ["absent-path:tests/sdr/"]


def test_absent_path_defaults_to_the_attributed_repo(roots: dict[str, Path]) -> None:
    ref = "atlan-mysql-app pyproject.toml — declared here rather than a uv.toml."
    _write(roots["atlan-openapi-app"], "uv.toml")
    assert _failures(ref, roots, rule_id="D012") == []
    _write(roots["atlan-mysql-app"], "uv.toml")
    assert _failures(ref, roots, rule_id="D012") == ["absent-path:uv.toml"]


def test_absent_symbol_fails_once_a_cited_file_has_it(roots: dict[str, Path]) -> None:
    ref = "atlan-metabase-app app/contracts.py — no field holds a `json.dumps` string."
    _write(roots["atlan-metabase-app"], "app/contracts.py", "orjson.dumps(x)\n")
    assert _failures(ref, roots, rule_id="P059") == []
    _write(roots["atlan-metabase-app"], "app/contracts.py", "json.dumps(x)\n")
    assert _failures(ref, roots, rule_id="P059") == ["absent-symbol:json.dumps"]


def test_absent_module_fails_once_it_is_restored(roots: dict[str, Path]) -> None:
    ref = "atlan-openapi-app pyproject.toml — never imports application_sdk.test_utils."
    _write(roots["atlan-openapi-app"], "pyproject.toml")
    assert _failures(ref, roots, rule_id="B009") == []
    _write(roots[vcr.SDK], "application_sdk/test_utils/__init__.py")
    assert _failures(ref, roots, rule_id="B009") == [
        "absent-module:application_sdk.test_utils"
    ]


# ── CLI ──────────────────────────────────────────────────────────────────────


def _argv(roots: dict[str, Path]) -> list[str]:
    argv = ["--sdk-root", str(roots[vcr.SDK])]
    for app in vcr.REFERENCE_APPS:
        argv += ["--app", f"{app}={roots[app]}"]
    return argv


def test_main_exit_codes(
    roots: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean = [("X001", "atlan-mysql-app app/handler.py — `fetch_metadata`.")]
    monkeypatch.setattr(vcr, "_catalog_references", lambda: clean)
    assert vcr.main(_argv(roots)) == 0

    stale = [("X002", "atlan-mysql-app app/gone.py — gone.")]
    monkeypatch.setattr(vcr, "_catalog_references", lambda: stale)
    assert vcr.main(_argv(roots)) == 1
    assert (
        "X002: atlan-mysql-app path 'app/gone.py' — does not exist"
        in capsys.readouterr().out
    )

    assert vcr.main(_argv(roots)[:2]) == 2


def test_main_rejects_an_unknown_app(roots: dict[str, Path]) -> None:
    with pytest.raises(SystemExit):
        vcr.main([*_argv(roots), "--app", "atlan-hello-world-app=/tmp"])


def test_every_catalog_reference_yields_a_claim() -> None:
    """A reference the parser finds nothing in is a reference nothing checks."""
    roots = {name: Path("/nonexistent") for name in (*vcr.REFERENCE_APPS, vcr.SDK)}
    silent = [
        rule_id
        for rule_id, reference in vcr._catalog_references()
        if not vcr.verify_reference(rule_id, reference, roots)[0]
    ]
    assert not silent, f"canonical_reference with no checkable claim: {silent}"
