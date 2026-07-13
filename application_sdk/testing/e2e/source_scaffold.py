"""Generate the hermetic-source CI scaffold for the full-DAG e2e harness.

The full-DAG (two-store SDR) e2e extracts from a **real source**. Historically
each connector hand-wrote three files under ``.github/e2e/``
(``e2e-full-docker-compose.yaml``, ``make-secrets-e2e-full.py``, ``seed.sql``).
The first two are ~95% boilerplate and the third is shareable *per engine* — the
e2e extracts metadata *shape*, not business data, so one canonical dataset per
engine gives every connector of that engine deterministic, uniform asset counts.

This module centralises all of that:

* **Canonical seeds ship in the SDK** (``seeds/<engine>.sql|.js``) — a connector
  provides *nothing* for a standard source; it may drop a ``.github/e2e/<seed>``
  to override for exotic object types.
* The **compose overlay + secrets bundle are generated** from a small
  ``.github/e2e/source.yaml`` declaration.

So per-app work collapses to a few lines of ``source.yaml`` (often just
``app`` + ``kind``). Scaffold *types* covered:

===============  =================================================  ===========
scaffold         engines                                            seed
===============  =================================================  ===========
``sql``          mysql, mariadb, postgres, clickhouse, oracle,      ``.sql``
                 mssql                                              (init dir)
``mongo``        mongodb                                            ``.js``
``object_store`` s3 (MinIO), gcs (fake-gcs), dynamodb               fixtures
``query_engine`` trino, presto (built-in ``tpch``)                  none
===============  =================================================  ===========

(iceberg needs a bespoke multi-service stack — REST catalog + object store +
compute — so it is intentionally *not* registered yet; it falls to a follow-up.)

Implements rungs 1–2 of the source ladder (testcontainer / freely-available
container) uniformly as a compose **service** — in this harness the *worker*
container connects to the source by hostname, so the source must be a compose
service, and "testcontainer available" reduces to "prefer the official image".
Rung 3 (DataForge, for cloud-only sources with no container) is a follow-up.

Grounding: the ``sql``/``mysql`` path is validated against atlan-mysql-app's
proven hand-written scaffold (parsed-YAML identical). ``object_store`` and
``query_engine`` templates are best-effort and should be validated on first
adoption; their secret bundles are not inferable, so those apps must declare
``secrets:`` in ``source.yaml``.

Usage (from a connector repo root)::

    # .github/e2e/source.yaml
    app: postgres
    kind: postgres
    # database/username/password/image/seed_file/secrets all optional

    python -m application_sdk.testing.e2e.source_scaffold
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass, fields
from importlib import resources
from pathlib import Path
from typing import Any

_CONFIG_DIR = ".github/e2e"
_COMPOSE_FILE = f"{_CONFIG_DIR}/e2e-full-docker-compose.yaml"
_SECRETS_FILE = f"{_CONFIG_DIR}/make-secrets-e2e-full.py"
_DEPLOYMENT_ENV = "ATLAN_DEPLOYMENT_NAME=e2e-full-ci-${GITHUB_RUN_ID}"
_HEALTHCHECK_TAIL: dict[str, Any] = {
    "interval": "5s",
    "timeout": "5s",
    "retries": 20,
    "start_period": "30s",
}


class UnsupportedSourceEngineError(ValueError):
    """Raised when ``source.yaml`` names an engine not in :data:`ENGINES`."""


class SecretsNotDeclaredError(ValueError):
    """Raised when a scaffold whose secret keys aren't inferable omits ``secrets``."""


@dataclass(frozen=True)
class SourceConfig:
    """A connector's hermetic-source declaration (from ``source.yaml``)."""

    app: str
    kind: str
    database: str = "e2e_main"
    username: str = "e2e_user"
    password: str = "e2e_pass"
    root_password: str = "root_e2e_pass"
    bucket: str = "e2e-bucket"
    region: str = "us-east-1"
    image: str | None = None
    seed_file: str | None = None  # per-app override; None ⇒ engine's canonical seed
    secrets: dict[str, str] | None = None  # override the generated bundle


