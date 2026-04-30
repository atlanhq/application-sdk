# Custom App Frontends with Atlantis

The Application SDK handler serves a custom frontend at `GET /` when a compiled frontend bundle is present at `app/generated/frontend/static/index.html`. This lets apps ship their own configuration UI instead of relying on the platform's generic form renderer.

---

## How It Works

The handler checks `ATLAN_FRONTEND_ASSETS_PATH` (default: `app/generated/frontend/static`) at startup:

- If `index.html` exists there, it is served at `GET /`.
- The rest of the directory is mounted as static files (JS, CSS, assets).
- If the path does not exist or is empty, `GET /` returns a minimal placeholder JSON response.

Your frontend communicates with the handler via the standard SDK HTTP API (authentication, preflight, metadata, workflow start) at the same origin.

---

## Directory Layout

```
app/
├── frontend/               # Your frontend source (any framework)
│   ├── src/
│   ├── public/
│   └── package.json
└── generated/
    └── frontend/
        └── static/         # Build output — served at GET /
            ├── index.html
            ├── main.js
            └── assets/
```

Build your frontend into `app/generated/frontend/static/`. The `Dockerfile` should `COPY app/ app/` so the built bundle lands in the image.

---

## Framework Choice

Any frontend framework that produces static HTML/JS/CSS works:

- **React / Vite** — recommended for new apps
- **Vue / Nuxt (static export)**
- **Svelte / SvelteKit (static adapter)**
- **Plain HTML + vanilla JS** — simplest option for simple forms

The handler does not impose any framework constraints. It serves whatever is in the static directory.

---

## Calling the Handler API

Your frontend runs at the same origin as the handler (`http://localhost:8000` locally, `https://your-app.your-tenant.atlan.com` in production), so all requests are same-origin — no CORS configuration needed.

```javascript
// Test authentication
const resp = await fetch('/workflows/v1/auth', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    credentials: [
      { key: 'host', value: 'db.example.com' },
      { key: 'username', value: 'admin' },
      { key: 'password', value: 'secret' },
    ],
  }),
});
const result = await resp.json();
// result.data.status == "success" | "failed"

// Fetch metadata (databases / schemas)
const meta = await fetch('/workflows/v1/metadata', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ credentials: [...] }),
});
const { data } = await meta.json();
// data is an array of metadata objects (databases, schemas, etc.)

// Start a workflow
await fetch('/workflows/v1/start', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    credential_guid: '<guid>',
    connection: {
      connection_name: 'my-db',
      connection_qualified_name: 'default/postgres/1234',
    },
  }),
});
```

See the [HTTP API Reference](../reference/http-api.md) for the full endpoint list.

---

## Local Development

Start the handler in combined mode and open your browser:

```bash
# Terminal 1: handler + worker
DAPR_HTTP_PORT=3500 DAPR_GRPC_PORT=50001 \
  uv run application-sdk --mode combined --app app.connector:MyApp

# Terminal 2: frontend dev server (example: Vite)
cd app/frontend
npm run dev -- --port 5173 --proxy /workflows=http://localhost:8000
```

Use your framework's dev-server proxy to forward `/workflows/*` to the SDK handler at port 8000. This avoids rebuilding the frontend on every change during development.

---

## Overriding the Static Path

Change where the handler looks for the frontend bundle:

```bash
ATLAN_FRONTEND_ASSETS_PATH=dist/public
```

Or in the Dockerfile:

```dockerfile
ENV ATLAN_FRONTEND_ASSETS_PATH=app/generated/frontend/static
```
