"""Unit tests for the hermetic-source CI scaffold generator."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from application_sdk.testing.e2e.source_scaffold import (
    ENGINES,
    SourceConfig,
    UnsupportedSourceEngineError,
    load_config,
    main,
    render,
    write_scaffold,
)

_COMPOSE = ".github/e2e/e2e-full-docker-compose.yaml"
_SECRETS = ".github/e2e/make-secrets-e2e-full.py"

# The proven, hand-written mysql overlay this generator must reproduce (parsed
# from atlan-mysql-app's committed .github/e2e/e2e-full-docker-compose.yaml).
_MYSQL_GOLDEN_SERVICES = {
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
                "-p$MYSQL_ROOT_PASSWORD",
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


def _mysql_cfg() -> SourceConfig:
    return SourceConfig(
        app="mysql",
        kind="mysql",
        database="e2e_main",
        username="e2e_user",
        password="e2e_pass",
        root_password="root_e2e_pass",
        image="mysql:8.0.40",
    )


def _compose(cfg: SourceConfig) -> dict:
    return yaml.safe_load(render(cfg)[_COMPOSE])


class TestGoldenParity:
    def test_mysql_services_match_hand_written_golden(self) -> None:
        # $$ in the raw file is compose's escape for a literal $; PyYAML sees the
        # raw text, so the golden constant above uses the pre-escape $ form. Assert
        # against the raw string's single-$ reading by normalising.
        services = _compose(_mysql_cfg())["services"]
        # Normalise the compose-escaped $$ → $ for comparison.
        hc = services["mysql"]["healthcheck"]["test"]
        hc[-1] = hc[-1].replace("$$", "$")
        assert services == _MYSQL_GOLDEN_SERVICES

    def test_render_emits_both_files(self) -> None:
        out = render(_mysql_cfg())
        assert set(out) == {_COMPOSE, _SECRETS}


class TestCompose:
    def test_all_engines_render_valid_yaml_with_both_services(self) -> None:
        for kind in ENGINES:
            compose = _compose(SourceConfig(app=kind, kind=kind, database="e2e_main"))
            assert set(compose["services"]) == {ENGINES[kind].service, "atlan-app"}

    def test_atlan_app_override_is_engine_agnostic(self) -> None:
        # The deployment-name override must be identical regardless of engine —
        # that identity is the whole reason it is templated, not hand-written.
        overrides = {
            kind: _compose(SourceConfig(app=kind, kind=kind, database="db"))[
                "services"
            ]["atlan-app"]["environment"]
            for kind in ENGINES
        }
        assert all(
            env == ["ATLAN_DEPLOYMENT_NAME=e2e-full-ci-${GITHUB_RUN_ID}"]
            for env in overrides.values()
        )

    def test_depends_on_targets_the_source_service(self) -> None:
        compose = _compose(SourceConfig(app="pg", kind="postgres", database="db"))
        assert compose["services"]["atlan-app"]["depends_on"] == {
            "postgres": {"condition": "service_healthy"}
        }

    def test_image_override_wins_over_engine_default(self) -> None:
        compose = _compose(
            SourceConfig(app="mysql", kind="mysql", database="db", image="mysql:9.0")
        )
        assert compose["services"]["mysql"]["image"] == "mysql:9.0"

    def test_seed_file_override_flows_into_volume_and_comment(self) -> None:
        text = render(
            SourceConfig(app="pg", kind="postgres", database="db", seed_file="init.sql")
        )[_COMPOSE]
        assert "/.github/e2e/init.sql:" in text

    def test_port_and_env_are_engine_specific(self) -> None:
        pg = _compose(SourceConfig(app="pg", kind="postgres", database="d"))["services"]
        assert pg["postgres"]["expose"] == ["5432"]
        assert "POSTGRES_DB" in pg["postgres"]["environment"]


class TestSecretsScript:
    def test_keys_are_uppercased_app_name(self) -> None:
        script = render(SourceConfig(app="my-db", kind="postgres", database="d"))[
            _SECRETS
        ]
        assert "SDR_MY-DB_USERNAME" in script
        assert "SDR_MY-DB_PASSWORD" in script

    def test_script_executes_and_writes_expected_bundle(self, tmp_path: Path) -> None:
        cfg = SourceConfig(
            app="mysql", kind="mysql", database="d", username="u", password="p"
        )
        script = render(cfg)[_SECRETS]
        cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            # Execute the generated bundle script to prove it writes the JSON the
            # Dapr secret store reads — the generated code is the unit under test.
            exec(compile(script, _SECRETS, "exec"), {})  # noqa: S102
        finally:
            os.chdir(cwd)
        bundle = json.loads(
            (tmp_path / ".github/e2e/secrets/credentials.json").read_text()
        )
        assert bundle == {"SDR_MYSQL_USERNAME": "u", "SDR_MYSQL_PASSWORD": "p"}


class TestErrors:
    def test_unknown_engine_raises_with_cloud_hint(self) -> None:
        with pytest.raises(UnsupportedSourceEngineError, match="DataForge"):
            render(SourceConfig(app="redshift", kind="redshift", database="d"))

    def test_load_config_rejects_missing_required_keys(self, tmp_path: Path) -> None:
        p = tmp_path / "source.yaml"
        p.write_text("app: mysql\n")  # no kind / database
        with pytest.raises(ValueError, match="missing required key"):
            load_config(p)

    def test_load_config_rejects_unknown_keys(self, tmp_path: Path) -> None:
        p = tmp_path / "source.yaml"
        p.write_text("app: mysql\nkind: mysql\ndatabase: d\nbogus: 1\n")
        with pytest.raises(ValueError, match="unknown key"):
            load_config(p)


class TestCli:
    def test_main_writes_scaffold_from_source_yaml(self, tmp_path: Path) -> None:
        (tmp_path / ".github/e2e").mkdir(parents=True)
        (tmp_path / ".github/e2e/source.yaml").write_text(
            "app: mysql\nkind: mysql\ndatabase: e2e_main\n"
        )
        assert main([str(tmp_path)]) == 0
        assert (tmp_path / _COMPOSE).is_file()
        assert (tmp_path / _SECRETS).is_file()

    def test_main_errors_when_source_yaml_absent(self, tmp_path: Path) -> None:
        assert main([str(tmp_path)]) == 1

    def test_write_scaffold_returns_written_paths(self, tmp_path: Path) -> None:
        written = write_scaffold(
            SourceConfig(app="mysql", kind="mysql", database="d"), tmp_path
        )
        assert {p.relative_to(tmp_path).as_posix() for p in written} == {
            _COMPOSE,
            _SECRETS,
        }