@dataclass(frozen=True)
class Scaffold:
    """The rendered pieces for one source: compose services + wiring + secrets."""

    services: dict[str, Any]  # source service(s), merged under compose `services`
    depends_on: dict[str, str]  # {service: condition} the atlan-app override waits on
    secrets: dict[str, str]  # SDR_<APP>_* bundle written to credentials.json
    seed_file: str | None  # target filename under .github/e2e (None ⇒ no seed)


@dataclass(frozen=True)
class EngineSpec:
    """Per-engine knobs. ``build`` renders the source service(s) + wiring."""

    scaffold: str
    image: str
    build: Callable[[SourceConfig, str], Scaffold]
    seed_asset: str | None = None  # canonical seed shipped in seeds/ (None ⇒ none)
    testcontainers: bool = False  # an official testcontainers module backs the image


# ── secret bundles ────────────────────────────────────────────────────────────


def _basic_secrets(cfg: SourceConfig) -> dict[str, str]:
    """SDR_<APP>_USERNAME/PASSWORD — the grounded convention SQLAppE2ETest emits."""
    app = cfg.app.upper()
    return {f"SDR_{app}_USERNAME": cfg.username, f"SDR_{app}_PASSWORD": cfg.password}


def _require_declared_secrets(cfg: SourceConfig) -> dict[str, str]:
    if not cfg.secrets:
        raise SecretsNotDeclaredError(
            f"source kind {cfg.kind!r} has no inferable secret-key convention — "
            f"declare `secrets:` in source.yaml with the exact SDR_* ref-keys this "
            f"connector's agent-json expects (e.g. "
            f"{{SDR_{cfg.app.upper()}_ACCESS_KEY_ID: ..., "
            f"SDR_{cfg.app.upper()}_SECRET_ACCESS_KEY: ...}})."
        )
    return cfg.secrets


# ── helpers ─────────────────────────────────────────────────────────────────--


def _healthcheck(test: list[str]) -> dict[str, Any]:
    return {"test": test, **_HEALTHCHECK_TAIL}


def _out_seed(cfg: SourceConfig, default: str) -> str:
    """Filename the seed is materialised to under .github/e2e (override wins).

    The *content* comes from the engine's canonical SDK asset (e.g. mysql.sql);
    the *output* keeps the conventional generic name (seed.sql / seed.js) the
    harness + any pre-existing per-app override already use.
    """
    return cfg.seed_file or default


def _seed_volume(cfg: SourceConfig, init_dir: str, out_name: str) -> str:
    return f"${{GITHUB_WORKSPACE}}/{_CONFIG_DIR}/{out_name}:{init_dir}/{out_name}:ro"


# ── SQL engines (auto-init image: mounts a seed into the init dir) ──────────────


def _sql_auto_init(
    *,
    service: str,
    port: int,
    init_dir: str,
    env: Callable[[SourceConfig], dict[str, str]],
    healthcheck: Callable[[SourceConfig], list[str]],
) -> Callable[[SourceConfig, str], Scaffold]:
    def build(cfg: SourceConfig, image: str) -> Scaffold:
        out = _out_seed(cfg, "seed.sql")
        return Scaffold(
            services={
                service: {
                    "image": image,
                    "healthcheck": _healthcheck(healthcheck(cfg)),
                    "environment": env(cfg),
                    "volumes": [_seed_volume(cfg, init_dir, out)],
                    "expose": [str(port)],
                }
            },
            depends_on={service: "service_healthy"},
            secrets=cfg.secrets or _basic_secrets(cfg),
            seed_file=out,
        )

    return build


