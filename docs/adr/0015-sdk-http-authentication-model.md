# ADR-0015: SDK HTTP Authentication Model

## Status
**Accepted**

## Context

Every app built on the SDK runs a FastAPI handler service exposing workflow
start/status/config routes, Dapr subscription endpoints, optionally the
FastMCP streamable-HTTP mount (`ENABLE_MCP=true`), and FastAPI's default
OpenAPI documentation. The SDK implements **no HTTP authentication of its
own**: the platform's ingress (edge auth) and network policy protect the
pod.

That assumption was real but undocumented — tribal knowledge. A security
audit of the SDK flagged the consequences:

- The MCP mount exposed every `@mcp_tool` activity at the root path with
  nothing warning an operator who deploys outside the standard ingress
  posture.
- `/docs`, `/redoc`, and `/openapi.json` shipped enabled in production,
  handing an attacker the full route map for reconnaissance.
- The dev-only local-vault provisioning route was registered in every
  environment (guarded only by a runtime check).
- Unhandled exceptions could leak exception class names and messages to
  HTTP clients, and responses carried no hardening headers.

We needed to decide: should the SDK enforce HTTP authentication itself,
document edge-auth as the contract, or both?

## Decision

**Edge auth is the authentication layer; the SDK documents that contract
explicitly and tightens its own attack-surface defaults.** Concretely:

1. The deployment security model is now a written standard
   ([docs/standards/authentication.md](../standards/authentication.md)):
   platform ingress authenticates callers; network policy restricts pod
   reachability; the SDK does not duplicate that layer. Deployments
   outside the standard topology own their own edge authentication.
2. The MCP mount logs a startup warning whenever it is enabled, making
   the dependency on platform-level protection loud instead of implicit.
3. Attack-surface defaults tightened: OpenAPI endpoints return 404 unless
   `ATLAN_ENABLE_OPENAPI_DOCS=true` or running locally; dev-only routes
   are registered only in local environments; unhandled exceptions return
   a generic 500; security headers (`X-Frame-Options`,
   `X-Content-Type-Options`, `Referrer-Policy`) are set on all responses.

Defaults are chosen so that **upgrading the SDK alone never changes
deployment behavior** beyond these hardening defaults — both of which are
gated to keep local development frictionless.

## Options Considered

### Option 1: Mandatory SDK-level authentication (rejected)

Every route requires a token, full stop. Strongest posture on paper, but
it duplicates the platform's existing edge auth, forces a
token-distribution and rotation story onto every app deployment
immediately, and breaks every existing deployment on upgrade.

### Option 2: Opt-in SDK bearer-token middleware (considered, deferred)

An `ATLAN_HTTP_AUTH_ENABLED`-gated bearer middleware as defense-in-depth
for non-standard deployments. Prototyped during the audit and deliberately
**not shipped**: it adds a second credential to distribute and rotate, its
protection is redundant in the standard platform posture, and a static
shared token authenticates callers without authorizing users — easily
mistaken for more security than it provides. If a concrete deployment
topology emerges that needs an SDK-level backstop, this option can be
revisited with that topology's requirements in hand.

### Option 3: Documented edge-auth contract + tightened defaults (chosen)

Writes the real security model down and shrinks the SDK's own attack
surface, without growing new authentication machinery. The SDK's job is to
make the secure posture easy to state and verify — not to re-implement the
platform's auth layer.

## Consequences

**Positive:**
- The deployment security model is explicit and reviewable instead of
  tribal; new surfaces (like MCP) must state their auth posture.
- Recon surface shrinks by default: OpenAPI off in prod, dev routes
  absent, generic 500s, hardening headers on every response.
- No new credentials, env vars, or middleware to operate in the standard
  deployment.

**Negative:**
- Deployments outside the standard ingress posture have no SDK-provided
  backstop — they must front the app with their own auth layer (now an
  explicit, documented obligation).
- Anyone scripting against `/docs` in deployed environments must set
  `ATLAN_ENABLE_OPENAPI_DOCS=true` deliberately.

## Implementation

- `application_sdk/handler/service.py` — OpenAPI gating, local-only
  dev-route registration, MCP startup warning, generic 500 handler.
- `application_sdk/server/middleware/headers.py` —
  `SecurityHeadersMiddleware` (pure ASGI: `BaseHTTPMiddleware` buffers
  responses and would break the MCP streamable-HTTP mount).
- `docs/standards/authentication.md` — the deployment security model.
