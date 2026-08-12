"""Resolve a connector e2e SOURCE from dataforge and print its env map as JSON.

Why this exists
---------------
Connector e2e needs source credentials, and dataforge is where CI sources live:
either a *provisioned resource* (a postgres/mysql/… instance created once,
human-approved, ``category: "ci"`` so the lifecycle reaper skips it) or a
*managed credential* (a 1Password-backed vault entry — the only home for SaaS
sources like powerbi/snowflake that cannot be provisioned). Resolution is one
call: ``GET /api/v1/datasources/{ds}/credentials`` (pin an instance with
``resource_id`` / a vault entry with ``credential_id``).

Field contract: **verbatim pass-through — this script does not interpret
fields**. Every scalar the datasource carries is exported as
``E2E_<DS>_<FIELD>`` exactly as dataforge returns it (``host`` ->
``E2E_POSTGRES_HOST``, ``iam_role_arn`` -> ``E2E_POSTGRES_IAM_ROLE_ARN``,
``client_secret`` -> ``E2E_POWERBI_CLIENT_SECRET``), plus
``E2E_SOURCE_DATASOURCE`` (the workflow's source-selection breadcrumb) and
two JSON escape hatches (``E2E_SOURCE_RAW_JSON`` for the scalar map,
``E2E_SOURCE_EXTRA_JSON`` for nested values). Mapping fields onto connector
config is the CONNECTOR's job, in its own secrets script / resolver — that is
where its auth variants (basic, IAM role, key-pair, OAuth) already live, so
there is no central per-connector map to maintain in the SDK and no canonical
host/username/password shape imposed on sources that have neither. Field
*semantics* (which fields a datasource must carry) belong to dataforge's own
per-datasource curation profiles — the endpoint's ``mandatory_missing`` is
computed there, server-side.

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

Authentication is **GitHub OIDC only — no stored dataforge secret**: the
script requests the run's OIDC token with ``audience=dataforge`` (requires
``permissions: id-token: write``), exchanges it at ``POST /oauth/token``
(RFC 8693) for a 1-hour SERVICE token, and resolves via
``GET /api/v1/datasources/{ds}/credentials`` — the one REST path SERVICE
tokens are honored on. Requires a dataforge workload binding for the repo.
This makes the script CI-only by design; for local runs, use the connector's
in-test resolver with a personal dataforge me-key instead.

Tokens arrive via env vars, never argv (argv is visible in process listings
and step logs).
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
from typing import Any

DEFAULT_BASE_URL = "https://api.dataforge.atlan.dev"


class DataforgeSourceError(RuntimeError):
    """The requested source cannot be resolved into usable credentials."""


# ── HTTP (kept trivially monkeypatchable for tests) ──────────────────────────


def _request(method: str, url: str, api_key: str) -> dict:
    req = urllib.request.Request(
        url, method=method, headers={"Authorization": f"Bearer {api_key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        # Do NOT echo the response body: nothing upstream enforces that a
        # dataforge error payload can never embed a credential value, and this
        # write happens before any masking exists. Log only the status plus a
        # fixed, allowlisted failure class parsed from a known-safe field.
        raise DataforgeSourceError(
            f"dataforge {method} {url.split('?')[0]} failed: "
            f"HTTP {exc.code} ({_error_class(exc)})"
        ) from exc


# Allowlisted failure classes dataforge is known to return. Anything else —
# including any body field that could carry a credential value — is collapsed
# to "request_failed" so it can never reach stderr.
_KNOWN_ERROR_CLASSES = frozenset(
    {
        "vault_disabled",
        "vault_item_missing",
        "credential_not_found",
        "resource_not_found",
        "not_provisioned",
        "forbidden",
        "unauthorized",
        "bad_request",
        "not_found",
        "conflict",
        "rate_limited",
    }
)


def _error_class(exc: urllib.error.HTTPError) -> str:
    """Extract a safe, allowlisted failure class from an HTTP error body.

    Reads only a known-safe ``error``/``code`` field, matches it against an
    allowlist, and returns ``request_failed`` for anything unrecognized — so a
    credential-shaped value in an error body can never reach stderr.
    """
    try:
        body = exc.read().decode("utf-8", "replace")
        parsed = json.loads(body)
    except Exception:
        return "request_failed"
    candidate = ""
    if isinstance(parsed, dict):
        raw = parsed.get("error") or parsed.get("code") or ""
        candidate = str(raw).strip().lower()
    return candidate if candidate in _KNOWN_ERROR_CLASSES else "request_failed"


def _http_get(url: str, api_key: str) -> dict:
    return _request("GET", url, api_key)


# ── GitHub OIDC exchange (DATFORG-88) ─────────────────────────────────────────


def _github_oidc_token(audience: str = "dataforge") -> str:
    """Fetch this workflow run's OIDC token from the runner's token service."""
    req_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    req_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    if not req_url or not req_token:
        raise DataforgeSourceError(
            "GitHub OIDC is unavailable — the job needs `permissions: "
            "id-token: write`. This script is CI-only; for local runs use "
            "the connector's in-test resolver with a personal me-key."
        )
    sep = "&" if "?" in req_url else "?"
    req = urllib.request.Request(
        f"{req_url}{sep}audience={urllib.parse.quote(audience)}",
        headers={"Authorization": f"Bearer {req_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["value"]
    except urllib.error.HTTPError as exc:
        raise DataforgeSourceError(
            f"GitHub OIDC token request failed: HTTP {exc.code}"
        ) from exc
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        # URLError/timeout/OSError = the runner's token service is unreachable;
        # JSONDecodeError/KeyError = an unexpected response shape. Fixed,
        # body-free message: the response (or socket state) is never echoed.
        raise DataforgeSourceError(
            f"GitHub OIDC token request failed: {type(exc).__name__}"
        ) from exc


def _oauth_base_urls(base_url: str) -> list[str]:
    """Candidate hosts for /oauth/token, tried in order.

    CI runners reach the API host over the VPN (its IP whitelist admits the
    GP egress IPs) but the app host sits behind Cloudflare, which rejects
    datacenter egress even with the tunnel up. The API host only routes
    /oauth once dataforge#742 is deployed — so try it first and fall back to
    the app host on 404, which keeps the fetch working on both sides of that
    deploy (and on developer laptops, where the app host IS reachable).
    DATAFORGE_OAUTH_BASE_URL pins a single host explicitly.
    """
    override = os.environ.get("DATAFORGE_OAUTH_BASE_URL", "").strip()
    if override:
        return [override.rstrip("/")]
    candidates = [base_url]
    app_host = base_url.replace("://api.", "://", 1)
    if app_host != base_url:
        candidates.append(app_host)
    return candidates


def _exchange_for_service_token(base_url: str, oidc_token: str) -> str:
    """RFC 8693 token exchange: runner OIDC token -> 1h dataforge SERVICE token."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": oidc_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "scope": "credentials:read",
        }
    ).encode()
    last_exc: urllib.error.HTTPError | None = None
    for oauth_base in _oauth_base_urls(base_url):
        req = urllib.request.Request(
            f"{oauth_base}/oauth/token",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)["access_token"]
        except urllib.error.HTTPError as exc:
            last_exc = exc
            # 404 = this host doesn't route /oauth (ingress, not the grant
            # handler) — try the next candidate. Anything else is a real
            # answer from the exchange; surface it.
            if exc.code == 404:
                continue
            break
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            # URLError/timeout/OSError = the host is unreachable (VPN down,
            # DNS, refused); JSONDecodeError/KeyError = an unexpected
            # response shape. Fixed, body-free message — never echo the
            # response or socket state.
            raise DataforgeSourceError(
                f"dataforge token exchange failed: {type(exc).__name__} — is "
                "the VPN up and a workload binding registered for this repo "
                "(issuer token.actions.githubusercontent.com, subject "
                "repo:<org>/<repo>…)?"
            ) from exc
    assert last_exc is not None
    raise DataforgeSourceError(
        f"dataforge token exchange failed: HTTP {last_exc.code} — is a workload "
        "binding registered for this repo (issuer "
        "token.actions.githubusercontent.com, subject repo:<org>/<repo>…)?"
    ) from last_exc