def _mssql_build(cfg: SourceConfig, image: str) -> Scaffold:
    # mcr mssql has no init-dir, so a mssql-tools sidecar applies the seed after
    # the server is healthy; atlan-app waits for that sidecar to complete.
    seed = _out_seed(cfg, "seed.sql")
    pw = cfg.root_password
    return Scaffold(
        services={
            "mssql": {
                "image": image,
                "environment": {"ACCEPT_EULA": "Y", "MSSQL_SA_PASSWORD": pw},
                "healthcheck": _healthcheck(
                    [
                        "CMD-SHELL",
                        f"/opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa "
                        f"-P {shlex.quote(pw)} -Q 'SELECT 1' || exit 1",
                    ]
                ),
                "expose": ["1433"],
            },
            "mssql-init": {
                "image": "mcr.microsoft.com/mssql-tools",
                "depends_on": {"mssql": {"condition": "service_healthy"}},
                "volumes": [
                    f"${{GITHUB_WORKSPACE}}/{_CONFIG_DIR}/{seed}:/seed/{seed}:ro"
                ],
                "entrypoint": [
                    "/bin/sh",
                    "-c",
                    f"/opt/mssql-tools/bin/sqlcmd -S mssql -U sa -P {shlex.quote(pw)} "
                    f"-i /seed/{seed}",
                ],
            },
        },
        depends_on={
            "mssql": "service_healthy",
            "mssql-init": "service_completed_successfully",
        },
        secrets=cfg.secrets or _basic_secrets(cfg),
        seed_file=seed,
    )


def _mongo_build(cfg: SourceConfig, image: str) -> Scaffold:
    seed = _out_seed(cfg, "seed.js")
    return Scaffold(
        services={
            "mongodb": {
                "image": image,
                "healthcheck": _healthcheck(
                    ["CMD", "mongosh", "--quiet", "--eval", "db.runCommand('ping').ok"]
                ),
                "environment": {
                    "MONGO_INITDB_ROOT_USERNAME": cfg.username,
                    "MONGO_INITDB_ROOT_PASSWORD": cfg.password,
                    "MONGO_INITDB_DATABASE": cfg.database,
                },
                "volumes": [_seed_volume(cfg, "/docker-entrypoint-initdb.d", seed)],
                "expose": ["27017"],
            }
        },
        depends_on={"mongodb": "service_healthy"},
        secrets=cfg.secrets or _basic_secrets(cfg),
        seed_file=seed,
    )


# ── object stores (fixtures uploaded by an init sidecar) ────────────────────────


def _minio_build(cfg: SourceConfig, image: str) -> Scaffold:
    return Scaffold(
        services={
            "minio": {
                "image": image,
                "command": "server /data",
                "environment": {
                    "MINIO_ROOT_USER": cfg.username,
                    "MINIO_ROOT_PASSWORD": cfg.password,
                },
                "healthcheck": _healthcheck(
                    [
                        "CMD-SHELL",
                        "mc ready local || curl -f http://localhost:9000/minio/health/live",
                    ]
                ),
                "expose": ["9000"],
            },
            "minio-init": {
                "image": "minio/mc",
                "depends_on": {"minio": {"condition": "service_healthy"}},
                "volumes": [
                    f"${{GITHUB_WORKSPACE}}/{_CONFIG_DIR}/fixtures:/fixtures:ro"
                ],
                "entrypoint": [
                    "/bin/sh",
                    "-c",
                    f"mc alias set local http://minio:9000 "
                    f"{shlex.quote(cfg.username)} {shlex.quote(cfg.password)} && "
                    f"mc mb -p local/{cfg.bucket} && "
                    f"mc cp --recursive /fixtures/ local/{cfg.bucket}/",
                ],
            },
        },
        depends_on={
            "minio": "service_healthy",
            "minio-init": "service_completed_successfully",
        },
        secrets=_require_declared_secrets(cfg),
        seed_file=None,  # fixtures dir, not a single seed file
    )


