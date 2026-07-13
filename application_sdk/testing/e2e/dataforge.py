"""DataForge integration for the full-DAG e2e harness — rung 3 (cloud sources).

Rungs 1-2 of the source ladder run the source as a local compose **container**
the CI worker reaches by hostname (see :mod:`source_scaffold`). Cloud-only
sources — Redshift, Snowflake, BigQuery, Databricks — have no container, so the
full-DAG e2e instead provisions a **live instance** through DataForge, an
internal Atlan provisioning service exposed as a REST API that stands up real
source infrastructure via Terraform, hands back connection artifacts, and
auto-expires the instance (``lifecycle_days``). Its base URL and credentials are
supplied at runtime via env vars (below) — this module hardcodes no endpoint.

This module wraps that API for the harness, mirroring the internal
provision-source flow:

    provision-or-reuse → poll to PROVISIONED → artifacts.data → DatabaseSpec

so a cloud connector's full-DAG test resolves its source the same way a
container connector does — via ``database_spec()`` — with the canonical seed
(``seeds/<engine>.sql``) applied to the live instance first.

Auth
----
DataForge authenticates with an OAuth **Bearer** session (SSO, device-code flow
— RFC 8628). There is no long-lived static API key. A human runs the device-code
flow **once** (the ``auth`` command prints a verification URL to approve in the
browser); that writes ``~/.dataforge/session.json`` (a rotating refresh token).
For CI, that ``session.json`` is stored as a repo secret and mounted to
``~/.dataforge/session.json``. This client only *reads* the session token — it
does not run the interactive flow unless asked. See the internal DataForge
developer docs for the endpoint + setup.

Config (env)
------------
* ``DATAFORGE_API_URL``   — base URL of the DataForge API (required; no default)
* ``DATAFORGE_SESSION``   — session JSON *contents* (CI secret); falls back to
  the file at ``DATAFORGE_SESSION_FILE`` (default ``~/.dataforge/session.json``)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e.payload import DatabaseSpec

logger = get_logger(__name__)

# No hardcoded endpoint: the DataForge base URL is internal and supplied at
# runtime via DATAFORGE_API_URL (this SDK is a public repo — see the
# public-repo hygiene rule; never bake internal hosts in).
_DEFAULT_SESSION_FILE = "~/.dataforge/session.json"
#: Terminal + in-flight resource states (per the DataForge resource lifecycle).
_READY = "PROVISIONED"
_FAILED = {"FAILED", "DELETED", "DELETING"}


class DataForgeError(RuntimeError):
    """A DataForge API / provisioning failure surfaced to the harness."""


class DataForgeAuthError(DataForgeError):
    """No usable DataForge session — the device-code flow must be run first."""


# A pluggable transport: (method, url, headers, body) -> (status, json_or_none).
# Real calls use urllib; tests inject a fake to exercise the flow offline.
Transport = Callable[[str, str, dict[str, str], bytes | None], "tuple[int, Any]"]


#: DataForge sits behind Cloudflare, which 403s (error 1010) the default
#: ``Python-urllib`` User-Agent as a bad-bot signature. Send an honest,
#: descriptive UA — verified accepted (a custom UA passes; no browser spoofing).
_USER_AGENT = "atlan-application-sdk-e2e/1.0 (+dataforge)"


def _urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, Any]:
    req = urllib.request.Request(
        url, data=body, method=method, headers={"User-Agent": _USER_AGENT, **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = {"error": raw.decode(errors="replace")[:300]}
        return exc.code, parsed


def _load_session() -> tuple[str, str, Path | None]:
    """Load ``(session_token, refresh_token, file_path)`` from the CI secret or
    the session file. ``file_path`` is where a refreshed session is written back
    (``None`` when the session came from the env, which can't be rewritten)."""
    inline = os.environ.get("DATAFORGE_SESSION")
    path: Path | None = None
    if inline:
        raw: str | None = inline
    else:
        path = Path(
            os.environ.get("DATAFORGE_SESSION_FILE", _DEFAULT_SESSION_FILE)
        ).expanduser()
        raw = path.read_text() if path.is_file() else None
        if raw is None:
            path = None
    if not raw:
        raise DataForgeAuthError(
            "No DataForge session found. Run the `auth` command once "
            "(approve the printed verification URL in the browser) to write "
            "~/.dataforge/session.json, then set DATAFORGE_SESSION (CI) or mount "
            "the file. See the internal DataForge developer docs."
        )
    try:
        d = json.loads(raw)
    except ValueError as exc:
        raise DataForgeAuthError(f"DataForge session is not valid JSON: {exc}") from exc
    token = d.get("session_token")
    if not token:
        raise DataForgeAuthError("DataForge session JSON has no 'session_token'.")
    return token, d.get("refresh_token", ""), path


@dataclass
class DataForgeClient:
    """Thin client over the DataForge resource-provisioning REST API.

    The session token is short-lived (~15 min), while a provisioning poll can
    run far longer — so a 401 mid-flight is expected and handled by exchanging
    the refresh token for a fresh session and retrying once (see :meth:`_call`).
    """

    api_url: str = field(
        default_factory=lambda: os.environ.get("DATAFORGE_API_URL", "").rstrip("/")
    )
    transport: Transport = _urllib_transport
    _token: str | None = None
    _refresh_token: str = ""
    _session_path: Path | None = None

    def _require_api_url(self) -> str:
        if not self.api_url:
            raise DataForgeError(
                "DATAFORGE_API_URL is not set — supply the DataForge API base URL "
                "at runtime (no endpoint is baked into this public SDK)."
            )
        return self.api_url

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            self._token, self._refresh_token, self._session_path = _load_session()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _refresh(self) -> bool:
        """Exchange the refresh token for a fresh session. Returns success."""
        if not self._refresh_token:
            return False
        status, data = self.transport(
            "POST",
            f"{self._require_api_url()}/auth/refresh",
            {"Content-Type": "application/json"},
            json.dumps({"refresh_token": self._refresh_token}).encode(),
        )
        if status != 200 or not data or not data.get("session_token"):
            return False
        self._token = data["session_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        if self._session_path is not None:
            try:  # best-effort: keep the on-disk session current for reuse
                _write_session(data, str(self._session_path))
            except OSError:
                pass
        return True

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self._require_api_url()}{path}"
        payload = json.dumps(body).encode() if body is not None else None
        status, data = self.transport(method, url, self._headers(), payload)
        if status == 401 and self._refresh():
            status, data = self.transport(method, url, self._headers(), payload)
        if status == 401:
            raise DataForgeAuthError(
                "DataForge returned 401 — session expired and refresh failed. "
                "Re-run the `auth` command to mint a new session."
            )
        if status >= 400:
            detail = (data or {}).get("error") or (data or {}).get("code") or data
            raise DataForgeError(f"{method} {path} → HTTP {status}: {detail}")
        return data

    # ── module discovery ──────────────────────────────────────────────────────

    def resolve_module_id(self, datasource: str, variant: str) -> str:
        """Resolve the module UUID for a datasource + deployment variant."""
        variants = self._call(
            "GET", f"/api/v1/modules/datasources/{datasource}/variants"
        )
        items = variants if isinstance(variants, list) else variants.get("variants", [])
        for v in items:
            if v.get("variant") == variant or v.get("name") == variant:
                mid = v.get("module_id") or v.get("id")
                if mid:
                    return mid
        available = sorted(v.get("variant") or v.get("name") or "?" for v in items)
        raise DataForgeError(
            f"No DataForge variant {variant!r} for datasource {datasource!r}; "
            f"available: {available}"
        )

    # ── resource lifecycle ─────────────────────────────────────────────────────

    def find_reusable(self, datasource: str) -> str | None:
        """Return the id of an existing PROVISIONED instance, or None."""
        resources = self._call("GET", f"/api/v1/resources?q={datasource}")
        items = resources if isinstance(resources, list) else resources.get("resources", [])
        for r in items:
            if r.get("status") == _READY:
                return r.get("id")
        return None

    def create(
        self,
        module_id: str,
        inputs: dict[str, Any],
        reason: str,
        lifecycle_days: int = 4,
        category: str = "development",
    ) -> str:
        if len(reason.strip()) < 10:
            raise DataForgeError("DataForge `reason` must be ≥10 chars (shown to approvers).")
        # category=development is the accurate bucket for e2e test infra (vs the
        # default `ai`). NOTE: provisioning is admin-approval-gated regardless of
        # category — a non-admin can't self-approve (skip_approval is role-gated),
        # so a DataForge admin approves once. This is why the harness provisions a
        # persistent instance and reuses it (find_reusable), rather than a fresh
        # admin-gated provision per run.
        resp = self._call(
            "POST",
            "/api/v1/resources",
            {
                "module_id": module_id,
                "inputs": inputs,
                "reason": reason,
                "lifecycle_enabled": True,
                "lifecycle_days": lifecycle_days,
                "skip_approval": True,
                "category": category,
            },
        )
        rid = (resp or {}).get("id")
        if not rid:
            raise DataForgeError(f"DataForge create returned no id: {resp}")
        return rid

    def poll(
        self,
        resource_id: str,
        *,
        timeout_s: int = 1800,
        interval_s: int = 30,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Poll until the resource is PROVISIONED; raise on FAILED/timeout."""
        deadline = now() + timeout_s
        while True:
            resource = self._call("GET", f"/api/v1/resources/{resource_id}")
            status = (resource or {}).get("status")
            if status == _READY:
                return resource
            if status in _FAILED:
                raise DataForgeError(
                    f"DataForge resource {resource_id} entered {status}: "
                    f"{(resource or {}).get('error', '')}"
                )
            if now() >= deadline:
                raise DataForgeError(
                    f"DataForge resource {resource_id} not PROVISIONED within "
                    f"{timeout_s}s (last status={status})."
                )
            logger.info("DataForge %s: %s — polling…", resource_id, status)
            sleep(interval_s)

    def destroy(self, resource_id: str) -> None:
        """Trigger async Terraform teardown (idempotent; 202 expected)."""
        try:
            self._call("DELETE", f"/api/v1/resources/{resource_id}")
        except DataForgeError as exc:
            logger.warning("DataForge destroy of %s failed (non-fatal): %s", resource_id, exc)

    # ── interactive auth (OAuth device-code, RFC 8628) ──────────────────────────

    def device_login(
        self,
        *,
        open_browser: bool = True,
        write_path: str = _DEFAULT_SESSION_FILE,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        echo: Callable[[str], None] = print,
    ) -> str:
        """Run the device-code flow and persist ``~/.dataforge/session.json``.

        The device endpoints are unauthenticated (no Bearer). Requests a
        user_code, prints the verification URL for the human to approve in the
        browser (Okta SSO), then polls until approved and writes the
        session/refresh tokens. Returns the path written.
        """
        hdr = {"Content-Type": "application/json"}
        status, start = self.transport(
            "POST", f"{self._require_api_url()}/auth/device/code", hdr, b"{}"
        )
        if status >= 400 or not start:
            raise DataForgeError(f"device/code start failed: HTTP {status} {start}")
        device_code = start["device_code"]
        url = start.get("verification_url_complete") or start.get("verification_url")
        echo(
            "\n============================================================\n"
            "  Authorize DataForge for this machine\n"
            f"  Visit:  {url}\n"
            f"  Code:   {start.get('user_code')}\n"
            "  (Complete Okta SSO + click Approve; polling…)\n"
            "============================================================\n"
        )
        if open_browser and url:
            try:
                import webbrowser  # noqa: PLC0415 — cold path, best-effort

                webbrowser.open(url)
            except Exception:  # noqa: BLE001,S110 — headless is fine; user has the URL
                pass
        interval = int(start.get("interval") or 5)
        deadline = now() + int(start.get("expires_in") or 600)
        body = json.dumps({"device_code": device_code}).encode()
        while now() < deadline:
            sleep(interval)
            st, data = self.transport(
                "POST", f"{self._require_api_url()}/auth/device/poll", hdr, body
            )
            if st == 200 and data and data.get("session_token"):
                return _write_session(data, write_path)
            err = (data or {}).get("error")
            if err in (None, "authorization_pending"):
                continue
            raise DataForgeError(f"device authorization failed: {err}")
        raise DataForgeError("device authorization timed out before approval")

    # ── high-level convenience ──────────────────────────────────────────────────

    def provision_or_reuse(
        self,
        *,
        datasource: str,
        variant: str,
        instance_name: str,
        reason: str,
        inputs: dict[str, Any] | None = None,
        lifecycle_days: int = 4,
        reuse: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        """Provision (or reuse) an instance and return ``(resource_id, creds)``.

        ``creds`` is the flat ``artifacts.data`` block (host / port / database /
        username / password) fetched on-demand — never cache it to disk.
        """
        if reuse:
            existing = self.find_reusable(datasource)
            if existing:
                logger.info("DataForge: reusing PROVISIONED %s %s", datasource, existing)
                resource = self._call("GET", f"/api/v1/resources/{existing}")
                return existing, _artifacts(resource)
        module_id = self.resolve_module_id(datasource, variant)
        merged = {"instance_name": instance_name, **(inputs or {})}
        rid = self.create(module_id, merged, reason, lifecycle_days)
        resource = self.poll(rid)
        return rid, _artifacts(resource)


def _artifacts(resource: dict[str, Any]) -> dict[str, Any]:
    data = ((resource or {}).get("artifacts") or {}).get("data")
    if not isinstance(data, dict) or not data:
        raise DataForgeError(
            f"DataForge resource {resource.get('id')!r} has no artifacts.data "
            f"connection block."
        )
    return data


def load_seed(engine: str) -> str:
    """Return the canonical DDL seed for a rung-3 engine (e.g. ``redshift``).

    Loaded straight from the SDK ``seeds/`` dir — independent of the container
    ``source_scaffold.ENGINES`` registry, since cloud sources have no compose
    service. Apply this to the DataForge-provisioned instance before extraction
    so asset counts match the container-source engines byte-for-byte.
    """
    try:
        return (
            resources.files("application_sdk.testing.e2e")
            .joinpath("seeds", f"{engine}.sql")
            .read_text()
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise DataForgeError(
            f"no canonical seed shipped for engine {engine!r} "
            f"(expected seeds/{engine}.sql)"
        ) from exc


def _write_session(tokens: dict[str, Any], path: str) -> str:
    """Persist the device-code token pair to ``path`` (mode 600). Returns path."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "session_token": tokens["session_token"],
                "refresh_token": tokens.get("refresh_token", ""),
            }
        )
    )
    p.chmod(0o600)
    return str(p)


