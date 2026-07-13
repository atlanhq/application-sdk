"""Generate the hermetic-source CI scaffold for the full-DAG e2e harness.

The full-DAG (two-store SDR) e2e extracts from a **real source**. Today each
connector hand-writes three files under ``.github/e2e/``:

* ``e2e-full-docker-compose.yaml`` — a seeded source container + the
  ``atlan-app`` deployment-name override,
* ``make-secrets-e2e-full.py`` — writes the ``SDR_<APP>_*`` creds the agent
  resolves from its Dapr secret store,
* ``seed.sql`` — the connector's hermetic dataset.

The first two are ~95% boilerplate: the ``atlan-app`` override is identical
across apps, and the source service differs only by *engine* (image, port,
healthcheck, env). This module generates them from a small declaration
(``.github/e2e/source.yaml``) so per-app work collapses to **declaring the
source + writing seed.sql** — no bespoke compose/secrets per app.

It implements rungs 1–2 of the source ladder (testcontainer / freely-available
container) uniformly as a docker-compose service. Cloud-only sources with no
container (rung 3, DataForge) are out of scope here.

Usage (from a connector repo root, after writing ``.github/e2e/source.yaml``
and ``.github/e2e/seed.sql``)::

    python -m application_sdk.testing.e2e.source_scaffold

``source.yaml``::

    app: mysql            # connector short name → SDR_<APP>_* secret keys
    kind: mysql           # engine (see ENGINES)
    database: e2e_main
    username: e2e_user    # optional (default e2e_user)
    password: e2e_pass    # optional (default e2e_pass)
    root_password: root_e2e_pass  # optional; only engines with a root/admin pw
    image: mysql:8.0.40   # optional; override the engine default
    seed_file: seed.sql   # optional (default seed.sql)
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CONFIG_DIR = ".github/e2e"
_COMPOSE_FILE = f"{_CONFIG_DIR}/e2e-full-docker-compose.yaml"
_SECRETS_FILE = f"{_CONFIG_DIR}/make-secrets-e2e-full.py"


class UnsupportedSourceEngineError(ValueError):
    """Raised when ``source.yaml`` names an engine not in :data:`ENGINES`."""


@dataclass(frozen=True)
class SourceConfig:
    """A connector's hermetic-source declaration (from ``source.yaml``)."""

    app: str
    kind: str
    database: str
    username: str = "e2e_user"
    password: str = "e2e_pass"
    root_password: str = "root_e2e_pass"
    image: str | None = None
    seed_file: str = "seed.sql"


@dataclass(frozen=True)
class EngineSpec:
    """Per-engine knobs for the generated compose source service.

    ``env`` maps a :class:`SourceConfig` to the image's environment variables;
    keeping it a callable lets each engine name its own vars (``MYSQL_*`` vs
    ``POSTGRES_*``) without leaking engine specifics into the renderer.
    """

    service: str
    image: str
    port: int
    seed_mount: str
    healthcheck: list[str]
    env: Callable[[SourceConfig], dict[str, str]]


def _mysql_env(cfg: SourceConfig) -> dict[str, str]:
    return {
        "MYSQL_ROOT_PASSWORD": cfg.root_password,
        "MYSQL_DATABASE": cfg.database,
        "MYSQL_USER": cfg.username,
        "MYSQL_PASSWORD": cfg.password,
    }


def _postgres_env(cfg: SourceConfig) -> dict[str, str]:
    return {
        "POSTGRES_DB": cfg.database,
        "POSTGRES_USER": cfg.username,
        "POSTGRES_PASSWORD": cfg.password,
    }


def _mariadb_env(cfg: SourceConfig) -> dict[str, str]:
    return {
        "MARIADB_ROOT_PASSWORD": cfg.root_password,
        "MARIADB_DATABASE": cfg.database,
        "MARIADB_USER": cfg.username,
        "MARIADB_PASSWORD": cfg.password,
    }


# Rungs 1–2 of the source ladder as compose services. Each entry is a public
# image that seeds an init script from a mounted volume; add engines here as
# containerizable connectors adopt the full-DAG e2e (the renderer never changes).
ENGINES: dict[str, EngineSpec] = {
    "mysql": EngineSpec(
        service="mysql",
        image="mysql:8.0.40",
        port=3306,
        seed_mount="/docker-entrypoint-initdb.d/seed.sql",
        healthcheck=[
            "CMD",
            "mysqladmin",
            "ping",
            "-h",
            "localhost",
            "-u",
            "root",
            "-p$$MYSQL_ROOT_PASSWORD",
        ],
        env=_mysql_env,
    ),
    "mariadb": EngineSpec(
        service="mariadb",
        image="mariadb:11",
        port=3306,
        seed_mount="/docker-entrypoint-initdb.d/seed.sql",
        healthcheck=["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"],
        env=_mariadb_env,
    ),
    "postgres": EngineSpec(
        service="postgres",
        image="postgres:16",
        port=5432,
        seed_mount="/docker-entrypoint-initdb.d/seed.sql",
        healthcheck=["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"],
        env=_postgres_env,
    ),
    "clickhouse": EngineSpec(
        service="clickhouse",
        image="clickhouse/clickhouse-server:24.3",
        port=9000,
        seed_mount="/docker-entrypoint-initdb.d/seed.sql",
        healthcheck=[
            "CMD",
            "clickhouse-client",
            "--query",
            "SELECT 1",
        ],
        env=lambda cfg: {
            "CLICKHOUSE_DB": cfg.database,
            "CLICKHOUSE_USER": cfg.username,
            "CLICKHOUSE_PASSWORD": cfg.password,
        },
    ),
}