def _fake_gcs_build(cfg: SourceConfig, image: str) -> Scaffold:
    # No healthcheck: the fake-gcs-server image is a minimal Go binary with no
    # curl/wget/shell, so a CMD healthcheck can't run in-container (verified —
    # curl-based checks never go healthy). It boots near-instantly and serves
    # the fixtures dir as the bucket, so atlan-app waits on service_started.
    return Scaffold(
        services={
            "gcs": {
                "image": image,
                "command": "-scheme http -public-host gcs -data /data",
                "volumes": [
                    f"${{GITHUB_WORKSPACE}}/{_CONFIG_DIR}/fixtures:/data/{cfg.bucket}:ro"
                ],
                "expose": ["4443"],
            }
        },
        depends_on={"gcs": "service_started"},
        secrets=_require_declared_secrets(cfg),
        seed_file=None,
    )


def _dynamodb_build(cfg: SourceConfig, image: str) -> Scaffold:
    # dynamodb has no canonical SDK seed (no SQL/JS dialect): the connector author
    # MUST supply `.github/e2e/seed-dynamodb.sh` (aws-cli commands to create the
    # tables + put items). `seed_file=None` below signals "not an SDK-materialised
    # seed", so the compose references this script but render() won't write it —
    # the author provides it, same as the minio/gcs fixtures dir.
    seed = cfg.seed_file or "seed-dynamodb.sh"
    return Scaffold(
        services={
            "dynamodb": {
                "image": image,
                "command": "-jar DynamoDBLocal.jar -inMemory -sharedDb",
                "expose": ["8000"],
                "healthcheck": _healthcheck(
                    ["CMD-SHELL", "curl -f http://localhost:8000 || exit 1"]
                ),
            },
            "dynamodb-init": {
                "image": "amazon/aws-cli",
                "depends_on": {"dynamodb": {"condition": "service_healthy"}},
                "environment": {
                    "AWS_ACCESS_KEY_ID": "dummy",
                    "AWS_SECRET_ACCESS_KEY": "dummy",
                    "AWS_DEFAULT_REGION": cfg.region,
                },
                "volumes": [
                    f"${{GITHUB_WORKSPACE}}/{_CONFIG_DIR}/{seed}:/seed/{seed}:ro"
                ],
                "entrypoint": ["/bin/sh", f"/seed/{seed}"],
            },
        },
        depends_on={
            "dynamodb": "service_healthy",
            "dynamodb-init": "service_completed_successfully",
        },
        secrets=_require_declared_secrets(cfg),
        seed_file=None,
    )


# ── query engines (built-in catalogs; no seed) ─────────────────────────────────


def _query_engine_build(
    *, service: str, port: int, health_path: str
) -> Callable[[SourceConfig, str], Scaffold]:
    def build(cfg: SourceConfig, image: str) -> Scaffold:
        return Scaffold(
            services={
                service: {
                    "image": image,
                    "healthcheck": _healthcheck(
                        [
                            "CMD-SHELL",
                            f"curl -f http://localhost:{port}{health_path} || exit 1",
                        ]
                    ),
                    "expose": [str(port)],
                }
            },
            depends_on={service: "service_healthy"},
            secrets=_require_declared_secrets(cfg),
            seed_file=None,  # built-in tpch/tpcds catalog — hermetic, no seed
        )

    return build


# ── engine registry ────────────────────────────────────────────────────────────

