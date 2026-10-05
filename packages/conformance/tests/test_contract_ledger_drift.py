"""Drift guard for the SDK's contract ledger — the forcing function for B006.

The SDK commits exactly one ledger, ``contract_schema.lock.json`` at the
repository root (FND-3108).  The SDK's own B005/B006 self-scan reads it, and the
conformance wheel's build hook packages it so B005 in a consumer app can tell an
SDK-retired field from an app-made removal.  For either use to mean anything,
the committed file must always equal a fresh regeneration of the SDK source:

    uv run atlan-application-sdk-conformance gen-contract-ledger

The ledger records the template contracts (``application_sdk/templates/
contracts/``) and the SDK contract bases ``Input``, ``Output`` and
``PublishInputMixin`` (``application_sdk/contracts/base.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite.checks._sdk_contract_mixins import SDK_CONTRACT_BASE_FIELDS
from conformance.suite.checks.deprecation import _ledger_schema
from conformance.suite.checks.deprecation._ledger_schema import (
    LEDGER_VERSION,
    ContractField,
    ContractLedger,
    load_ledger,
    load_sdk_ledger,
    serialize,
)
from conformance.tools.generate_contract_ledger import build_ledger

_LEDGER_NAME = "contract_schema.lock.json"


def _find_sdk_root() -> Path | None:
    """The SDK checkout holding this test: ``application_sdk/`` beside ``packages/conformance/``."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "application_sdk").is_dir() and (
            parent / "packages" / "conformance" / "pyproject.toml"
        ).is_file():
            return parent
    return None


@pytest.fixture
def sdk_root() -> Path:
    root = _find_sdk_root()
    if root is None:
        pytest.skip("SDK source not on disk — the SDK ledger cannot be checked here.")
    return root


def test_root_ledger_is_committed(sdk_root: Path) -> None:
    """The root ledger exists, parses, and records the SDK's template contracts."""
    ledger_file = sdk_root / _LEDGER_NAME
    assert ledger_file.is_file(), (
        f"{ledger_file} is missing — run "
        "`uv run atlan-application-sdk-conformance gen-contract-ledger`."
    )
    assert load_ledger(ledger_file).fields


def test_committed_ledger_matches_fresh_scan(sdk_root: Path) -> None:
    """The root ledger equals a fresh scan of the SDK source.

    If this fails, an SDK contract was added or changed without regenerating
    the ledger.  Run ``gen-contract-ledger`` and commit the result in the same
    PR.
    """
    ledger_file = sdk_root / _LEDGER_NAME
    fresh = build_ledger(sdk_root, load_ledger(ledger_file))
    assert ledger_file.read_text(encoding="utf-8") == serialize(fresh), (
        "contract_schema.lock.json is stale — regenerate with "
        "`uv run atlan-application-sdk-conformance gen-contract-ledger` and commit it."
    )


def test_ledger_records_every_sdk_contract_base_field(sdk_root: Path) -> None:
    """``Input``/``Output``/``PublishInputMixin`` are recorded, so a 'sunset' on
    one of their fields can reach the apps that inherit it (FND-3107)."""
    recorded = {
        (f.contract, f.field) for f in load_ledger(sdk_root / _LEDGER_NAME).fields
    }
    expected = {
        (contract, f.name)
        for contract, fields in SDK_CONTRACT_BASE_FIELDS.items()
        for f in fields
    }
    assert expected <= recorded


def test_sdk_ledger_retires_no_field_before_origin_is_recorded(
    sdk_root: Path,
) -> None:
    """No SDK field may be 'sunset' until consumer ledgers record field origin (FND-3277).

    B005 waives a removed field when the SDK ledger retires one of the same name
    and type, but a consumer ledger cannot yet tell an inherited field from one
    the app declared itself. The first SDK sunset would let an app drop its own
    shipped field unflagged. Land FND-3277, then delete this test.
    """
    sunset = sorted(
        f"{f.contract}.{f.field}"
        for f in load_ledger(sdk_root / _LEDGER_NAME).fields
        if f.status == "sunset"
    )
    assert not sunset, (
        f"the SDK ledger retires {sunset}, but consumer ledgers do not record "
        "field origin yet, so B005 would also waive an app's removal of its "
        "own same-named field. Land FND-3277 before retiring an SDK field."
    )


