"""Tests for .github/scripts/marketplace_publish_body.py."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from marketplace_publish_body import (  # noqa: E402
    PublishBodyError,
    PublishRequest,
    build,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _minimal(**overrides: object) -> PublishRequest:
    base = {
        "app_id": "019d1f6b-6fea-7db3-96d8-e61e159d0351",
        "image": "ghcr.io/atlanhq/atlan-openapi-app:sdr-test-abc12345",
        "version": "sdr-test-abc12345",
        "branch": "chrishehim/fnd-31",
        "allowed_tenants": ("example-tenant",),
    }
    base.update(overrides)
    return PublishRequest(**base)  # type: ignore[arg-type]


# ── Required fields ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["app_id", "image", "version", "branch"])
def test_missing_required_field_fails_naming_it(field: str) -> None:
    with pytest.raises(PublishBodyError, match=field):
        build(_minimal(**{field: ""}))


def test_unscoped_publish_is_refused() -> None:
    # Neither allowed_tenants nor target_channel: GM would decide, and which way
    # it decides is whether a per-PR e2e build reaches every tenant.
    with pytest.raises(PublishBodyError, match="scoped"):
        build(_minimal(allowed_tenants=()))


# ── Scoping ──────────────────────────────────────────────────────────────────


def test_tenant_targeting_produces_allowed_tenants_and_no_channel() -> None:
    body = build(_minimal(allowed_tenants=("example-tenant",)))
    assert body["allowed_tenants"] == ["example-tenant"]
    assert "target_channel" not in body


def test_tenant_targeting_wins_over_channel() -> None:
    body = build(_minimal(allowed_tenants=("example-tenant",), target_channel="all"))
    assert body["allowed_tenants"] == ["example-tenant"]
    assert "target_channel" not in body, (
        "sending both lets GM choose; tenant-targeting must win so a per-PR "
        "build cannot become visible to every tenant"
    )


def test_channel_used_when_no_tenants() -> None:
    body = build(_minimal(allowed_tenants=(), target_channel="all"))
    assert body["target_channel"] == "all"
    assert "allowed_tenants" not in body


# ── Optional fields: absent vs empty ─────────────────────────────────────────


@pytest.mark.parametrize(
    "field, wire_key",
    [
        ("repo_url", "repo"),
        ("deploy_config", "config"),
        ("sdk_version", "sdk_version"),
        ("app_configs", "app_configs"),
        ("release_model", "release_model"),
        ("created_by", "created_by"),
        ("entrypoints", "entrypoints"),
    ],
)
def test_empty_optional_is_omitted_not_blanked(field: str, wire_key: str) -> None:
    # GM distinguishes absent from empty on several of these — an empty `repo`
    # 409s for apps registered with a source repo.
    body = build(_minimal(**{field: ""}))
    assert wire_key not in body


def test_entrypoints_is_sent_as_parsed_json() -> None:
    body = build(_minimal(entrypoints='[{"name": "openapi"}]'))
    assert body["entrypoints"] == [{"name": "openapi"}]


def test_invalid_entrypoints_json_fails_loudly() -> None:
    with pytest.raises(PublishBodyError, match="entrypoints"):
        build(_minimal(entrypoints="{not json"))


def test_sdr_capability_is_prepended_to_config() -> None:
    body = build(_minimal(deploy_config="key: value", self_deployed_runtime=True))
    assert body["config"] == "self_deployed_runtime: true\nkey: value"


def test_sdr_capability_alone_still_produces_a_config() -> None:
    # SDR capability rides inside the opaque config blob, so it must survive an
    # app with no deploy: block at all.
    body = build(_minimal(deploy_config="", self_deployed_runtime=True))
    assert body["config"] == "self_deployed_runtime: true"


def test_source_marks_the_registration_as_ci() -> None:
    assert build(_minimal())["source"] == "ci_publish"


# ── Drift guard against the release path ─────────────────────────────────────


def test_field_set_matches_the_release_workflow() -> None:
    """The e2e path must register the same SHAPE the release path does.

    FND-31 exists so the tenant runs the version under test. If e2e registered a
    differently-shaped version than releases do, e2e would be validating a
    registration shape that never ships.

    build-and-publish-app.yaml still builds its body in an inline heredoc — it is
    the path every release goes through, so it is deliberately not rewired in the
    same change that introduces this module. This guard is what makes that safe:
    the two cannot drift unnoticed. When the workflow is switched over to import
    this module, delete this test.
    """
    workflow = (_REPO_ROOT / ".github/workflows/build-and-publish-app.yaml").read_text(
        encoding="utf-8"
    )
    # Keys the workflow assigns into its publish body, either as a literal in the
    # dict or via body["key"] = ....
    workflow_keys = set(re.findall(r'body\["(\w+)"\]', workflow))
    workflow_keys |= {
        key
        for key in re.findall(r'^\s{14}"(\w+)":', workflow, re.M)
        # The literal dict is indented 14 spaces inside the heredoc; filter to the
        # publish-body keys we know it declares there.
        if key in {"app_id", "image", "version", "branch", "repo", "source"}
    }

    ours = set(build(_minimal(allowed_tenants=(), target_channel="all")))
    ours |= set(
        build(
            _minimal(
                repo_url="https://github.com/atlanhq/atlan-openapi-app",
                deploy_config="k: v",
                sdk_version="3.26.1",
                entrypoints="[]",
                app_configs="e30=",
                release_model="semver",
                created_by="someone",
            )
        )
    )

    missing = workflow_keys - ours
    assert not missing, (
        f"build-and-publish-app.yaml sends publish fields this builder does not: "
        f"{sorted(missing)}. The e2e path would register a different shape than "
        "releases do."
    )


def test_body_is_json_serialisable() -> None:
    # It goes straight onto the wire; a non-serialisable value would fail at the
    # request rather than here.
    json.dumps(build(_minimal(entrypoints='[{"name": "openapi"}]')))
