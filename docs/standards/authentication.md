# Authentication & HTTP Security Posture

How an app built on the SDK is protected in a deployment, and which knobs
the SDK itself provides.

## Deployment security model

**Platform ingress (edge auth) is the authentication layer.** Apps deployed
on the Atlan platform sit behind the platform's ingress, which
authenticates and authorizes callers before traffic reaches the app pod.
Network policy restricts who can reach the pod at all. The SDK's HTTP
server does not duplicate that layer: it assumes requests that reach it
have already been authenticated at the edge.

This assumption is part of the deployment contract. Running an app outside
the standard ingress posture (standalone container, dev cluster, custom
topology) means *you* own putting an authentication layer in front of it.

## MCP exposure requirements

`ENABLE_MCP=true` mounts the FastMCP streamable-HTTP app on the same
server, **without SDK-level authentication**. Before enabling MCP in any
deployed environment, platform ingress / network policy must restrict
access to the MCP endpoints (the default platform posture).

Whenever MCP is enabled, the SDK logs a startup warning so the dependency
on platform-level protection is visible:
`MCP endpoints are exposed without SDK-level authentication; ensure
platform ingress/network policy restricts access to them.`

## Server hardening (always on)

- Response headers on every response: `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
- Unhandled exceptions return a generic
  `{"success": false, "message": "Internal server error"}` 500 — no stack
  traces or internals reach clients.
- OpenAPI endpoints (`/docs`, `/redoc`, `/openapi.json`) exist only in
  local development, unless `ATLAN_ENABLE_OPENAPI_DOCS=true`.
- The dev-only local-vault route (`POST /workflows/v1/dev/local-vault`) is
  registered only when `ATLAN_DEPLOYMENT_NAME` is unset/`local`.
- Atlan credential `base_url` / Temporal auth URLs must be `https`
  (plain `http` is allowed only for `localhost`/`127.0.0.1`).

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_ENABLE_OPENAPI_DOCS` | `false` | Expose `/docs`, `/redoc`, `/openapi.json` in deployed environments (always on in local dev). |
| `ATLAN_ALLOW_INSECURE_SSL` | `false` | Required to honor `insecureSSL: true` on an S3 Dapr binding; otherwise store creation fails with a config error. |

See `docs/configuration.md` for the full environment variable reference,
and [ADR-0015](../adr/0015-sdk-http-authentication-model.md) for the
decision record behind this model.