def test_installed_package_ships_the_root_ledger_byte_identical(
    sdk_root: Path,
) -> None:
    """The ledger the installed conformance distribution hands to apps is the root one.

    Read through the distribution's RECORD rather than ``conformance.__file__``:
    pytest puts ``packages/conformance`` on ``sys.path``, so the imported module
    is the source tree even when the venv holds a built install.
    """
    from importlib import metadata

    try:
        dist = metadata.distribution("atlan-application-sdk-conformance")
    except metadata.PackageNotFoundError:
        pytest.skip("atlan-application-sdk-conformance is not installed")
    direct_url = json.loads(dist.read_text("direct_url.json") or "{}")
    if direct_url.get("dir_info", {}).get("editable"):
        pytest.skip(
            "conformance is an editable install; only a built install carries "
            "the packaged ledger."
        )
    packaged = f"conformance/data/{_LEDGER_NAME}"
    recorded = [f for f in dist.files or [] if f.as_posix() == packaged]
    assert recorded, (
        f"the installed distribution has no {packaged} — the wheel's build hook "
        "did not package the SDK ledger."
    )
    installed = Path(str(dist.locate_file(recorded[0])))
    assert installed.read_bytes() == (sdk_root / _LEDGER_NAME).read_bytes(), (
        "the installed conformance package carries a different SDK ledger than "
        "the repo root — reinstall it (uv sync --reinstall-package "
        "atlan-application-sdk-conformance)."
    )


