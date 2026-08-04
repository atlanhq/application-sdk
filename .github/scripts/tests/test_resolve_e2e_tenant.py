"""Tests for .github/scripts/resolve_e2e_tenant.py."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SCRIPTS_DIR.parents[1]
_MODULE_PATH = _SCRIPTS_DIR / "resolve_e2e_tenant.py"

# Plain sys.path insertion rather than importlib: the module under test itself
# does `from export_extra_env import …`, so its sibling directory has to be
# importable anyway — which is exactly what the runner gives it (sys.path[0] is
# the script's own directory).
sys.path.insert(0, str(_SCRIPTS_DIR))

from resolve_e2e_tenant import TenantMatrixError, main, resolve  # noqa: E402

_MATRIX = {
    "aws": {
        "tenant": "e2e-aws-main.example.com",
        "client_id": "aws-client",
        "client_secret": "aws-secret",
        "api_key": "aws-key",
    },
    "azure": {
        "tenant": "e2e-azure-main.example.com",
        "client_id": "azure-client",
        "client_secret": "azure-secret",
        "api_key": "azure-key",
    },
    "gcp": {
        "tenant": "e2e-gcp-main1.example.com",
        "client_id": "gcp-client",
        "client_secret": "gcp-secret",
        "api_key": "gcp-key",
        "deployment_name": "staging",
    },
}

_FALLBACK = {
    "SDR_TEST_TENANT": "legacy.example.com",
    "SDR_CLIENT_ID": "legacy-client",
    "SDR_CLIENT_SECRET": "legacy-secret",
    "ATLAN_API_KEY": "legacy-key",
}


def _json(matrix: object = None) -> str:
    return json.dumps(_MATRIX if matrix is None else matrix)


def _parse_env(rendered: str) -> dict[str, str]:
    """Parse the ``NAME<<delim\\nvalue\\ndelim`` heredoc blocks back to a dict."""
    out: dict[str, str] = {}
    lines = rendered.split("\n")
    i = 0
    while i < len(lines):
        if "<<" not in lines[i]:
            i += 1
            continue
        name, delimiter = lines[i].split("<<", 1)
        j = i + 1
        value: list[str] = []
        while lines[j] != delimiter:
            value.append(lines[j])
            j += 1
        out[name] = "\n".join(value)
        i = j + 1
    return out


# ── resolve() ────────────────────────────────────────────────────────────────


def test_selects_the_requested_cloud() -> None:
    assert resolve(_json(), "azure") == {
        "SDR_TEST_TENANT": "e2e-azure-main.example.com",
        "SDR_CLIENT_ID": "azure-client",
        "SDR_CLIENT_SECRET": "azure-secret",
        "ATLAN_API_KEY": "azure-key",
        "ATLAN_BASE_URL": "https://e2e-azure-main.example.com",
    }


def test_other_clouds_credentials_never_appear() -> None:
    """Least privilege: a leg must not carry the other tenants' credentials."""
    rendered = json.dumps(resolve(_json(), "aws"))
    for cloud in ("azure", "gcp"):
        for value in _MATRIX[cloud].values():
            assert value not in rendered


def test_base_url_is_derived_from_the_resolved_tenant() -> None:
    # Never configured separately: ATLAN_BASE_URL cannot name a different tenant
    # than SDR_TEST_TENANT, which is what the pre-FND-6 env: block guaranteed.
    for cloud in _MATRIX:
        env = resolve(_json(), cloud)
        assert env["ATLAN_BASE_URL"] == f"https://{env['SDR_TEST_TENANT']}"


def test_optional_deployment_name_is_emitted_only_when_present() -> None:
    assert resolve(_json(), "gcp")["E2E_TENANT_DEPLOYMENT_NAME"] == "staging"
    assert "E2E_TENANT_DEPLOYMENT_NAME" not in resolve(_json(), "aws")


def test_blank_deployment_name_is_treated_as_absent() -> None:
    matrix = {"aws": {**_MATRIX["aws"], "deployment_name": "  "}}
    assert "E2E_TENANT_DEPLOYMENT_NAME" not in resolve(_json(matrix), "aws")


@pytest.mark.parametrize("cloud", ["", "   "])
def test_empty_cloud_falls_back_to_the_single_tenant(cloud: str) -> None:
    env = resolve(_json(), cloud, _FALLBACK)
    assert env["SDR_TEST_TENANT"] == "legacy.example.com"
    assert env["ATLAN_BASE_URL"] == "https://legacy.example.com"


