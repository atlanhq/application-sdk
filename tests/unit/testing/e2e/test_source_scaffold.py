"""Unit tests for the hermetic-source CI scaffold generator."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from application_sdk.testing.e2e.source_scaffold import (
    APP_KIND,
    ENGINES,
    SecretsNotDeclaredError,
    SourceConfig,
    UnsupportedSourceEngineError,
    canonical_seed,
    load_config,
    main,
    render,
    write_scaffold,
)

_COMPOSE = ".github/e2e/e2e-full-docker-compose.yaml"
_SECRETS = ".github/e2e/make-secrets-e2e-full.py"

# The proven, hand-written mysql overlay this generator must reproduce (parsed
# from atlan-mysql-app's committed e2e-full-docker-compose.yaml; $$ is compose's
# escape for a literal $, preserved verbatim through YAML round-trip).
_MYSQL_GOLDEN = {
    "mysql": {
        "image": "mysql:8.0.40",
        "healthcheck": {
            "test": [
                "CMD",
                "mysqladmin",
                "ping",
                "-h",
                "localhost",
                "-u",
                "root",
                "-p$$MYSQL_ROOT_PASSWORD",
            ],
            "interval": "5s",
            "timeout": "5s",
            "retries": 20,
            "start_period": "30s",
        },
        "environment": {
            "MYSQL_ROOT_PASSWORD": "root_e2e_pass",
            "MYSQL_DATABASE": "e2e_main",
            "MYSQL_USER": "e2e_user",
            "MYSQL_PASSWORD": "e2e_pass",
        },
        "volumes": [
            "${GITHUB_WORKSPACE}/.github/e2e/seed.sql:"
            "/docker-entrypoint-initdb.d/seed.sql:ro"
        ],
        "expose": ["3306"],
    },
    "atlan-app": {
        "depends_on": {"mysql": {"condition": "service_healthy"}},
        "environment": ["ATLAN_DEPLOYMENT_NAME=e2e-full-ci-${GITHUB_RUN_ID}"],
    },
}


def _services(cfg: SourceConfig) -> dict:
    return yaml.safe_load(render(cfg)[_COMPOSE])["services"]


class TestGoldenParity:
    def test_mysql_services_match_hand_written_golden(self) -> None:
        cfg = SourceConfig(app="mysql", kind="mysql", image="mysql:8.0.40")
        assert _services(cfg) == _MYSQL_GOLDEN

    def test_mysql_materializes_canonical_seed_to_seed_sql(self) -> None:
        out = render(SourceConfig(app="mysql", kind="mysql"))
        assert ".github/e2e/seed.sql" in out
        # canonical content, not an empty placeholder
        assert "CREATE TABLE customers" in out[".github/e2e/seed.sql"]


class TestScaffoldStructure:
    def test_every_engine_renders_valid_yaml_with_atlan_app(self) -> None:
        for kind in ENGINES:
            cfg = SourceConfig(app=kind, kind=kind, secrets={"SDR_X": "y"})
            services = _services(cfg)
            assert "atlan-app" in services
            assert len(services) >= 2  # ≥1 source service + atlan-app

    def test_atlan_app_override_is_engine_agnostic(self) -> None:
        envs = {
            kind: _services(SourceConfig(app=kind, kind=kind, secrets={"S": "v"}))[
                "atlan-app"
            ]["environment"]
            for kind in ENGINES
        }
        assert all(
            e == ["ATLAN_DEPLOYMENT_NAME=e2e-full-ci-${GITHUB_RUN_ID}"]
            for e in envs.values()
        )

    def test_atlan_app_depends_on_all_source_services(self) -> None:
        # mssql adds an init sidecar; atlan-app must wait for BOTH the server
        # (healthy) and the seed sidecar (completed) — else it races the seed.
        dep = _services(SourceConfig(app="mssql", kind="mssql"))["atlan-app"][
            "depends_on"
        ]
        assert dep == {
            "mssql": {"condition": "service_healthy"},
            "mssql-init": {"condition": "service_completed_successfully"},
        }

    def test_image_override_wins(self) -> None:
        svc = _services(SourceConfig(app="pg", kind="postgres", image="postgres:15"))
        assert svc["postgres"]["image"] == "postgres:15"


class TestSecretsPolicy:
    def test_sql_defaults_to_basic_username_password_bundle(self) -> None:
        script = render(SourceConfig(app="my-db", kind="postgres"))[_SECRETS]
        assert "SDR_MY-DB_USERNAME" in script
        assert "SDR_MY-DB_PASSWORD" in script

    def test_object_store_requires_declared_secrets(self) -> None:
        with pytest.raises(SecretsNotDeclaredError, match="declare `secrets:`"):
            render(SourceConfig(app="s3", kind="s3"))

    def test_query_engine_requires_declared_secrets(self) -> None:
        with pytest.raises(SecretsNotDeclaredError):
            render(SourceConfig(app="trino", kind="trino"))

    def test_declared_secrets_flow_into_bundle(self) -> None:
        script = render(
            SourceConfig(app="s3", kind="s3", secrets={"SDR_S3_ACCESS_KEY_ID": "AK"})
        )[_SECRETS]
        assert "SDR_S3_ACCESS_KEY_ID" in script and "AK" in script


class TestCanonicalSeeds:
    def test_shipped_engines_return_seed_text(self) -> None:
        for kind in ("mysql", "mariadb", "postgres", "mongodb"):
            assert canonical_seed(kind)  # non-empty

    def test_unshipped_sql_engines_return_none(self) -> None:
        for kind in ("clickhouse", "oracle", "mssql"):
            assert canonical_seed(kind) is None

    def test_render_omits_seed_when_no_canonical_shipped(self) -> None:
        # clickhouse has no shipped seed yet → compose + secrets only (app must
        # supply its own seed.sql). The compose still mounts seed.sql.
        out = render(SourceConfig(app="ch", kind="clickhouse"))
        assert set(out) == {_COMPOSE, _SECRETS}
        assert "seed.sql" in out[_COMPOSE]

    def test_seed_file_override_skips_canonical_materialization(self) -> None:
        out = render(SourceConfig(app="mysql", kind="mysql", seed_file="custom.sql"))
        assert ".github/e2e/seed.sql" not in out
        assert "custom.sql" in out[_COMPOSE]

    def test_mongo_materializes_js_seed(self) -> None:
        out = render(SourceConfig(app="mongo", kind="mongodb"))
        assert ".github/e2e/seed.js" in out


class TestObjectStoreAndQueryEngine:
    def test_minio_has_init_sidecar_and_bucket(self) -> None:
        svc = _services(
            SourceConfig(app="s3", kind="s3", bucket="my-bkt", secrets={"S": "v"})
        )
        assert "minio" in svc and "minio-init" in svc
        assert "my-bkt" in " ".join(svc["minio-init"]["entrypoint"])

    def test_trino_is_single_service_no_seed(self) -> None:
        out = render(SourceConfig(app="trino", kind="trino", secrets={"S": "v"}))
        assert not any(p.endswith(("seed.sql", "seed.js")) for p in out)
        svc = yaml.safe_load(out[_COMPOSE])["services"]
        assert set(svc) == {"trino", "atlan-app"}

    def test_gcs_has_no_healthcheck_and_uses_service_started(self) -> None:
        # The fake-gcs-server image is a minimal Go binary (no curl/shell), so a
        # CMD healthcheck can't run in-container — verified live. atlan-app must
        # depend on service_started, not service_healthy.
        compose = yaml.safe_load(
            render(SourceConfig(app="gcs", kind="gcs", secrets={"S": "v"}))[_COMPOSE]
        )["services"]
        assert "healthcheck" not in compose["gcs"]
        assert compose["atlan-app"]["depends_on"] == {
            "gcs": {"condition": "service_started"}
        }


class TestErrors:
    def test_unknown_engine_raises_with_dataforge_hint(self) -> None:
        with pytest.raises(UnsupportedSourceEngineError, match="DataForge"):
            render(SourceConfig(app="redshift", kind="redshift"))


class TestAppMap:
    def test_all_mapped_apps_resolve_to_registered_engine(self) -> None:
        for app, kind in APP_KIND.items():
            assert kind in ENGINES, f"{app} → {kind} not registered"

    def test_load_config_defaults_kind_from_app_map(self, tmp_path: Path) -> None:
        p = tmp_path / "source.yaml"
        p.write_text("app: cloudsqlpostgres\n")  # no kind
        cfg = load_config(p)
        assert cfg.kind == "postgres"

    def test_load_config_requires_kind_for_unknown_app(self, tmp_path: Path) -> None:
        p = tmp_path / "source.yaml"
        p.write_text("app: some-new-thing\n")
        with pytest.raises(ValueError, match="not in the known-app map"):
            load_config(p)

    def test_load_config_rejects_unknown_keys(self, tmp_path: Path) -> None:
        p = tmp_path / "source.yaml"
        p.write_text("app: mysql\nbogus: 1\n")
        with pytest.raises(ValueError, match="unknown key"):
            load_config(p)


class TestCli:
    def test_main_writes_scaffold_from_source_yaml(self, tmp_path: Path) -> None:
        (tmp_path / ".github/e2e").mkdir(parents=True)
        (tmp_path / ".github/e2e/source.yaml").write_text("app: postgres\n")
        assert main([str(tmp_path)]) == 0
        assert (tmp_path / _COMPOSE).is_file()
        assert (tmp_path / _SECRETS).is_file()
        assert (tmp_path / ".github/e2e/seed.sql").is_file()  # canonical materialized

    def test_main_errors_when_source_yaml_absent(self, tmp_path: Path) -> None:
        assert main([str(tmp_path)]) == 1

    def test_main_prints_no_seed_note_for_unshipped_engine(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A registered SQL engine that ships no canonical seed (clickhouse) — the
        # main() note is the primary adopter signal that they must supply seed.sql.
        (tmp_path / ".github/e2e").mkdir(parents=True)
        (tmp_path / ".github/e2e/source.yaml").write_text("app: ch\nkind: clickhouse\n")
        assert main([str(tmp_path)]) == 0
        assert "no canonical seed shipped" in capsys.readouterr().out

    def test_main_reports_missing_secrets_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # query_engine kind with no `secrets:` → clean stderr line + exit 1
        # (SecretsNotDeclaredError), not a raw traceback.
        (tmp_path / ".github/e2e").mkdir(parents=True)
        (tmp_path / ".github/e2e/source.yaml").write_text("app: trino\nkind: trino\n")
        assert main([str(tmp_path)]) == 1
        assert "secrets" in capsys.readouterr().err.lower()

    def test_secrets_script_executes_to_expected_bundle(self, tmp_path: Path) -> None:
        script = render(
            SourceConfig(app="mysql", kind="mysql", username="u", password="p")
        )[_SECRETS]
        cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            exec(compile(script, _SECRETS, "exec"), {})  # noqa: S102 — verify generated code
        finally:
            os.chdir(cwd)
        bundle = json.loads(
            (tmp_path / ".github/e2e/secrets/credentials.json").read_text()
        )
        assert bundle == {"SDR_MYSQL_USERNAME": "u", "SDR_MYSQL_PASSWORD": "p"}

    def test_write_scaffold_returns_written_paths(self, tmp_path: Path) -> None:
        written = write_scaffold(SourceConfig(app="mysql", kind="mysql"), tmp_path)
        rels = {p.relative_to(tmp_path).as_posix() for p in written}
        assert _COMPOSE in rels and _SECRETS in rels and ".github/e2e/seed.sql" in rels