def _load_ledger_build_hook(monkeypatch: pytest.MonkeyPatch) -> type:
    """Import ``hatch_build_ledger`` against a stub ``BuildHookInterface``.

    hatchling is a build-time requirement only, so it is absent from the test
    environment; the hook uses nothing of its base class but ``self.root``.
    """
    import importlib.util
    import sys
    import types

    class BuildHookInterface:
        def __init__(self, root: str) -> None:
            self.root = root

    names = (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
        "hatchling.builders.hooks.plugin.interface",
    )
    for name in names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules[names[-1]].BuildHookInterface = BuildHookInterface  # type: ignore[attr-defined]
    path = Path(__file__).resolve().parents[1] / "hatch_build_ledger.py"
    spec = importlib.util.spec_from_file_location("hatch_build_ledger", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LedgerBuildHook


@pytest.mark.parametrize("target", ["wheel", "sdist"])
def test_build_hook_is_registered_for_every_build_target(target: str) -> None:
    """The hook only packages the ledger if pyproject wires it into the build."""
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    targets = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["hatch"][
        "build"
    ]["targets"]
    assert targets[target]["hooks"]["custom"]["path"] == "hatch_build_ledger.py"


def test_build_hook_packages_the_root_ledger_into_a_standard_wheel(
    sdk_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check the editable-install skip above cannot make in CI.

    CI installs conformance editable, so no built wheel is ever inspected; this
    drives the hook directly the way a ``uv build`` from the checkout does.
    """
    hook = _load_ledger_build_hook(monkeypatch)(
        str(sdk_root / "packages" / "conformance")
    )
    build_data: dict[str, dict[str, str]] = {"force_include": {}}
    hook.initialize("standard", build_data)
    assert build_data["force_include"] == {
        str(sdk_root / _LEDGER_NAME): f"conformance/data/{_LEDGER_NAME}"
    }


def test_no_ledger_copy_is_committed_inside_the_package(sdk_root: Path) -> None:
    """The packaged copy is a build output; a second committed ledger drifts (FND-3108)."""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    tracked = subprocess.run(
        ["git", "-C", str(sdk_root), "ls-files", "--", f"*{_LEDGER_NAME}"],
        capture_output=True,
        text=True,
    )
    if tracked.returncode != 0:
        pytest.skip("not a git checkout")
    sdk_ledgers = [
        line
        for line in tracked.stdout.splitlines()
        if not line.startswith("packages/conformance/tests/")
    ]
    assert sdk_ledgers == [_LEDGER_NAME]


# ── load_ledger resolution ───────────────────────────────────────────────────


def test_load_ledger_repo_root_picks_up_committed_file(tmp_path: Path) -> None:
    """repo_root/contract_schema.lock.json is loaded when present."""
    ledger_data = {
        "version": LEDGER_VERSION,
        "fields": [{"contract": "R", "field": "r", "type": "str", "status": "active"}],
    }
    (tmp_path / _LEDGER_NAME).write_text(json.dumps(ledger_data), encoding="utf-8")
    ledger = load_ledger(repo_root=tmp_path)
    assert [f.contract for f in ledger.fields] == ["R"]


def test_load_ledger_is_empty_when_the_repo_has_no_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No repo ledger means an empty ledger — never the SDK's packaged one."""
    monkeypatch.delenv("ATLAN_CONTRACT_LEDGER_PATH", raising=False)
    assert load_ledger(repo_root=tmp_path).fields == []
    assert load_ledger().fields == []


def test_load_ledger_env_override_takes_priority_over_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ATLAN_CONTRACT_LEDGER_PATH wins over repo_root when both are set."""
    env_file = tmp_path / "env_ledger.json"
    env_data = {
        "version": LEDGER_VERSION,
        "fields": [
            {"contract": "Env", "field": "x", "type": "str", "status": "active"}
        ],
    }
    env_file.write_text(json.dumps(env_data), encoding="utf-8")

    repo_file = tmp_path / "contract_schema.lock.json"
    repo_data = {"version": LEDGER_VERSION, "fields": []}
    repo_file.write_text(json.dumps(repo_data), encoding="utf-8")

    monkeypatch.setenv("ATLAN_CONTRACT_LEDGER_PATH", str(env_file))
    ledger = load_ledger(repo_root=tmp_path)
    assert len(ledger.fields) == 1
    assert ledger.fields[0].contract == "Env"


def test_load_ledger_explicit_path_takes_priority_over_repo_root(
    tmp_path: Path,
) -> None:
    """An explicit *path* argument wins over repo_root."""
    explicit_file = tmp_path / "explicit.json"
    explicit_data = {
        "version": LEDGER_VERSION,
        "fields": [
            {"contract": "Explicit", "field": "y", "type": "int", "status": "active"}
        ],
    }
    explicit_file.write_text(json.dumps(explicit_data), encoding="utf-8")

    repo_file = tmp_path / "contract_schema.lock.json"
    repo_file.write_text(
        json.dumps({"version": LEDGER_VERSION, "fields": []}), encoding="utf-8"
    )

    ledger = load_ledger(path=explicit_file, repo_root=tmp_path)
    assert len(ledger.fields) == 1
    assert ledger.fields[0].contract == "Explicit"


# ── load_sdk_ledger ──────────────────────────────────────────────────────────


def test_load_sdk_ledger_reads_the_source_tree_root_without_a_packaged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SDK source-tree run reads the root ledger the wheel would package."""
    root_ledger = tmp_path / _LEDGER_NAME
    root_ledger.write_text(
        serialize(
            ContractLedger(
                version=LEDGER_VERSION,
                fields=[ContractField("Input", "workflow_slug", "str", "sunset")],
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_ledger_schema, "_LEDGER_RELPATH", ("data", "absent.json"))
    monkeypatch.setattr(_ledger_schema, "_source_tree_sdk_ledger", lambda: root_ledger)
    assert load_sdk_ledger().fields == [
        ContractField("Input", "workflow_slug", "str", "sunset")
    ]


def test_load_sdk_ledger_warns_when_the_package_carries_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A build without the ledger says so instead of silently dropping the exemption."""
    monkeypatch.setattr(_ledger_schema, "_LEDGER_RELPATH", ("data", "absent.json"))
    monkeypatch.setattr(_ledger_schema, "_source_tree_sdk_ledger", lambda: None)
    assert load_sdk_ledger().fields == []
    assert "carries no SDK contract ledger" in capsys.readouterr().err


def test_load_sdk_ledger_prefers_the_source_tree_root_over_a_packaged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale untracked packaged copy in a checkout never shadows the root ledger."""
    root_ledger = tmp_path / _LEDGER_NAME
    root_ledger.write_text(
        serialize(
            ContractLedger(
                version=LEDGER_VERSION,
                fields=[ContractField("Input", "workflow_slug", "str", "sunset")],
            )
        ),
        encoding="utf-8",
    )
    stale = tmp_path / "stale.json"
    stale.write_text(
        serialize(ContractLedger(version=LEDGER_VERSION, fields=[])), encoding="utf-8"
    )
    monkeypatch.setattr(_ledger_schema, "_LEDGER_RELPATH", (str(stale),))
    monkeypatch.setattr(_ledger_schema, "_source_tree_sdk_ledger", lambda: root_ledger)
    assert load_sdk_ledger().fields == [
        ContractField("Input", "workflow_slug", "str", "sunset")
    ]


@pytest.mark.parametrize(
    "payload", ['{"version": 1, "fields": [{"contract": "X"}]}', "[]"]
)
def test_load_sdk_ledger_warns_on_a_malformed_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: str,
) -> None:
    """A ledger of the wrong shape disables the exemption with a warning, not a crash."""
    bad = tmp_path / _LEDGER_NAME
    bad.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(_ledger_schema, "_source_tree_sdk_ledger", lambda: bad)
    assert load_sdk_ledger().fields == []
    assert "SDK contract ledger is unreadable" in capsys.readouterr().err


def test_load_sdk_ledger_ignores_the_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk_root: Path
) -> None:
    """Neither ATLAN_CONTRACT_LEDGER_PATH nor an app ledger stands in for the SDK's."""
    override = tmp_path / "env.json"
    override.write_text('{"version": 1, "fields": []}\n', encoding="utf-8")
    monkeypatch.setenv("ATLAN_CONTRACT_LEDGER_PATH", str(override))
    expected = load_ledger(sdk_root / _LEDGER_NAME).fields
    assert load_sdk_ledger().fields == expected


# ── gen-contract-ledger ──────────────────────────────────────────────────────


def test_sdk_generator_targets_the_root_ledger_from_a_subdirectory(
    sdk_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """In the SDK, ``gen-contract-ledger --check`` targets the root ledger from a subdir."""
    from conformance.tools.generate_contract_ledger import main

    monkeypatch.chdir(sdk_root / "packages" / "conformance")
    main(["--check"])
    assert "up-to-date" in capsys.readouterr().out


def test_new_consumer_ledger_is_not_seeded_from_the_sdk_ledger(
    tmp_path: Path, monkeypatch
) -> None:
    """An app generating its FIRST ledger must start empty, never from the
    SDK's own packaged ledger. build_ledger is append-only, so a seeded entry
    can never be removed: every SDK template contract would be baked into the
    consumer's ledger and fire B005 against any app class sharing its name
    (live: 14 of clickhouse's 25 findings)."""
    from conformance.tools.generate_contract_ledger import main

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "app.py").write_text(
        "from application_sdk.app import App\n\n"
        "class MyInput:\n    only_mine: str = ''\n\n"
        "class MyApp(App):\n"
        "    async def run(self, input: MyInput) -> None:\n        pass\n",
        encoding="utf-8",
    )
    out = tmp_path / "contract_schema.lock.json"
    main(["--repo", str(tmp_path), "--outfile", str(out)])

    import json

    contracts = {f["contract"] for f in json.loads(out.read_text())["fields"]}
    assert contracts == {"MyInput"}
    # The SDK's own template contracts must not have leaked in.
    assert "QueryExtractionInput" not in contracts
    assert "QueryExtractionOutput" not in contracts


def test_generator_records_a_contract_exposed_under_a_module_alias(
    tmp_path: Path,
) -> None:
    """A pkl-generated contract re-bound to a domain name is recorded, not skipped.

    The generator resolves entrypoint contracts by bare class name, so until it
    saw through the rebinding, ``MyInput = AppInputContract`` produced an empty
    ledger: every field of the app's real input contract went unrecorded, and
    B005 then had nothing to compare a later removal against. A clean B006 run
    in that state meant no protection, not compliance (FND-2605).

    Recorded under the *declaring* class, so the identity survives a rename of
    the local binding. Inherited fields come through the same resolution, which
    is why the SDK base's ``app_name`` is recorded here too.
    """
    from conformance.tools.generate_contract_ledger import main

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "generated.py").write_text(
        "from application_sdk.contracts import Input\n\n"
        "class AppInputContract(Input):\n    include_database_regex: str = ''\n",
        encoding="utf-8",
    )
    (tmp_path / "contracts.py").write_text(
        "from generated import AppInputContract\n\nMyInput = AppInputContract\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "from application_sdk.app import App\n"
        "from contracts import MyInput\n\n"
        "class MyApp(App):\n"
        "    async def run(self, input: MyInput) -> None:\n        pass\n",
        encoding="utf-8",
    )
    out = tmp_path / "contract_schema.lock.json"
    main(["--repo", str(tmp_path), "--outfile", str(out)])

    import json

    recorded = {
        (f["contract"], f["field"]) for f in json.loads(out.read_text())["fields"]
    }
    assert ("AppInputContract", "include_database_regex") in recorded
    assert ("AppInputContract", "app_name") in recorded
    assert not any(contract == "MyInput" for contract, _ in recorded)