@pytest.mark.parametrize("matrix_json", ["", "   "])
def test_empty_matrix_falls_back_to_the_single_tenant(matrix_json: str) -> None:
    env = resolve(matrix_json, "aws", _FALLBACK)
    assert env["SDR_TEST_TENANT"] == "legacy.example.com"
    assert env["SDR_CLIENT_ID"] == "legacy-client"


def test_unknown_cloud_is_an_error_not_a_skipped_leg() -> None:
    # A typo in the e2e-clouds input must fail loudly. Quietly dropping the leg
    # would show a green gate for coverage that never ran.
    with pytest.raises(TenantMatrixError) as exc:
        resolve(_json(), "aws-2", _FALLBACK)
    assert "aws, azure, gcp" in str(exc.value)


@pytest.mark.parametrize("field", ["tenant", "client_id", "client_secret", "api_key"])
def test_missing_or_blank_required_field_is_rejected(field: str) -> None:
    matrix = {"aws": {**_MATRIX["aws"], field: ""}}
    with pytest.raises(TenantMatrixError) as exc:
        resolve(_json(matrix), "aws")
    assert field in str(exc.value)

    matrix = {"aws": {k: v for k, v in _MATRIX["aws"].items() if k != field}}
    with pytest.raises(TenantMatrixError):
        resolve(_json(matrix), "aws")


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(TenantMatrixError):
        resolve("{not json", "aws")


def test_non_object_payloads_are_rejected() -> None:
    with pytest.raises(TenantMatrixError):
        resolve(json.dumps(["aws"]), "aws")
    with pytest.raises(TenantMatrixError):
        resolve(json.dumps({"aws": "e2e-aws.example.com"}), "aws")


def test_no_tenant_anywhere_is_an_error() -> None:
    with pytest.raises(TenantMatrixError) as exc:
        resolve("", "", {})
    assert "no e2e tenant resolved" in str(exc.value)


def test_error_messages_never_carry_a_credential_value() -> None:
    """These strings are printed to the CI log."""
    secrets = [
        v
        for entry in _MATRIX.values()
        for k, v in entry.items()
        if k
        != "tenant"  # the tenant host is named nowhere either, but is not a credential
    ]
    for bad in ("aws-2", "nope"):
        with pytest.raises(TenantMatrixError) as exc:
            resolve(_json(), bad, _FALLBACK)
        for secret in secrets:
            assert secret not in str(exc.value)


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_main_writes_heredoc_env_lines(capsys) -> None:
    rc = main(["--matrix-json", _json(), "--cloud", "gcp"])
    assert rc == 0
    env = _parse_env(capsys.readouterr().out)
    assert env == {
        "SDR_TEST_TENANT": "e2e-gcp-main1.example.com",
        "SDR_CLIENT_ID": "gcp-client",
        "SDR_CLIENT_SECRET": "gcp-secret",
        "ATLAN_API_KEY": "gcp-key",
        "E2E_TENANT_DEPLOYMENT_NAME": "staging",
        "ATLAN_BASE_URL": "https://e2e-gcp-main1.example.com",
    }


