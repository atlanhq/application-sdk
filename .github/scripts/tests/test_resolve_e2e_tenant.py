"""Tests for .github/scripts/resolve_e2e_tenant.py."""

from __future__ import annotations

import json
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
#
# Where the two differ: export_extra_env's invocation is a fixed two-line shape,
# so a regex reads it safely. This script takes a variable number of
# backslash-continued flag lines, and the regex for that needs a repeated group
# whose leading whitespace class overlaps the previous iteration's trailing one
# — exponential backtracking (CodeQL py/redos, which flagged exactly that on the
# first version of this guard). So the continuation is walked line by line
# instead, which is linear and closer to what the shell does anyway.

# How an invocation ends, and what that means. The write form is matched on the
# exact redirect so a `>> "$SOMETHING_ELSE"` cannot pass as a masked write.
_MASK_TOKEN = "--mask-only"
_WRITE_TOKEN = '>> "$GITHUB_ENV"'

_SCRIPT_MENTION = "resolve_e2e_tenant"
_CALL_SITE_SUFFIXES = ("*.y*ml", "*.sh", "*.py")
# The script and this file are the implementation, not callers. So is
# test_export_extra_env.py: it names this module in its own `_NOT_A_CALLER`
# exclusion (the twin guard has to know this one exists), which is a live
# non-comment mention but never an invocation.
#
# The two FND-354 scripts name this module for the opposite reason: each states
# in its own docstring that it handles cloud KEYS and never credentials, and
# that this module remains the only thing that sees a tenant's entry. They are
# the *statement* of that boundary, so an invocation appearing in either would
# contradict the file it sits in — which is the reviewer's cue, since the
# exclusion itself would hide it from this guard. Kept to the two files whose
# prose carries the crux; everything else in FND-354 refers to "the per-leg
# tenant resolver" instead, precisely so it stays inside this net.
_NOT_A_CALLER = frozenset(
    {
        _MODULE_PATH.resolve(),
        Path(__file__).resolve(),
        (Path(__file__).parent / "test_export_extra_env.py").resolve(),
        (
            _REPO_ROOT / ".github/actions/discover-e2e-suites/discover_e2e_suites.py"
        ).resolve(),
        (_REPO_ROOT / ".github/scripts/e2e_tenant_matrix_clouds.py").resolve(),
    }
)


def _live_mentions(text: str) -> list[str]:
    return [
        line
        for line in text.split("\n")
        if _SCRIPT_MENTION in line and not line.lstrip().startswith("#")
    ]


def _invocation_modes(text: str) -> list[tuple[str, str | None]]:
    """Return ``(invoking line, "mask" | "write" | None)`` per invocation.

    Line-oriented rather than a single regex. The invocation spans a variable
    number of backslash-continued flag lines, and a regex for that shape needs a
    repeated group whose leading whitespace class overlaps the previous
    iteration's trailing one — ambiguity that backtracks exponentially on a
    crafted input, which is what CodeQL's py/redos flagged on the first version
    of this guard. Walking the continuation explicitly is linear, and it is a
    closer description of what the shell actually does with a trailing ``\\``.

    ``None`` means the invocation was found but its terminator was not
    recognised; the caller fails on that rather than skipping it, so an
    unparseable call site can never be silently treated as absent.
    """
    lines = text.split("\n")
    out: list[tuple[str, str | None]] = []
    for index, line in enumerate(lines):
        if f"{_SCRIPT_MENTION}.py" not in line or line.lstrip().startswith("#"):
            continue

        # Consume the backslash continuation to find the line that terminates
        # this command. Bounded by the file, so no unbounded scan.
        cursor = index
        while cursor < len(lines) - 1 and lines[cursor].rstrip().endswith("\\"):
            cursor += 1
        terminator = lines[cursor]

        if _MASK_TOKEN in terminator:
            out.append((line, "mask"))
        elif _WRITE_TOKEN in terminator:
            out.append((line, "write"))
        else:
            out.append((line, None))
    return out


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
        # FND-31: installs a given app image onto one e2e tenant, so it resolves
        # that tenant the same way a leg does. Audited: it runs the two-pass
        # --mask-only-then-write protocol, which the ordering guard below checks.
        ".github/workflows/e2e-tenant-install.yaml",
        ".github/workflows/tests-reusable.yaml",
    }