ENGINES: dict[str, EngineSpec] = {
    "mysql": EngineSpec(
        scaffold="sql",
        image="mysql:8.0.40",
        seed_asset="mysql.sql",
        testcontainers=True,
        build=_sql_auto_init(
            service="mysql",
            port=3306,
            init_dir="/docker-entrypoint-initdb.d",
            env=lambda cfg: {
                "MYSQL_ROOT_PASSWORD": cfg.root_password,
                "MYSQL_DATABASE": cfg.database,
                "MYSQL_USER": cfg.username,
                "MYSQL_PASSWORD": cfg.password,
            },
            healthcheck=lambda cfg: [
                "CMD",
                "mysqladmin",
                "ping",
                "-h",
                "localhost",
                "-u",
                "root",
                "-p$$MYSQL_ROOT_PASSWORD",
            ],
        ),
    ),
    "mariadb": EngineSpec(
        scaffold="sql",
        image="mariadb:11",
        seed_asset="mysql.sql",  # MariaDB shares the MySQL dialect
        testcontainers=True,
        build=_sql_auto_init(
            service="mariadb",
            port=3306,
            init_dir="/docker-entrypoint-initdb.d",
            env=lambda cfg: {
                "MARIADB_ROOT_PASSWORD": cfg.root_password,
                "MARIADB_DATABASE": cfg.database,
                "MARIADB_USER": cfg.username,
                "MARIADB_PASSWORD": cfg.password,
            },
            healthcheck=lambda cfg: [
                "CMD",
                "healthcheck.sh",
                "--connect",
                "--innodb_initialized",
            ],
        ),
    ),
    "postgres": EngineSpec(
        scaffold="sql",
        image="postgres:16",
        seed_asset="postgres.sql",
        testcontainers=True,
        build=_sql_auto_init(
            service="postgres",
            port=5432,
            init_dir="/docker-entrypoint-initdb.d",
            env=lambda cfg: {
                "POSTGRES_DB": cfg.database,
                "POSTGRES_USER": cfg.username,
                "POSTGRES_PASSWORD": cfg.password,
            },
            healthcheck=lambda cfg: [
                "CMD-SHELL",
                "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB",
            ],
        ),
    ),
    "clickhouse": EngineSpec(
        scaffold="sql",
        image="clickhouse/clickhouse-server:24.3",
        seed_asset=None,  # canonical clickhouse seed TODO — provide .github/e2e/seed.sql
        testcontainers=True,
        build=_sql_auto_init(
            service="clickhouse",
            port=9000,
            init_dir="/docker-entrypoint-initdb.d",
            env=lambda cfg: {
                "CLICKHOUSE_DB": cfg.database,
                "CLICKHOUSE_USER": cfg.username,
                "CLICKHOUSE_PASSWORD": cfg.password,
            },
            healthcheck=lambda cfg: ["CMD", "clickhouse-client", "--query", "SELECT 1"],
        ),
    ),
    "oracle": EngineSpec(
        scaffold="sql",
        image="gvenzl/oracle-free:23-slim",
        seed_asset=None,  # canonical oracle seed TODO (PL/SQL) — provide seed.sql
        testcontainers=True,
        build=_sql_auto_init(
            service="oracle",
            port=1521,
            init_dir="/container-entrypoint-initdb.d",
            env=lambda cfg: {
                "ORACLE_PASSWORD": cfg.root_password,
                "APP_USER": cfg.username,
                "APP_USER_PASSWORD": cfg.password,
                "ORACLE_DATABASE": cfg.database,
            },
            healthcheck=lambda cfg: ["CMD", "healthcheck.sh"],
        ),
    ),
    "mssql": EngineSpec(
        scaffold="sql",
        image="mcr.microsoft.com/mssql/server:2022-latest",
        seed_asset=None,  # canonical mssql seed TODO (T-SQL) — provide seed.sql
        testcontainers=True,
        build=_mssql_build,
    ),
    "mongodb": EngineSpec(
        scaffold="mongo",
        image="mongo:7",
        seed_asset="mongo.js",
        testcontainers=True,
        build=_mongo_build,
    ),
    "s3": EngineSpec(
        scaffold="object_store",
        image="minio/minio",
        build=_minio_build,
    ),
    "gcs": EngineSpec(
        scaffold="object_store",
        image="fsouza/fake-gcs-server",
        build=_fake_gcs_build,
    ),
    "dynamodb": EngineSpec(
        scaffold="object_store",
        image="amazon/dynamodb-local",
        build=_dynamodb_build,
    ),
    "trino": EngineSpec(
        scaffold="query_engine",
        image="trinodb/trino:latest",
        testcontainers=True,
        build=_query_engine_build(service="trino", port=8080, health_path="/v1/info"),
    ),
    "presto": EngineSpec(
        scaffold="query_engine",
        image="prestodb/presto:latest",
        build=_query_engine_build(service="presto", port=8080, health_path="/v1/info"),
    ),
}