def test_main_mask_only_emits_masks_and_no_env_lines(capsys) -> None:
    rc = main(["--matrix-json", _json(), "--cloud", "aws", "--mask-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "<<" not in out, "--mask-only must not write $GITHUB_ENV lines"
    masked = {ln[len("::add-mask::") :] for ln in out.splitlines()}
    # Every resolved value is registered, including the derived base URL.
    for value in resolve(_json(), "aws").values():
        assert value in masked


def test_deployment_name_is_written_but_not_masked(capsys) -> None:
    # It is not a credential, and it is a short common word — the runner masks by
    # substring, so registering "staging" would redact unrelated log text
    # (including the queue names an operator reads to trace a leg).
    main(["--matrix-json", _json(), "--cloud", "gcp", "--mask-only"])
    masked = {ln[len("::add-mask::") :] for ln in capsys.readouterr().out.splitlines()}
    assert "staging" not in masked
    # Still masks the actual credentials on the same leg.
    assert "gcp-secret" in masked

    main(["--matrix-json", _json(), "--cloud", "gcp"])
    assert _parse_env(capsys.readouterr().out)["E2E_TENANT_DEPLOYMENT_NAME"] == (
        "staging"
    )


def test_main_fallback_flags_reproduce_the_single_tenant_shape(capsys) -> None:
    rc = main(
        [
            "--matrix-json",
            "",
            "--cloud",
            "",
            "--fallback-tenant",
            "legacy.example.com",
            "--fallback-client-id",
            "legacy-client",
            "--fallback-client-secret",
            "legacy-secret",
            "--fallback-api-key",
            "legacy-key",
        ]
    )
    assert rc == 0
    env = _parse_env(capsys.readouterr().out)
    assert env == {**_FALLBACK, "ATLAN_BASE_URL": "https://legacy.example.com"}


def test_main_reports_errors_as_workflow_commands(capsys) -> None:
    rc = main(["--matrix-json", _json(), "--cloud", "bogus"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == "", "nothing may reach $GITHUB_ENV on the error path"
    assert captured.err.startswith("::error::")


def test_mask_and_write_passes_agree_on_the_value_set(capsys) -> None:
    """Every credential written to $GITHUB_ENV was registered by the mask pass.

    azure carries no deployment_name, so here the two sets are exactly equal —
    nothing is written that was not masked first.
    """
    main(["--matrix-json", _json(), "--cloud", "azure", "--mask-only"])
    masked = {ln[len("::add-mask::") :] for ln in capsys.readouterr().out.splitlines()}
    main(["--matrix-json", _json(), "--cloud", "azure"])
    written = set(_parse_env(capsys.readouterr().out).values())
    assert written <= masked


# ── Call-site ordering guard (twin of test_export_extra_env's) ───────────────
#
# resolve_e2e_tenant.py has the same leak shape as export_extra_env.py: a call
# site that keeps only the `>> "$GITHUB_ENV"` invocation puts unregistered
# credentials into the environment of every later step. The guard there
# discovers its callers rather than listing them; this one does the same for
# this script, so a new caller is covered the day it is added.

_CALL_SITE_INVOCATION = re.compile(
    r"resolve_e2e_tenant\.py\"?[ \t]*\\\n"
    r"(?:\s*--[a-z-]+[ \t]+\"?\$?\{?[^\n\"]*\"?[ \t]*\\\n)*"
    r"\s*(?P<mode>--mask-only|>>[ \t]*\"\$GITHUB_ENV\")"
)

_SCRIPT_MENTION = "resolve_e2e_tenant"
_CALL_SITE_SUFFIXES = ("*.y*ml", "*.sh", "*.py")
# The script and this file are the implementation, not callers. So is
# test_export_extra_env.py: it names this module in its own `_NOT_A_CALLER`
# exclusion (the twin guard has to know this one exists), which is a live
# non-comment mention but never an invocation.
_NOT_A_CALLER = frozenset(
    {
        _MODULE_PATH.resolve(),
        Path(__file__).resolve(),
        (Path(__file__).parent / "test_export_extra_env.py").resolve(),
    }
)


def _live_mentions(text: str) -> list[str]:
    return [
        line
        for line in text.split("\n")
        if _SCRIPT_MENTION in line and not line.lstrip().startswith("#")
    ]


def _call_site_files() -> list[Path]:
    found: list[Path] = []
    for pattern in _CALL_SITE_SUFFIXES:
        for path in (_REPO_ROOT / ".github").rglob(pattern):
            text = path.read_text()
            if _SCRIPT_MENTION not in text:
                continue
            if path.resolve() in _NOT_A_CALLER:
                continue
            found.append(path)
    assert found, "no call site found — did the script move or lose its callers?"
    return sorted(set(found))


def test_call_sites_are_the_expected_files() -> None:
    """Fails loudly if a call site appears somewhere this PR did not audit."""
    relative = {p.relative_to(_REPO_ROOT).as_posix() for p in _call_site_files()}
    assert relative == {
        ".github/workflows/e2e-full-reusable.yaml",
        ".github/workflows/tests-reusable.yaml",
    }


def test_every_env_write_call_site_masks_first() -> None:
    for path in _call_site_files():
        text = path.read_text()
        where = path.relative_to(_REPO_ROOT).as_posix()

        calls = list(_CALL_SITE_INVOCATION.finditer(text))
        live = [ln for ln in _live_mentions(text) if "resolve_e2e_tenant.py" in ln]
        assert len(calls) == len(live), (
            f"{where} names the script on {len(live)} non-comment line(s) but "
            f"only {len(calls)} match a checkable invocation. Rewrite it in the "
            "backslash-continued flag shape, or teach _CALL_SITE_INVOCATION the "
            f"new shape. Unreadable lines: {live}"
        )

        modes = ["mask" if c["mode"] == "--mask-only" else "write" for c in calls]
        assert modes, f"{where} has no recognisable invocation"
        assert len(modes) % 2 == 0, f"{where} has an unpaired invocation: {modes}"
        assert modes == ["mask", "write"] * (len(modes) // 2), (
            f"{where} does not mask before writing $GITHUB_ENV: {modes}. Values "
            "written to $GITHUB_ENV before ::add-mask:: registers them leak in "
            "cleartext from the next step that renders its env: group."
        )
