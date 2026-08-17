"""Resolve one e2e matrix leg's tenant + credentials into ``$GITHUB_ENV``.

Why this exists
---------------
Before FND-6 the e2e job pinned a single tenant into its ``env:`` block from
four flat secrets (``SDR_TEST_TENANT``, ``SDR_CLIENT_ID``, ``SDR_CLIENT_SECRET``,
``ATLAN_API_KEY``), so every connector's e2e ran against one cloud and
CSP-specific behaviour — the objectstore binding ``atlan-configurator`` emits,
blobstorage proxy behaviour, Temporal host resolution — was never exercised
before release.

The matrix now carries a ``cloud`` per leg, but a ``strategy.matrix`` value
cannot index the ``secrets`` context, and ``tests-reusable.yaml`` declares its
``workflow_call`` secrets explicitly, so a per-cloud *name* per credential would
have to be declared (and re-declared in every reusable) for every cloud added.
Instead one secret carries the whole map — the same shape ``E2E_SOURCE_ENV_JSON``
already uses for per-connector source credentials::

    {
      "aws":   {"tenant": "…", "client_id": "…", "client_secret": "…", "api_key": "…"},
      "azure": {…},
      "gcp":   {…}
    }

and this script extracts exactly one cloud's entry. Adding a fourth CSP is a
secret edit; no workflow, app repo, or code change.

Least privilege by construction: only the requested cloud's entry is ever
rendered, so a leg never sees the other tenants' credentials at all.

Masking
-------
Registering ``E2E_TENANT_MATRIX_JSON`` as a secret does **not** redact the
values inside it — the runner's masker matches registered strings, not
substrings of one. The two-pass ``--mask-only``-then-write protocol and the
mask rendering itself are ``export_extra_env``'s, imported rather than
reimplemented so both call sites share one audited implementation (and
``test_every_env_write_call_site_masks_first`` polices the ordering)::

    python3 resolve_e2e_tenant.py --matrix-json "$E2E_TENANT_MATRIX_JSON" --cloud "$E2E_CLOUD" --mask-only
    python3 resolve_e2e_tenant.py --matrix-json "$E2E_TENANT_MATRIX_JSON" --cloud "$E2E_CLOUD" >> "$GITHUB_ENV"

Fallback
--------
An empty ``--matrix-json`` (secret not shared with this repo) or an empty
``--cloud`` (matrix leg with no cloud dimension) emits the ``--fallback-*``
values instead, so a caller that has not adopted the tenant matrix keeps the
pre-FND-6 single-tenant behaviour exactly. The fallback values come in as flags
rather than being read from the environment so that both modes see identical
inputs and the mask pass cannot diverge from the write pass.
"""

from __future__ import annotations

import argparse
import json
import sys

from export_extra_env import ExtraEnvError, render, render_masks

# The tenant entry's JSON keys, mapped to the environment variables the sdr-e2e
# action and the pytest harness read. ATLAN_BASE_URL is NOT here: it is derived
# from the tenant below rather than configured, so it can never name a different
# tenant than SDR_TEST_TENANT does.
_FIELD_TO_ENV = {
    "tenant": "SDR_TEST_TENANT",
    "client_id": "SDR_CLIENT_ID",
    "client_secret": "SDR_CLIENT_SECRET",
    "api_key": "ATLAN_API_KEY",
}

# Optional. Overrides BaseE2ETest.tenant_deployment_name, which resolves
# "{deployment_name}" when the harness addresses a tenant's system apps. Absent
# for a tenant that deploys them under the "production" default.
_DEPLOYMENT_NAME_FIELD = "deployment_name"
_DEPLOYMENT_NAME_ENV = "E2E_TENANT_DEPLOYMENT_NAME"

# Optional. The tenant's ID — its vcluster instance name ("markeznp37"), which is
# what GM matches `allowed_tenants` against when a release is scoped to specific
# tenants (heracles/handler/marketplace.go reads it from the atlan-defaults
# ConfigMap key `instance`; it is deliberately NOT in the JWT, where the realm is
# "default" for every tenant).
#
# Distinct from `tenant`, which is the HOSTNAME. Scoping a release with a hostname
# publishes successfully and produces a release visible to no tenant, so the
# install then fails with "version not found" — FND-31 lost three live runs to
# exactly that. Absent for a caller that never publishes.
_TENANT_ID_FIELD = "tenant_id"
_TENANT_ID_ENV = "E2E_TENANT_ID"

# Values excluded from ::add-mask::. Everything else is masked, including the
# tenant host and its derived base URL — those are secrets today and staying
# masked is not a change in posture.
#
# The deployment name is not a credential, and masking it would be actively
# harmful: it is a short common word ("production", "staging"), and the runner's
# masker does substring replacement, so registering it redacts every unrelated
# occurrence in the log — including the queue names an operator reads to work out
# where a leg's activities went.
_UNMASKED_ENV = frozenset({_DEPLOYMENT_NAME_ENV})


class TenantMatrixError(ValueError):
    """The tenant matrix payload or the requested cloud is not usable."""