# ── per-app source map (the container-eligible fleet → engine) ──────────────────
# Codifies the 2026-07 audit so `source.yaml` can omit `kind` for a known app.
# Query-engine / object-store apps still need `secrets:` declared (see docstring).
APP_KIND: dict[str, str] = {
    "mysql": "mysql",
    "metabase": "postgres",  # metabase ships its own; here for completeness
    "postgres": "postgres",
    "cloudsqlpostgres": "postgres",
    "alloydbpostgres": "postgres",
    "mssql": "mssql",
    "clickhouse": "clickhouse",
    "oracle": "oracle",
    "mongodbatlas": "mongodb",
    "s3": "s3",
    "gcs": "gcs",
    "amazondynamodbassets": "dynamodb",
    "trino": "trino",
    "presto": "presto",
    # saphana / sapase / iceberg / dremio: no clean free container — DataForge
    # (rung 3) or bespoke multi-service; deliberately not mapped here.
}


# ── rendering ───────────────────────────────────────────────────────────────--


def _atlan_app_override(depends_on: dict[str, str]) -> dict[str, Any]:
    return {
        "depends_on": {svc: {"condition": cond} for svc, cond in depends_on.items()},
        # queue = atlan-{connector}-{deployment_name} = atlan-<app>-e2e-full-ci-<run_id>
        "environment": [_DEPLOYMENT_ENV],
    }


def _scaffold(cfg: SourceConfig) -> tuple[EngineSpec, Scaffold]:
    engine = ENGINES.get(cfg.kind)
    if engine is None:
        supported = ", ".join(sorted(ENGINES))
        raise UnsupportedSourceEngineError(
            f"unsupported source engine {cfg.kind!r}; supported: {supported}. "
            f"Cloud-only sources (redshift/bigquery/snowflake/…) have no container "
            f"— use the DataForge track (rung 3) instead."
        )
    image = cfg.image or engine.image
    return engine, engine.build(cfg, image)


def render_compose(cfg: SourceConfig) -> str:
    import yaml  # noqa: PLC0415 — cold path: only when scaffolding

    engine, scaffold = _scaffold(cfg)
    services = dict(scaffold.services)
    services["atlan-app"] = _atlan_app_override(scaffold.depends_on)
    header = (
        "# Generated by application_sdk.testing.e2e.source_scaffold — DO NOT EDIT.\n"
        "# Regenerate: python -m application_sdk.testing.e2e.source_scaffold\n"
        f"# {engine.scaffold} source for {cfg.app} ({cfg.kind}); the atlan-app\n"
        f"# override routes the worker onto atlan-{cfg.app}-e2e-full-ci-<run_id>.\n"
    )
    body = yaml.safe_dump({"services": services}, sort_keys=False, width=100)
    return header + body


def render_secrets_script(cfg: SourceConfig) -> str:
    import json  # noqa: PLC0415

    _, scaffold = _scaffold(cfg)
    bundle = json.dumps(scaffold.secrets, indent=4)
    return f'''"""Generated by application_sdk.testing.e2e.source_scaffold — DO NOT EDIT.

e2e-full secret bundle for the {cfg.app} connector. The CI worker resolves these
keys from the local.file Dapr secret store against this JSON — the same creds the
compose overlay sets on the {cfg.kind} source.
"""

from __future__ import annotations

import json
import os

out = {bundle}

os.makedirs(".github/e2e/secrets", exist_ok=True)
with open(".github/e2e/secrets/credentials.json", "w") as f:
    json.dump(out, f)
print("Wrote .github/e2e/secrets/credentials.json (e2e-full bundle)")
'''