def _validate_endpoint_doc(doc: Any, datasource: str) -> dict:
    """Narrow shape check on the datasource-credentials response.

    ``_http_get`` returns ``json.load`` unvalidated, so a 200 whose body is
    valid JSON of the wrong *shape* (a list, a scalar, a dict whose ``fields``
    is a list) would otherwise escape downstream as a raw
    ``AttributeError``/``TypeError`` traceback — which ``main()`` does not
    convert, so it prints unstructured under ``continue-on-error``. Re-raise
    shape violations as ``DataforgeSourceError`` with a fixed, body-free
    message (field values are never echoed). Deliberately narrow — not a broad
    ``except Exception`` — so programming errors still surface.
    """
    if not isinstance(doc, dict):
        raise DataforgeSourceError(
            f"datasource-credentials returned an unexpected response shape "
            f"for {datasource!r}"
        )
    fields_ok = isinstance(doc.get("fields", {}), dict)
    missing_raw = doc.get("mandatory_missing", [])
    missing_ok = isinstance(missing_raw, list) and all(
        isinstance(item, str) for item in missing_raw
    )
    resolved_ok = isinstance(doc.get("resolved_id"), (str, type(None)))
    if not (fields_ok and missing_ok and resolved_ok):
        raise DataforgeSourceError(
            f"datasource-credentials returned an unexpected response shape "
            f"for {datasource!r}"
        )
    return doc


