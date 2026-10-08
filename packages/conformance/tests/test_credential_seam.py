"""Meta-tests for the P-series credential-seam check (P053, FND-2949).

P053 flags app code that routes a workflow input's credential channels itself —
``CredentialRef.resolve`` / ``resolve_or_none``, ``CredentialRef(credential_guid=
...)``, or its own flattening of inline ``[{key, value}]`` pairs — and module-level
copies of the credential types the SDK's seam exports.  The fixtures are
synthetic equivalents of the local ``build_credential_ref`` variants the fleet
carried; each must fire, and the named-secret / agent-spec / already-migrated
shapes must not.

The rule is gated on the app's locked SDK (``route_credentials`` ships in
3.40.0): ``scan_text`` is the ungated AST pass, ``scan_all`` / ``scan_path``
read ``uv.lock`` and stay silent below the floor or when it cannot be read.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks.credential_seam import (
    SERIES,
    discover,
    scan_all,
    scan_path,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)


def _p053(src: str, file: str = "app/credentials.py") -> list:
    return [f for f in scan_text(src, file) if f.rule_id == "P053"]


def _live(src: str, file: str = "app/credentials.py") -> list:
    return [f for f in _p053(src, file) if not f.suppressed]


def test_series_letter() -> None:
    assert SERIES == "P"


def test_p053_rule_metadata() -> None:
    rule = get_rule("P053")
    assert rule.name == "LocalCredentialRouting"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.mechanism == RuleMechanism.STATIC
    assert rule.category == "credential-seam"
    assert rule.autofixable is True
    assert rule.since == "0.40.0"
    assert rule.help_uri and rule.help_uri.endswith("prescriptions.md#p053")
    assert "route_credentials" in rule.full_description


# ── Fleet variants (synthetic) — each must fire ─────────────────────────────

#: Pre-built ``<app>_credential`` field, then strict ``resolve`` in a try/except,
#: then inline ``credentials`` pairs flattened into a dict.
_VARIANT_RESOLVE_WITH_FALLBACKS = (
    "from typing import Any\n"
    "from application_sdk.credentials import CredentialRef\n"
    "from application_sdk.credentials.errors import CredentialRoutingError\n"
    "\n"
    "def build_credential_ref(input) -> tuple[CredentialRef | None, dict[str, Any]]:\n"
    "    if input.example_credential is not None:\n"
    "        return input.example_credential, {}\n"
    "    try:\n"
    "        return CredentialRef.resolve(input), {}\n"
    "    except CredentialRoutingError:\n"
    "        pass\n"
    "    inline: dict[str, Any] = {}\n"
    "    for item in input.credentials or []:\n"
    '        inline[item["key"]] = item.get("value", "")\n'
    "    return None, inline\n"
)

#: Builds a GUID ref directly — never routes agent_json.
_VARIANT_DIRECT_GUID = (
    "from application_sdk.credentials import CredentialRef\n"
    "\n"
    "def build_credential_ref(input):\n"
    "    if input.credential_guid:\n"
    "        return CredentialRef(\n"
    "            name=input.credential_guid,\n"
    '            credential_type="unknown",\n'
    "            credential_guid=input.credential_guid,\n"
    "        ), {}\n"
    "    return None, {}\n"
)

#: Lenient resolution.
_VARIANT_RESOLVE_OR_NONE = (
    "from application_sdk.credentials import CredentialRef\n"
    "\n"
    "def build_credential_ref(input):\n"
    "    return CredentialRef.resolve_or_none(input), {}\n"
)

#: Dict-based router over raw workflow args.
_VARIANT_DICT_ARGS = (
    "from typing import Any\n"
    "\n"
    "def build_credential_ref(workflow_args: dict) -> dict:\n"
    '    credential_guid = workflow_args.get("credential_guid", "")\n'
    "    if credential_guid:\n"
    '        return {"credential_guid": credential_guid}\n'
    '    creds_list = workflow_args.get("credentials", [])\n'
    "    inline: dict[str, Any] = {}\n"
    "    for item in creds_list:\n"
    '        if isinstance(item, dict) and "key" in item:\n'
    '            inline[item["key"]] = item.get("value", "")\n'
    '    return {"inline_credentials": inline}\n'
)

#: Module-level copies of the seam's types.
_VARIANT_LOCAL_TYPES = (
    "from typing import Annotated\n"
    "from application_sdk.contracts.types import MaxItems\n"
    "\n"
    "CredentialValue = str | int | bool | None\n"
    "BoundedCredentialDict = Annotated[dict[str, CredentialValue], MaxItems(500)]\n"
)


def test_p053_fires_once_on_resolve_with_fallbacks() -> None:
    fs = _live(_VARIANT_RESOLVE_WITH_FALLBACKS)
    assert len(fs) == 1, "one function to migrate is one finding"
    finding = fs[0]
    assert finding.line == 9  # the first routing site: CredentialRef.resolve(...)
    assert "`build_credential_ref`" in finding.message
    assert "CredentialRef.resolve(...)" in finding.message
    assert "inline [{key, value}] flattening" in finding.message
    assert "route_credentials" in finding.message


def test_p053_fires_on_direct_guid_construction() -> None:
    fs = _live(_VARIANT_DIRECT_GUID)
    assert len(fs) == 1 and fs[0].line == 5
    assert "CredentialRef(credential_guid=...)" in fs[0].message


def test_p053_fires_on_resolve_or_none() -> None:
    fs = _live(_VARIANT_RESOLVE_OR_NONE)
    assert len(fs) == 1
    assert "CredentialRef.resolve_or_none(...)" in fs[0].message


def test_p053_fires_on_dict_based_router() -> None:
    fs = _live(_VARIANT_DICT_ARGS)
    assert len(fs) == 1 and fs[0].line == 9
    assert "inline [{key, value}] flattening" in fs[0].message
    assert "normalize_inline_credentials" in fs[0].message


def test_p053_fires_on_each_local_type_alias() -> None:
    fs = _live(_VARIANT_LOCAL_TYPES)
    assert [f.line for f in fs] == [4, 5]
    assert "`CredentialValue` is a local copy" in fs[0].message
    assert "`BoundedCredentialDict` is a local copy" in fs[1].message
    assert "application_sdk.credentials" in fs[0].message


@pytest.mark.parametrize(
    "stmt",
    [
        "CredentialValue = Union[str, int, None]\n",
        "CredentialValue = Optional[str]\n",
        "CredentialValue: TypeAlias = str | int\n",
        "CredentialMap = dict[str, str | None]\n",
        "InlineCredentials = list[dict[str, str]] | dict[str, str]\n",
        "BoundedCredentialList = Annotated[list[dict[str, str]], MaxItems(50)]\n",
        "_BoundedInlineCredentials = typing.Annotated[dict[str, str], 1]\n",
    ],
)
def test_p053_type_alias_forms(stmt: str) -> None:
    assert len(_live(stmt)) == 1


def test_p053_fires_on_pep695_type_alias() -> None:
    import ast

    if not hasattr(ast, "TypeAlias"):
        pytest.skip("PEP 695 `type` statements need Python 3.12+")
    assert len(_live("type CredentialValue = str | int | None\n")) == 1


def test_p053_fires_on_dict_comprehension_flattening() -> None:
    src = (
        "def inline(input):\n"
        '    return {i["key"]: i.get("value", "") for i in input.credentials}\n'
    )
    fs = _live(src)
    assert len(fs) == 1 and "inline [{key, value}] flattening" in fs[0].message


def test_p053_fires_through_aliased_and_qualified_credential_ref() -> None:
    aliased = (
        "from application_sdk.credentials.ref import CredentialRef as Ref\n"
        "def f(input):\n"
        "    return Ref.resolve(input)\n"
    )
    qualified = (
        "from application_sdk.credentials import ref\n"
        "def f(input):\n"
        "    return ref.CredentialRef.resolve(input)\n"
    )
    assert len(_live(aliased)) == 1
    assert len(_live(qualified)) == 1


def test_p053_nested_function_is_its_own_finding() -> None:
    src = (
        "from application_sdk.credentials import CredentialRef\n"
        "def outer(input):\n"
        "    def inner():\n"
        "        return CredentialRef.resolve_or_none(input)\n"
        "    return CredentialRef.resolve(input), inner\n"
    )
    fs = _live(src)
    assert sorted(f.line for f in fs) == [4, 5]
    assert any("`inner`" in f.message for f in fs)
    assert any("`outer`" in f.message for f in fs)


@pytest.mark.parametrize(
    "value",
    [
        "input.credential_guid",
        "input.credential_guid or ''",
        'workflow_args["credential_guid"]',
        'workflow_args.get("credential_guid", "")',
    ],
)
def test_p053_fires_on_each_own_guid_read(value: str) -> None:
    src = (
        "from application_sdk.credentials import CredentialRef\n"
        "def f(input, workflow_args):\n"
        f"    return CredentialRef(credential_guid={value})\n"
    )
    assert len(_live(src)) == 1


def test_p053_fires_on_guid_bound_from_the_input() -> None:
    src = (
        "from application_sdk.credentials import CredentialRef\n"
        "def f(input):\n"
        "    guid = input.credential_guid\n"
        "    return CredentialRef(name=guid, credential_guid=guid)\n"
    )
    fs = _live(src)
    assert len(fs) == 1 and fs[0].line == 4
    assert "CredentialRef(credential_guid=...)" in fs[0].message


def test_p053_silent_on_second_credential_guid() -> None:
    # openapi's shape: a per-source credential carried in another field is not
    # one of the input's own channels, so route_credentials cannot replace it.
    src = (
        "from application_sdk.credentials import CredentialRef\n"
        "async def download_cloud_spec(self, input):\n"
        "    ref = CredentialRef(credential_guid=input.cloud_source)\n"
        "    return await self.context.resolve_credential_raw(ref)\n"
    )
    assert _p053(src) == []


@pytest.mark.parametrize(
    "src",
    [
        # A name bound from some other field.
        "def f(input):\n"
        "    guid = input.cloud_source\n"
        "    return CredentialRef(credential_guid=guid)\n",
        # A name bound from the input's GUID in a *different* function.
        "def a(input):\n"
        "    guid = input.credential_guid\n"
        "def b(guid):\n"
        "    return CredentialRef(credential_guid=guid)\n",
        # Rebound away from the GUID before use.
        "def f(input):\n"
        "    guid = input.credential_guid\n"
        "    guid = input.cloud_source\n"
        "    return CredentialRef(credential_guid=guid)\n",
        # A literal.
        'REF = CredentialRef(name="g", credential_guid="g")\n',
        # A module-level own-GUID name, shadowed by a helper's parameter.
        "guid = ARGS.credential_guid\n"
        "def _ref(guid):\n"
        "    return CredentialRef(credential_guid=guid)\n",
        # ... and shadowed by a local rebinding to something else.
        "guid = ARGS.credential_guid\n"
        "def _ref(source):\n"
        "    guid = source.cloud_source\n"
        "    return CredentialRef(credential_guid=guid)\n",
    ],
)
def test_p053_silent_on_guid_not_from_the_input_channel(src: str) -> None:
    assert _p053(src) == []


def test_p053_module_guid_name_still_reaches_an_unshadowing_function() -> None:
    src = (
        "from application_sdk.credentials import CredentialRef\n"
        "guid = ARGS.credential_guid\n"
        "def _ref():\n"
        "    return CredentialRef(credential_guid=guid)\n"
    )
    assert len(_live(src)) == 1


# F-f3ea21: the value read may sit in the comprehension's filter.
def test_p053_fires_when_the_pair_value_is_read_in_a_filter() -> None:
    src = (
        "def flatten(credentials):\n"
        '    return {i["key"]: v for i in credentials if (v := i["value"])}\n'
    )
    assert len(_live(src)) == 1


def test_p053_module_level_call_fires() -> None:
    src = (
        "from application_sdk.credentials import CredentialRef\n"
        'REF = CredentialRef(name="g", credential_guid=ARGS["credential_guid"])\n'
    )
    fs = _live(src)
    assert len(fs) == 1 and "This code routes" in fs[0].message


# ── Negatives ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        # A named secret: names a credential, does not route one.
        "from application_sdk.credentials import CredentialRef\n"
        "def f():\n"
        '    return CredentialRef(name="x", credential_type="basic")\n',
        # A name taken from a variable, still no credential_guid= keyword.
        "from application_sdk.credentials import CredentialRef\n"
        "SECRET = 'svc-account'\n"
        "def f():\n"
        "    return CredentialRef(name=SECRET)\n",
        # An agent ref built from a spec the app already holds.
        "from application_sdk.credentials import CredentialRef\n"
        "def f(spec):\n"
        '    return CredentialRef(agent_spec=spec, credential_type="unknown")\n',
        # Already migrated onto the seam.
        "from application_sdk.credentials import route_credentials\n"
        "def run(self, input):\n"
        "    ref, inline = route_credentials(input)\n"
        "    return self.context.resolve_credential_raw_or_inline(ref, inline)\n",
        # The SDK's own types, imported.
        "from application_sdk.credentials import CredentialMap, CredentialValue\n"
        "Alias = CredentialMap\n",
        # Re-binding to the SDK's type is not a type shape.
        "import application_sdk.credentials as sdk\n"
        "CredentialValue = sdk.CredentialValue\n",
        # A same-named alias that is not a module-level statement.
        "class Model:\n    CredentialValue = str | int\n",
        # An unrelated union alias.
        "ScalarValue = str | int | None\n",
        # Key/value pairs that are not credentials (tags, parameters).
        "def tags(input):\n"
        '    return {t["key"]: t.get("value") for t in input.tags}\n',
        "def params(rows):\n"
        "    out = {}\n"
        "    for row in rows:\n"
        '        out[row["key"]] = row["value"]\n'
        "    return out\n",
        # A credentials loop that does not read a key/value pair.
        "def names(input):\n" '    return [c["key"] for c in input.credentials]\n',
        # A mention in a docstring or a comment is not a call.
        "def helper():\n"
        '    """Routes via CredentialRef.resolve(input), see CredentialRef(credential_guid=g)."""\n'
        "    # CredentialRef.resolve_or_none(input)\n"
        "    return None\n",
        # Some other class's resolve().
        "def f(path):\n    return path.resolve()\n",
    ],
)
def test_p053_silent(src: str) -> None:
    assert _p053(src) == []


def test_p053_syntax_error_is_silent() -> None:
    assert _p053("def broken(:\n") == []


# ── Inline suppression ───────────────────────────────────────────────────────


def test_p053_trailing_suppression() -> None:
    src = (
        "from application_sdk.credentials import CredentialRef\n"
        "def legacy_ref(input):\n"
        "    return CredentialRef(credential_guid=input.credential_guid)"
        "  # conformance: ignore[P053] migration tracked separately\n"
    )
    fs = _p053(src)
    assert len(fs) == 1 and fs[0].suppressed
    assert fs[0].suppression_justification == ("migration tracked separately")


def test_p053_comment_above_suppression_on_type_alias() -> None:
    src = (
        "# conformance: ignore[P053] kept until the SDK floor reaches 3.40.0\n"
        "CredentialValue = str | int | bool | None\n"
    )
    fs = _p053(src)
    assert len(fs) == 1 and fs[0].suppressed


def test_p053_other_rule_suppression_does_not_apply() -> None:
    src = "CredentialValue = str | int  # conformance: ignore[P052] unrelated\n"
    assert len(_live(src)) == 1


# ── SDK-version gate (repo boundary) ────────────────────────────────────────


def _uv_lock(sdk_version: str | None) -> str:
    """A minimal uv.lock. ``None`` omits the atlan-application-sdk entry."""
    other = (
        "[[package]]\n"
        'name = "some-dep"\n'
        'version = "1.2.3"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    if sdk_version is None:
        return other
    return (
        f"{other}\n[[package]]\n"
        'name = "atlan-application-sdk"\n'
        f'version = "{sdk_version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )


def _app(tmp_path: Path, lock: str | None) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "credentials.py").write_text(
        _VARIANT_RESOLVE_OR_NONE, encoding="utf-8"
    )
    if lock is not None:
        (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("version", ["3.40.0", "3.40.1", "3.41.0", "4.0.0"])
def test_p053_fires_at_or_above_the_sdk_floor(tmp_path: Path, version: str) -> None:
    root = _app(tmp_path, _uv_lock(version))
    fs = scan_all(discover(root), root)
    assert [(f.rule_id, f.file) for f in fs] == [("P053", "app/credentials.py")]
    assert len(scan_path(root / "app" / "credentials.py", root)) == 1


@pytest.mark.parametrize(
    "lock",
    [
        _uv_lock("3.39.1"),
        _uv_lock("3.28.0"),
        _uv_lock(None),  # SDK not in the lock
        "this is : not valid = toml [",
        "package = 1\n",  # valid TOML, but `package` is not a list
        None,  # no lock at all
    ],
    ids=[
        "below-floor",
        "far-below",
        "sdk-absent",
        "unparseable",
        "package-not-a-list",
        "no-lock",
    ],
)
def test_p053_silent_when_seam_not_confirmed(tmp_path: Path, lock: str | None) -> None:
    root = _app(tmp_path, lock)
    assert scan_all(discover(root), root) == []
    assert scan_path(root / "app" / "credentials.py", root) == []


def test_p053_skips_test_files(tmp_path: Path) -> None:
    root = _app(tmp_path, _uv_lock("3.40.0"))
    for rel in ("tests/unit/test_creds.py", "tests/conftest.py", "app/test_creds.py"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_VARIANT_DIRECT_GUID, encoding="utf-8")
    assert {f.file for f in scan_all(discover(root), root)} == {"app/credentials.py"}


def test_p053_reaches_the_runner(tmp_path: Path) -> None:
    """End to end: registered with the runner, reported for an app on >= 3.40.0."""
    import json

    from conformance.suite.runner import main as runner_main

    root = _app(tmp_path, _uv_lock("3.40.0"))
    (root / "atlan.yaml").write_text("name: example\n", encoding="utf-8")
    out = tmp_path / "report.sarif"
    runner_main(["--repo", str(root), "--series", "P", "--output", str(out)])
    results = json.loads(out.read_text(encoding="utf-8"))["runs"][0]["results"]
    assert any(r["ruleId"] == "P053" for r in results)