def creds_to_database_spec(
    creds: dict[str, Any], *, connector_config_name: str = ""
) -> DatabaseSpec:
    """Map a DataForge ``artifacts.data`` block to a harness ``DatabaseSpec``.

    ``database`` rides in ``extra`` (a SQL client whose DB_CONFIG.required lists
    it reads it from ``credentials["extra"]["database"]`` — see build_agent_json).
    Accepts the common artifact key spellings DataForge emits.
    """

    def pick(*keys: str, default: Any = None) -> Any:
        for k in keys:
            if creds.get(k) not in (None, ""):
                return creds[k]
        return default

    database = pick("database", "dbname", "db")
    return DatabaseSpec(
        host=pick("host", "endpoint", "hostname", default=""),
        port=int(pick("port", default=5439)),  # Redshift default
        username=pick("username", "user", "master_username", default=""),
        password=pick("password", "master_password", default=""),
        extra={"database": database} if database else {},
        connector_config_name=connector_config_name,
    )


# ── cloud-engine registry + CLI ─────────────────────────────────────────────--
# engine → (DataForge datasource, module name/variant, connector-config).
# Module names verified against the live catalog (GET /modules/datasources/
# redshift/variants): "aws-redshift" (managed, ~$180/mo) and
# "aws-redshift-serverless" (pay-per-RPU). Serverless preferred: cheaper for
# short-lived e2e and can expose a public endpoint a GitHub-hosted CI worker
# can reach (managed sits inside a private VPC).
_ENGINES: dict[str, tuple[str, str, str]] = {
    "redshift": ("redshift", "aws-redshift-serverless", "atlan-connectors-redshift"),
}