def resolve_via_endpoint(
    base_url: str,
    bearer: str,
    datasource: str,
    env_tier: str = "",
    resource_id: str = "",
    credential_id: str = "",
) -> tuple[dict, str]:
    """Resolve through GET /api/v1/datasources/{ds}/credentials (one call).

    Pass resource_id (or credential_id) whenever available — a datasource can
    carry several instances and the pin is what makes CI deterministic.
    """
    params = []
    if env_tier:
        params.append(f"env={urllib.parse.quote(env_tier)}")
    if resource_id:
        params.append(f"resource_id={urllib.parse.quote(resource_id)}")
    if credential_id:
        params.append(f"credential_id={urllib.parse.quote(credential_id)}")
    url = f"{base_url}/api/v1/datasources/{urllib.parse.quote(datasource)}/credentials"
    if params:
        url += "?" + "&".join(params)
    doc = _validate_endpoint_doc(_http_get(url, bearer), datasource)
    fields = doc.get("fields") or {}
    if not fields:
        raise DataforgeSourceError(
            f"datasource-credentials returned no fields for {datasource!r}"
        )
    missing = doc.get("mandatory_missing") or []
    if missing:
        print(
            f"::warning::dataforge reports mandatory fields missing for "
            f"{datasource}: {', '.join(missing)}",
            file=sys.stderr,
        )
    return fields, str(doc.get("resolved_id") or "")


# ── Resolution ────────────────────────────────────────────────────────────────


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


def build_exports(
    raw: dict, datasource: str, output_prefix: str = ""
) -> dict[str, str]:
    """Verbatim pass-through: every raw scalar becomes E2E_<PREFIX>_<FIELD>.

    No canonical shape, no field interpretation — a basic-auth postgres
    exports E2E_POSTGRES_HOST/…/PASSWORD because those are its field names;
    an IAM-role source exports E2E_<DS>_IAM_ROLE_ARN/… because those are
    its. The connector's own secrets script maps them onto its config,
    where its auth variants already live.
    """
    scalars, extras = _flatten(raw)
    prefix = "E2E_" + re.sub(
        r"[^A-Za-z0-9]", "_", (output_prefix or datasource).upper()
    )
    exports: dict[str, str] = {}
    for key, value in scalars.items():
        exports[_env_name(prefix, key)] = value
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
    parser.add_argument("--output-prefix", default="")
    parser.add_argument(
        "--base-url", default=os.environ.get("DATAFORGE_BASE_URL", DEFAULT_BASE_URL)
    )
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    try:
        # OIDC only: exchange the run's identity for a 1h credentials:read
        # token and resolve via the one-call endpoint — no dataforge secret
        # stored anywhere.
        bearer = _exchange_for_service_token(base_url, _github_oidc_token())
        pin_resource = args.resource_id if args.mode == "resource" else ""
        pin_credential = args.resource_id if args.mode == "managed" else ""
        try:
            raw, resolved_id = resolve_via_endpoint(
                base_url,
                bearer,
                args.datasource,
                env_tier=args.env_tier,
                resource_id=pin_resource,
                credential_id=pin_credential,
            )
        except (OSError, json.JSONDecodeError) as exc:
            # URLError/timeout/OSError = unreachable mid-resolution;
            # JSONDecodeError = an unexpected response shape. Fixed,
            # body-free message — never echo the response or socket state.
            raise DataforgeSourceError(
                f"dataforge credential resolution failed: {type(exc).__name__}"
            ) from exc
        exports = build_exports(raw, args.datasource, args.output_prefix)
    except DataforgeSourceError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    # Non-secret breadcrumbs -> stderr (the log); the env map -> stdout ONLY,
    # for the caller's command substitution.
    print(
        f"Resolved dataforge source: datasource={args.datasource} "
        f"mode={args.mode} id={resolved_id} fields={len(raw)}",
        file=sys.stderr,
    )

    sys.stdout.write(json.dumps(exports, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
