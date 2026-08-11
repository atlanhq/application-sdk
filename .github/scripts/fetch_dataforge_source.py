"""Resolve a connector e2e SOURCE from dataforge and print its env map as JSON.

Why this exists
---------------
Connector e2e needs source credentials, and dataforge is where CI sources live:
either a *provisioned resource* (a postgres/mysql/… instance created once,
human-approved, ``category: "ci"`` so the lifecycle reaper skips it) or a
*managed credential* (a 1Password-backed vault entry — the only home for SaaS
sources like powerbi/snowflake that cannot be provisioned). Dataforge has no
source-name-keyed one-shot credentials endpoint, so this script implements the
two lookups:

* ``resource`` mode — ``GET /api/v1/resources/{id}`` -> ``artifacts.data``
  (plaintext is only served for non-aisdlc categories).
* ``managed`` mode — ``POST /api/v1/managed-credentials/{id}/reveal``, or when
  no id is pinned, ``GET /api/v1/managed-credentials?datasource=X`` filtered
  client-side to ``LifecycleStatus == "active"`` (the list endpoint does NOT
  exclude decommissioned rows) preferring ``TestStatus == "passing"``, then
  reveal. List items are PascalCase (the Go entity has no json tags);
  ``RevealResult.fields`` is a flat, vault-controlled, free-form map.

Raw field names vary per source (SQL modules emit host/port/database/username/
password; snowflake emits account/warehouse/…; powerbi emits tenant_id/
client_id/client_secret), so the raw map is normalized against
``dataforge_field_maps.json`` into a canonical ``E2E_SOURCE_*`` set plus
prefixed per-source aliases (``E2E_POSTGRES_HOST``, …) and two JSON escape
hatches (``E2E_SOURCE_RAW_JSON`` / ``E2E_SOURCE_EXTRA_JSON``).

Security contract
-----------------
This script performs NO masking and NO ``$GITHUB_ENV`` writes. It prints the
env map as one JSON object on stdout and everything else on stderr, so the
call site captures stdout into a shell variable (values never reach the log)
and hands it to ``export_extra_env.py`` — whose mask-first two-pass protocol
is the audited path for turning a JSON env map into redacted job env::

    DF_ENV_JSON="$(python3 .../fetch_dataforge_source.py --datasource X ...)"
    python3 .../export_extra_env.py \
      --json "$DF_ENV_JSON" --mask-only
    python3 .../export_extra_env.py \
      --json "$DF_ENV_JSON" >> "$GITHUB_ENV"

The dataforge API key arrives via the ``DATAFORGE_API_KEY`` env var, never
argv (argv is visible in process listings and step logs).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.dataforge.atlan.dev"
_FIELD_MAPS_PATH = Path(__file__).parent / "dataforge_field_maps.json"

# The five concepts almost every connector's secrets-script / harness reads.
_CANONICAL_KEYS = ("host", "port", "database", "username", "password")


class DataforgeSourceError(RuntimeError):
    """The requested source cannot be resolved into usable credentials."""


# ── HTTP (kept trivially monkeypatchable for tests) ──────────────────────────


def _request(method: str, url: str, api_key: str) -> dict:
    req = urllib.request.Request(
        url, method=method, headers={"Authorization": f"Bearer {api_key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — https API host from trusted config
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        # Error bodies name the failure class (vault_disabled,
        # vault_item_missing, …) and never contain credential values.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: S110 — pragma: no cover - best-effort detail
            pass
        raise DataforgeSourceError(
            f"dataforge {method} {url.split('?')[0]} failed: HTTP {exc.code} {detail}"
        ) from exc


def _http_get(url: str, api_key: str) -> dict:
    return _request("GET", url, api_key)


def _http_post(url: str, api_key: str) -> dict:
    return _request("POST", url, api_key)


# ── Resolution ────────────────────────────────────────────────────────────────


def resolve_resource(base_url: str, api_key: str, resource_id: str) -> dict:
    """Return the raw connection map for a provisioned resource."""
    doc = _http_get(f"{base_url}/api/v1/resources/{resource_id}", api_key)
    status = doc.get("status", "UNKNOWN")
    if status != "PROVISIONED":
        raise DataforgeSourceError(
            f"dataforge resource {resource_id} is {status}, not PROVISIONED — "
            "resume it (POST /api/v1/resources/{id}/resume) or re-provision "
            "before running e2e"
        )
    data = (doc.get("artifacts") or {}).get("data") or {}
    if not data:
        raise DataforgeSourceError(
            f"dataforge resource {resource_id} returned no plaintext artifacts "
            "— aisdlc-category resources are vault-only by design; the e2e "
            "resource must be provisioned with category 'ci'"
        )
    return data


def resolve_managed(
    base_url: str,
    api_key: str,
    datasource: str,
    credential_id: str = "",
    env_tier: str = "",
    instance_name: str = "",
) -> tuple[dict, str]:
    """Return (raw field map, credential id) for a vault-managed credential."""
    if not credential_id:
        query = f"datasource={urllib.parse.quote(datasource)}"
        if env_tier:
            query += f"&env={urllib.parse.quote(env_tier)}"
        listing = _http_get(f"{base_url}/api/v1/managed-credentials?{query}", api_key)
        # PascalCase keys: entity.ManagedCredential carries no json tags.
        rows = [
            row
            for row in listing.get("items") or []
            if row.get("LifecycleStatus") == "active"
            and (not instance_name or row.get("InstanceName") == instance_name)
        ]
        if not rows:
            raise DataforgeSourceError(
                f"no active managed credential in dataforge for datasource="
                f"{datasource!r}"
                + (f" env={env_tier!r}" if env_tier else "")
                + (f" instance={instance_name!r}" if instance_name else "")
                + " — add one in the dataforge vault or pass a credential UUID"
            )
        # Prefer entries whose last connectivity test passed; never fail the
        # lookup over TestStatus alone (untested is common for fresh entries).
        rows.sort(key=lambda row: 0 if row.get("TestStatus") == "passing" else 1)
        credential_id = rows[0]["ID"]

    reveal = _http_post(
        f"{base_url}/api/v1/managed-credentials/{credential_id}/reveal", api_key
    )
    fields = reveal.get("fields") or {}
    if not fields:
        raise DataforgeSourceError(
            f"managed credential {credential_id} revealed no fields — the vault "
            "item may be orphaned; check dataforge's managed-credentials page"
        )
    return fields, credential_id


# ── Normalization ─────────────────────────────────────────────────────────────


def _flatten(raw: dict) -> tuple[dict[str, str], dict[str, Any]]:
    """Split *raw* into scalar fields and non-scalar extras."""
    scalars: dict[str, str] = {}
    extras: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, (str, int, float, bool)):
            scalars[key] = str(value)
        elif value is None:
            scalars[key] = ""
        else:
            extras[key] = value
    return scalars, extras


def _env_name(prefix: str, key: str) -> str:
    return f"{prefix}_{re.sub(r'[^A-Za-z0-9]', '_', key).upper()}"


def load_field_maps(override_json: str = "") -> dict:
    maps = json.loads(_FIELD_MAPS_PATH.read_text())
    if override_json.strip():
        override = json.loads(override_json)
        if not isinstance(override, dict):
            raise DataforgeSourceError("--field-map must be a JSON object")
        maps["_override"] = override
    return maps


def normalize(
    raw: dict, datasource: str, field_maps: dict, output_prefix: str = ""
) -> dict[str, str]:
    """Build the flat env map: canonical + prefixed aliases + JSON escape hatches."""
    scalars, extras = _flatten(raw)
    prefix = "E2E_" + re.sub(
        r"[^A-Za-z0-9]", "_", (output_prefix or datasource).upper()
    )

    # Candidate lists: per-repo override > per-datasource entry > _default.
    profile: dict[str, list[str]] = dict(field_maps.get("_default", {}))
    profile.update(field_maps.get(datasource, {}))
    profile.update(field_maps.get("_override", {}))

    lowered = {key.lower(): value for key, value in scalars.items()}
    exports: dict[str, str] = {}
    for canonical in _CANONICAL_KEYS:
        value = ""
        for candidate in profile.get(canonical, [canonical]):
            if lowered.get(candidate.lower(), "") != "":
                value = lowered[candidate.lower()]
                break
        exports[f"E2E_SOURCE_{canonical.upper()}"] = value
        exports[_env_name(prefix, canonical)] = value

    # Prefixed alias for every raw scalar so connectors can read source-shaped
    # names directly (E2E_POWERBI_TENANT_ID, E2E_SNOWFLAKE_WAREHOUSE, …).
    for key, value in scalars.items():
        exports.setdefault(_env_name(prefix, key), value)

    exports["E2E_SOURCE_DATASOURCE"] = datasource
    exports["E2E_SOURCE_RAW_JSON"] = json.dumps(scalars, sort_keys=True)
    exports["E2E_SOURCE_EXTRA_JSON"] = json.dumps(extras, sort_keys=True)
    return exports


# ── Entry point ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasource", required=True)
    parser.add_argument("--mode", choices=("resource", "managed"), default="resource")
    parser.add_argument("--resource-id", default="")
    parser.add_argument("--env-tier", default="")
    parser.add_argument("--instance-name", default="")
    parser.add_argument("--output-prefix", default="")
    parser.add_argument("--field-map", default="")
    parser.add_argument(
        "--base-url", default=os.environ.get("DATAFORGE_BASE_URL", DEFAULT_BASE_URL)
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("DATAFORGE_API_KEY", "").strip()
    if not api_key:
        print("::error::DATAFORGE_API_KEY is not set", file=sys.stderr)
        return 1

    base_url = args.base_url.rstrip("/")
    try:
        if args.mode == "resource":
            if not args.resource_id:
                raise DataforgeSourceError(
                    "resource mode needs --resource-id (dataforge has no "
                    "name-keyed resource lookup; pin the UUID in the "
                    "DATAFORGE_RESOURCE_ID secret)"
                )
            raw = resolve_resource(base_url, api_key, args.resource_id)
            resolved_id = args.resource_id
        else:
            raw, resolved_id = resolve_managed(
                base_url,
                api_key,
                args.datasource,
                credential_id=args.resource_id,
                env_tier=args.env_tier,
                instance_name=args.instance_name,
            )
        exports = normalize(
            raw, args.datasource, load_field_maps(args.field_map), args.output_prefix
        )
    except DataforgeSourceError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    # Non-secret breadcrumbs -> stderr (the log); the env map -> stdout ONLY,
    # for the caller's command substitution. resolved-id is a UUID, not a
    # secret, so a single-line $GITHUB_OUTPUT write is safe.
    print(
        f"Resolved dataforge source: datasource={args.datasource} "
        f"mode={args.mode} id={resolved_id} fields={len(raw)}",
        file=sys.stderr,
    )
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(f"resolved-id={resolved_id}\n")

    sys.stdout.write(json.dumps(exports, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
