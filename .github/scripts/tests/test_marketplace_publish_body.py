"""Tests for .github/scripts/marketplace_publish_body.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from marketplace_publish_body import (  # noqa: E402
    PublishBodyError,
    PublishRequest,
    build,
)


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
        ("commit_sha", "commit_sha"),
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


def test_commit_sha_is_sent_when_present() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    body = build(_minimal(commit_sha=sha))
    assert body["commit_sha"] == sha


def test_body_is_json_serialisable() -> None:
    # It goes straight onto the wire; a non-serialisable value would fail at the
    # request rather than here.
    json.dumps(build(_minimal(entrypoints='[{"name": "openapi"}]')))