def _entry(matrix_json: str, cloud: str) -> dict[str, str]:
    """Return the credential fields for *cloud* from the *matrix_json* map.

    Every error message names cloud KEYS only, never a value: these strings are
    printed to the CI log, and the values are credentials.
    """
    try:
        parsed = json.loads(matrix_json)
    except json.JSONDecodeError as exc:
        raise TenantMatrixError(
            f"E2E_TENANT_MATRIX_JSON is not valid JSON ({exc}). Expected an "
            'object of {"<cloud>": {"tenant": …, "client_id": …, '
            '"client_secret": …, "api_key": …}}.'
        ) from exc
    if not isinstance(parsed, dict):
        raise TenantMatrixError(
            "E2E_TENANT_MATRIX_JSON must be a JSON object keyed by cloud, got "
            f"{type(parsed).__name__}."
        )

    if cloud not in parsed:
        available = ", ".join(sorted(parsed)) or "none"
        raise TenantMatrixError(
            f"cloud {cloud!r} is not in E2E_TENANT_MATRIX_JSON (available: "
            f"{available}). A cloud named in the workflow's e2e-clouds input "
            "but missing from the secret is a coverage hole, not a leg to skip. "
            "Since FND-354 the DEFAULTED cloud list is narrowed to the secret's "
            "keys at discovery, so reaching this means either that this cloud "
            "was named explicitly (drop it from e2e-clouds, or add its entry to "
            "the secret) or that discovery could not read the secret's keys — "
            "in which case the discover job carries a ::warning:: saying so."
        )

    entry = parsed[cloud]
    if not isinstance(entry, dict):
        raise TenantMatrixError(
            f"E2E_TENANT_MATRIX_JSON[{cloud!r}] must be an object, got "
            f"{type(entry).__name__}."
        )

    missing = [
        field for field in _FIELD_TO_ENV if not str(entry.get(field, "") or "").strip()
    ]
    if missing:
        raise TenantMatrixError(
            f"E2E_TENANT_MATRIX_JSON[{cloud!r}] is missing or blank for: "
            f"{', '.join(missing)}."
        )

    resolved = {env: str(entry[field]) for field, env in _FIELD_TO_ENV.items()}
    deployment_name = str(entry.get(_DEPLOYMENT_NAME_FIELD, "") or "").strip()
    if deployment_name:
        resolved[_DEPLOYMENT_NAME_ENV] = deployment_name
    tenant_id = str(entry.get(_TENANT_ID_FIELD, "") or "").strip()
    if tenant_id:
        resolved[_TENANT_ID_ENV] = tenant_id
    return resolved


def resolve(
    matrix_json: str,
    cloud: str,
    fallback: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment map for one matrix leg.

    Takes the tenant matrix when both *matrix_json* and *cloud* are set, and the
    *fallback* single-tenant values otherwise. ``ATLAN_BASE_URL`` is derived from
    the resolved tenant in both paths, so it is always the same tenant as
    ``SDR_TEST_TENANT`` — the property the pre-FND-6 job-level ``env:`` block
    guaranteed by string-interpolating one from the other.
    """
    matrix_json = matrix_json.strip()
    cloud = cloud.strip()

    if matrix_json and cloud:
        resolved = _entry(matrix_json, cloud)
    else:
        resolved = {k: v for k, v in (fallback or {}).items() if v}

    tenant = resolved.get("SDR_TEST_TENANT", "").strip()
    if not tenant:
        raise TenantMatrixError(
            "no e2e tenant resolved: E2E_TENANT_MATRIX_JSON is unset (or this "
            "leg has no cloud) and the fallback SDR_TEST_TENANT is empty too. "
            "Share the tenant-matrix secret with this repo, or set the legacy "
            "single-tenant secrets."
        )
    resolved["ATLAN_BASE_URL"] = f"https://{tenant}"
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--matrix-json",
        default="",
        help="E2E_TENANT_MATRIX_JSON: cloud -> credential map. Empty = fallback.",
    )
    parser.add_argument(
        "--cloud",
        default="",
        help="Matrix leg's cloud key (aws/azure/gcp). Empty = fallback.",
    )
    parser.add_argument("--fallback-tenant", default="")
    parser.add_argument("--fallback-client-id", default="")
    parser.add_argument("--fallback-client-secret", default="")
    parser.add_argument("--fallback-api-key", default="")
    parser.add_argument(
        "--mask-only",
        action="store_true",
        help=(
            "Print only ::add-mask:: commands for the resolved values, and no "
            "$GITHUB_ENV lines. Run this first, with stdout going to the log, "
            "before the env-writing invocation redirects stdout into "
            "$GITHUB_ENV. See export_extra_env for why the two are separate."
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    fallback = {
        "SDR_TEST_TENANT": args.fallback_tenant,
        "SDR_CLIENT_ID": args.fallback_client_id,
        "SDR_CLIENT_SECRET": args.fallback_client_secret,
        "ATLAN_API_KEY": args.fallback_api_key,
    }

    try:
        resolved = resolve(args.matrix_json, args.cloud, fallback)
    except TenantMatrixError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    # Hand the rendering to export_extra_env so the heredoc form, the delimiter
    # collision guard, and the per-line mask registration are the same audited
    # code both call sites use. The mask pass sees a subset (see _UNMASKED_ENV);
    # the env pass always writes everything.
    try:
        if args.mask_only:
            secrets = {k: v for k, v in resolved.items() if k not in _UNMASKED_ENV}
            sys.stdout.write(render_masks(json.dumps(secrets)))
        else:
            sys.stdout.write(render(json.dumps(resolved)))
    except ExtraEnvError as exc:  # pragma: no cover - resolved names are ours
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