def canonical_seed(kind: str) -> str | None:
    """Return the SDK-shipped canonical seed text for ``kind`` (or None).

    ``None`` for an unknown ``kind`` or an engine that ships no seed — matching
    the documented contract (no ``KeyError`` for a direct/future caller that
    hasn't gone through ``_scaffold``'s validation).
    """
    engine = ENGINES.get(kind)
    if engine is None or engine.seed_asset is None:
        return None
    return (
        resources.files("application_sdk.testing.e2e")
        .joinpath("seeds", engine.seed_asset)
        .read_text()
    )


def render(cfg: SourceConfig) -> dict[str, str]:
    """Return ``{relative_path: file_text}`` for the generated scaffold.

    Includes the canonical seed (materialised to ``.github/e2e/<seed>``) when the
    engine ships one and the app didn't override ``seed_file``.
    """
    _, scaffold = _scaffold(cfg)
    out = {
        _COMPOSE_FILE: render_compose(cfg),
        _SECRETS_FILE: render_secrets_script(cfg),
    }
    if scaffold.seed_file and cfg.seed_file is None:
        seed_text = canonical_seed(cfg.kind)
        if seed_text is not None:
            out[f"{_CONFIG_DIR}/{scaffold.seed_file}"] = seed_text
    return out


def load_config(path: Path) -> SourceConfig:
    """Parse ``source.yaml`` into a :class:`SourceConfig` (``kind`` may be omitted
    for an app in :data:`APP_KIND`)."""
    import yaml  # noqa: PLC0415

    raw = yaml.safe_load(path.read_text())
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if not data.get("app"):
        raise ValueError(f"{path}: missing required key 'app'.")
    data.setdefault("kind", APP_KIND.get(data["app"], ""))
    if not data["kind"]:
        raise ValueError(
            f"{path}: 'kind' not given and app {data['app']!r} is not in the "
            f"known-app map; set kind: to one of {sorted(ENGINES)}."
        )
    allowed = {f.name for f in field_names()}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(
            f"{path}: unknown key(s) {unknown}; allowed: {sorted(allowed)}"
        )
    return SourceConfig(**data)


def field_names() -> tuple[Any, ...]:
    return fields(SourceConfig)


def write_scaffold(cfg: SourceConfig, root: Path = Path(".")) -> list[Path]:
    """Render + write the scaffold under ``root``; return the written paths."""
    written: list[Path] = []
    for rel, text in render(cfg).items():
        out = root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        written.append(out)
    return written


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]) if argv else Path(".")
    config_path = root / _CONFIG_DIR / "source.yaml"
    if not config_path.is_file():
        sys.stderr.write(
            f"error: {config_path} not found — declare the hermetic source there "
            f"(see application_sdk.testing.e2e.source_scaffold docstring).\n"
        )
        return 1
    # UnsupportedSourceEngineError + SecretsNotDeclaredError (both ValueError
    # subclasses) and load_config's ValueErrors carry clear, actionable messages
    # — surface them as a clean stderr line + exit 1, not a raw traceback.
    try:
        cfg = load_config(config_path)
        written = write_scaffold(cfg, root)
        _, scaffold = _scaffold(cfg)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    for path in written:
        sys.stdout.write(f"wrote {path}\n")
    if (
        scaffold.seed_file
        and cfg.seed_file is None
        and canonical_seed(cfg.kind) is None
    ):
        sys.stdout.write(
            f"note: no canonical seed shipped for {cfg.kind!r} yet — provide "
            f"{_CONFIG_DIR}/{scaffold.seed_file} with the hermetic dataset.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