def _assert_call_sites_mask_first() -> None:
    """The ordering audit, as a plain helper.

    Extracted for the same reason as its twin in test_export_extra_env.py: the
    negative test below asserts it raises, and calling a test function directly
    would turn a fixture added later into a TypeError where an AssertionError
    was expected.
    """
    for path in _call_site_files():
        text = path.read_text()
        where = path.relative_to(_REPO_ROOT).as_posix()

        found = _invocation_modes(text)

        # Parity, not just presence: every non-comment mention of the script has
        # to belong to an invocation this guard could classify. Anything else
        # would be *discovered* but silently unaudited.
        unreadable = [line for line, mode in found if mode is None]
        assert not unreadable, (
            f"{where} invokes the script on {len(unreadable)} line(s) whose "
            'terminator is neither --mask-only nor >> "$GITHUB_ENV". Rewrite '
            "them in the backslash-continued flag shape, or teach "
            f"_invocation_modes the new shape. Unreadable lines: {unreadable}"
        )
        live = [ln for ln in _live_mentions(text) if f"{_SCRIPT_MENTION}.py" in ln]
        assert len(found) == len(live), (
            f"{where} names the script on {len(live)} non-comment line(s) but "
            f"only {len(found)} were parsed as invocations."
        )

        modes = [mode for _line, mode in found]
        assert modes, f"{where} has no recognisable invocation"
        assert len(modes) % 2 == 0, f"{where} has an unpaired invocation: {modes}"
        assert modes == ["mask", "write"] * (len(modes) // 2), (
            f"{where} does not mask before writing $GITHUB_ENV: {modes}. Values "
            "written to $GITHUB_ENV before ::add-mask:: registers them leak in "
            "cleartext from the next step that renders its env: group."
        )


def test_every_env_write_call_site_masks_first() -> None:
    _assert_call_sites_mask_first()


def _invocation(*flags: str, mode: str) -> str:
    """A backslash-continued invocation, as it appears in a workflow `run:`."""
    lines = ["          python3 scripts/resolve_e2e_tenant.py \\"]
    lines += [f"            {flag} \\" for flag in flags]
    lines.append(f"            {mode}")
    return "\n".join(lines) + "\n"


_WRITE_ONLY = _invocation('--matrix-json "$M"', '--cloud "$C"', mode=_WRITE_TOKEN)
_INVERTED = _invocation('--matrix-json "$M"', mode=_WRITE_TOKEN) + _invocation(
    '--matrix-json "$M"', mode=_MASK_TOKEN
)
_UNPARSEABLE = '          python3 resolve_e2e_tenant.py --matrix-json "$M" | tee out\n'


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(_WRITE_ONLY, "unpaired invocation", id="write-with-no-mask"),
        pytest.param(_INVERTED, "does not mask before writing", id="wrong-order"),
        pytest.param(_UNPARSEABLE, "terminator is neither", id="unreadable-shape"),
    ],
)
def test_a_leaky_caller_is_never_discovered_green(
    payload: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must actually fail on the shapes it exists to catch.

    Without this, a discovery predicate that quietly matched nothing would let
    every real call site through with the suite still green — the failure mode
    a discovery-based guard is most prone to.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "leaky.yaml").write_text(
        f"jobs:\n  x:\n    steps:\n      - run: |\n{payload}"
    )
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
    with pytest.raises(AssertionError, match=expected):
        _assert_call_sites_mask_first()


def test_the_real_call_sites_are_what_the_negative_tests_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correct shape passes — otherwise the tests above prove nothing."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    good = _invocation(
        '--matrix-json "$M"', '--cloud "$C"', mode=_MASK_TOKEN
    ) + _invocation('--matrix-json "$M"', '--cloud "$C"', mode=_WRITE_TOKEN)
    (workflows / "good.yaml").write_text(
        f"jobs:\n  x:\n    steps:\n      - run: |\n{good}"
    )
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
    _assert_call_sites_mask_first()