def _render_compose(cfg: SourceConfig, eng: EngineSpec) -> str:
    image = cfg.image or eng.image
    env_block = "\n".join(
        f"      {key}: {value}" for key, value in eng.env(cfg).items()
    )
    healthcheck = ", ".join(f'"{part}"' for part in eng.healthcheck)
    return f"""# Generated by application_sdk.testing.e2e.source_scaffold — DO NOT EDIT.
# Regenerate: python -m application_sdk.testing.e2e.source_scaffold
#
# e2e-full compose overlay: a hermetic {cfg.kind} source seeded from
# .github/e2e/{cfg.seed_file}, plus the atlan-app deployment-name override so the
# worker registers on a unique per-run Temporal queue
# (atlan-{cfg.app}-e2e-full-ci-<run_id>) that AE's agent-json routing targets.
# The objectstore component + ATLAN_AUTH_* are provided by the SDK CI base layer.
services:
  {eng.service}:
    image: {image}
    healthcheck:
      test: [{healthcheck}]
      interval: 5s
      timeout: 5s
      retries: 20
      start_period: 30s
    environment:
{env_block}
    volumes:
      - ${{GITHUB_WORKSPACE}}/.github/e2e/{cfg.seed_file}:{eng.seed_mount}:ro
    expose:
      - "{eng.port}"

  atlan-app:
    depends_on:
      {eng.service}:
        condition: service_healthy
    environment:
      # queue = atlan-{{connector}}-{{deployment_name}} = atlan-{cfg.app}-e2e-full-ci-<run_id>
      - ATLAN_DEPLOYMENT_NAME=e2e-full-ci-${{GITHUB_RUN_ID}}
"""


def _render_secrets_script(cfg: SourceConfig) -> str:
    app_upper = cfg.app.upper()
    return f'''"""Generated by application_sdk.testing.e2e.source_scaffold — DO NOT EDIT.

e2e-full secret bundle for the {cfg.app} connector. The CI worker resolves
SDR_{app_upper}_USERNAME / SDR_{app_upper}_PASSWORD from the local.file Dapr
secret store against this JSON — the same creds the compose overlay sets on the
{cfg.kind} container and that seed.sql grants.
"""

from __future__ import annotations

import json
import os

out = {{
    "SDR_{app_upper}_USERNAME": "{cfg.username}",
    "SDR_{app_upper}_PASSWORD": "{cfg.password}",
}}

os.makedirs(".github/e2e/secrets", exist_ok=True)
with open(".github/e2e/secrets/credentials.json", "w") as f:
    json.dump(out, f)
print("Wrote .github/e2e/secrets/credentials.json (e2e-full bundle)")
'''


def render(cfg: SourceConfig) -> dict[str, str]:
    """Return ``{relative_path: file_text}`` for the generated scaffold."""
    engine = ENGINES.get(cfg.kind)
    if engine is None:
        supported = ", ".join(sorted(ENGINES))
        raise UnsupportedSourceEngineError(
            f"unsupported source engine {cfg.kind!r}; supported: {supported}. "
            f"Cloud-only sources (redshift/bigquery/snowflake/…) have no "
            f"container — use the DataForge track instead."
        )
    return {
        _COMPOSE_FILE: _render_compose(cfg, engine),
        _SECRETS_FILE: _render_secrets_script(cfg),
    }


def load_config(path: Path) -> SourceConfig:
    """Parse a ``source.yaml`` declaration into a :class:`SourceConfig`."""
    import yaml  # noqa: PLC0415 — cold path: only when scaffolding

    raw = yaml.safe_load(path.read_text())
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    missing = [k for k in ("app", "kind", "database") if not data.get(k)]
    if missing:
        raise ValueError(
            f"{path}: missing required key(s) {missing}; need at least "
            f"app, kind, database."
        )
    fields = {
        f.name
        for f in SourceConfig.__dataclass_fields__.values()  # type: ignore[attr-defined]
    }
    unknown = sorted(set(data) - fields)
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {unknown}; allowed: {sorted(fields)}")
    return SourceConfig(**data)


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
    cfg = load_config(config_path)
    for path in write_scaffold(cfg, root):
        sys.stdout.write(f"wrote {path}\n")
    sys.stdout.write(
        f"scaffold ready for {cfg.app} ({cfg.kind}); provide "
        f"{_CONFIG_DIR}/{cfg.seed_file} with the hermetic dataset.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