def provision_source(
    engine: str,
    *,
    instance_name: str,
    reason: str,
    lifecycle_days: int = 4,
    reuse: bool = True,
    client: DataForgeClient | None = None,
) -> tuple[str, DatabaseSpec]:
    """Provision (or reuse) a cloud source for ``engine`` and return its DatabaseSpec.

    The connector's full-DAG test calls this from ``database_spec()`` — the
    cloud analog of pointing at a compose service. Apply :func:`load_seed`
    (``engine``) to the instance before extraction.
    """
    if engine not in _ENGINES:
        raise DataForgeError(
            f"engine {engine!r} not registered for DataForge; known: {sorted(_ENGINES)}"
        )
    datasource, variant, config_name = _ENGINES[engine]
    rid, creds = (client or DataForgeClient()).provision_or_reuse(
        datasource=datasource,
        variant=variant,
        instance_name=instance_name,
        reason=reason,
        lifecycle_days=lifecycle_days,
        reuse=reuse,
    )
    return rid, creds_to_database_spec(creds, connector_config_name=config_name)


def main(argv: list[str] | None = None, client: DataForgeClient | None = None) -> int:
    """CLI: ``provision <engine>`` / ``destroy <id>`` / ``verify``.

    ``provision`` writes SOURCE_* (+ masks the password) to ``$GITHUB_ENV`` for a
    CI step, and prints a non-secret summary. Password is never printed.
    """
    import argparse  # noqa: PLC0415 — cold path

    parser = argparse.ArgumentParser(prog="dataforge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_prov = sub.add_parser("provision")
    p_prov.add_argument("engine", choices=sorted(_ENGINES))
    p_prov.add_argument("--instance-name", required=True)
    p_prov.add_argument("--reason", required=True)
    p_prov.add_argument("--lifecycle-days", type=int, default=4)
    p_prov.add_argument("--no-reuse", action="store_true")
    p_del = sub.add_parser("destroy")
    p_del.add_argument("resource_id")
    sub.add_parser("verify")
    sub.add_parser("auth")
    args = parser.parse_args(argv)

    df = client or DataForgeClient()
    if args.cmd == "auth":
        path = df.device_login()
        print(f"DataForge session saved to {path}")  # noqa: T201 — CLI stdout
        return 0
    if args.cmd == "verify":
        df._call("GET", "/api/v1/modules")  # raises on auth/HTTP failure
        print("DataForge auth OK")  # noqa: T201 — CLI stdout
        return 0
    if args.cmd == "destroy":
        df.destroy(args.resource_id)
        print(f"destroy triggered for {args.resource_id}")  # noqa: T201 — CLI stdout
        return 0
    # provision
    rid, spec = provision_source(
        args.engine,
        instance_name=args.instance_name,
        reason=args.reason,
        lifecycle_days=args.lifecycle_days,
        reuse=not args.no_reuse,
        client=df,
    )
    # Emit only NON-secret connection info + the resource id (for teardown). The
    # password is deliberately never printed or written to disk/env here — the
    # e2e resolves it *in process* via provision_source() → DatabaseSpec.password
    # (held in memory by the pytest run), so a secret never crosses a log or an
    # env file. This SDK is a public repo; keep it that way.
    fields = {
        "SOURCE_HOST": spec.host,
        "SOURCE_PORT": str(spec.port),
        "SOURCE_DATABASE": spec.extra.get("database", ""),
        "SOURCE_USERNAME": spec.username,
        "DATAFORGE_RESOURCE_ID": rid,
    }
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a") as fh:
            fh.writelines(f"{k}={v}\n" for k, v in fields.items())
    print(  # noqa: T201 — CLI stdout, non-secret summary only
        f"provisioned {args.engine} resource={rid} "
        f"host={spec.host} port={spec.port} database={spec.extra.get('database')} "
        f"(password resolved in-process via provision_source(); not emitted)"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
