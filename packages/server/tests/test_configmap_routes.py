"""The connector setup-form surface, which had no test at all.

application_sdk specifies this heavily (test_service.py carries named
regression tests for the default fallback); the carve-out reimplemented it with
zero coverage, and one divergence had already crept in.

FND-1682 is the one that matters: ``artifact_schemas`` sorts before every real
form stem, so once an app declares ``artifactSchemas`` an unqualified request
serves the schema document as the form. It has no ``config`` key, so the setup
wizard renders blank behind an HTTP 200 -- nothing in the logs, the network tab
or pod stderr.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from server_sdk.handler.base import DefaultHandler
from server_sdk.server import _is_form_configmap, _norm_cm_id, build_asgi_app

FORM = {"config": {"steps": [{"id": "auth"}]}, "defaultConnectorType": "snowflake"}


@pytest.fixture
def gen_dir(tmp_path):
    def _write(**files):
        for stem, body in files.items():
            (tmp_path / f"{stem.replace('__', '-')}.json").write_text(json.dumps(body))
        return tmp_path

    return _write


def _client(directory) -> TestClient:
    return TestClient(
        build_asgi_app(DefaultHandler(), app_name="acme", generated_dir=directory),
        raise_server_exceptions=False,
    )


# ── the form filter ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stem", "is_form"),
    [
        ("snowflake-crawler", True),
        ("manifest", False),
        # FND-1682. Sorts first, so getting this wrong is not a corner case.
        ("artifact_schemas", False),
        ("atlan-connectors-s3", False),
        ("csa-connectors-gcs", False),
    ],
)
def test_is_form_configmap(stem: str, is_form: bool) -> None:
    assert _is_form_configmap(stem) is is_form


def test_artifact_schemas_never_wins_the_default_fallback(gen_dir) -> None:
    """The regression in full: an unqualified id must reach the real form."""
    d = gen_dir(
        artifact_schemas={"schemas": []},
        manifest={"dag": {}},
        snowflake__crawler=FORM,
    )
    resp = _client(d).get("/workflows/v1/configmap/acme")
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["data"]["defaultConnectorType"] == "snowflake"
    assert "steps" in body["data"]["config"]


# ── exact, fuzzy, fallback, 404 ─────────────────────────────────────────────


def test_exact_stem_returns_that_file(gen_dir) -> None:
    d = gen_dir(snowflake__crawler=FORM, postgres__crawler={"config": {"x": 1}})
    resp = _client(d).get("/workflows/v1/configmap/postgres-crawler")
    assert resp.status_code == 200
    assert resp.json()["data"]["metadata"]["name"] == "postgres-crawler"
    assert "x" in resp.json()["data"]["data"]["config"]


def test_exact_match_wins_over_the_fallback(gen_dir) -> None:
    """Two forms present, so a fallback would pick the wrong one."""
    d = gen_dir(aaa__first={"config": {"which": "aaa"}}, zzz__last={"config": {"which": "zzz"}})
    resp = _client(d).get("/workflows/v1/configmap/zzz-last")
    # config is orjson-serialized compactly, so compare parsed, not by substring.
    assert json.loads(resp.json()["data"]["data"]["config"]) == {"which": "zzz"}


@pytest.mark.parametrize("requested", ["Snowflake-Crawler", "atlan-snowflake-crawler"])
def test_fuzzy_ids_resolve(gen_dir, requested: str) -> None:
    d = gen_dir(snowflake__crawler=FORM, zzz__other={"config": {"which": "zzz"}})
    resp = _client(d).get(f"/workflows/v1/configmap/{requested}")
    assert resp.status_code == 200
    assert resp.json()["data"]["data"]["defaultConnectorType"] == "snowflake"


def test_norm_strips_only_the_bare_atlan_prefix() -> None:
    assert _norm_cm_id("atlan-snowflake") == "snowflake"
    assert _norm_cm_id("atlan-connectors-s3") == "atlan-connectors-s3"
    assert _norm_cm_id("atlan-csa-x") == "atlan-csa-x"


def test_unknown_id_with_no_eligible_form_is_404_not_500(gen_dir) -> None:
    d = gen_dir(manifest={"dag": {}}, artifact_schemas={"schemas": []})
    assert _client(d).get("/workflows/v1/configmap/nope").status_code == 404


def test_default_connector_type_is_echoed_only_when_present(gen_dir) -> None:
    d = gen_dir(plain__form={"config": {"a": 1}})
    data = _client(d).get("/workflows/v1/configmap/plain-form").json()["data"]["data"]
    assert "defaultConnectorType" not in data


# ── the listing ─────────────────────────────────────────────────────────────


def test_listing_excludes_the_manifest_but_keeps_credential_templates(gen_dir) -> None:
    """Deliberately a different rule from _is_form_configmap: the setup form's
    credential widget fetches atlan-connectors-<source> as its own configmap,
    so filtering those would drop names that work."""
    d = gen_dir(snowflake__crawler=FORM, manifest={"dag": {}}, atlan__connectors__s3={"c": 1})
    ids = _client(d).get("/workflows/v1/configmaps").json()["data"]["configmaps"]
    assert "snowflake-crawler" in ids
    assert "manifest" not in ids
    assert "atlan-connectors-s3" in ids


def test_listing_is_empty_not_an_error_without_a_generated_dir(tmp_path) -> None:
    resp = _client(tmp_path / "absent").get("/workflows/v1/configmaps")
    assert resp.status_code == 200
    assert resp.json()["data"]["configmaps"] == []
