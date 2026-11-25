# Local Temporal with JWT auth

A Temporal stack (PostgreSQL + server) that enforces RS256 JWT authentication.
`tests/integration/test_temporal_docker_auth.py` runs against it: token refresh in
`TemporalAuthManager`, and a reproducer of the worker crash when a token expires
without a refresh. The nightly `temporal-docker-auth` job in
`.github/workflows/schedule.yaml` runs the same steps as below.

## Prerequisites

- Docker with the Compose plugin
- `uv`

## 1. Generate keys

The key pair and JWKS are gitignored. Generate them after cloning:

```bash
bash local/temporal-auth/scripts/gen_keys.sh
```

## 2. Start the stack

```bash
docker compose -f local/temporal-auth/docker-compose.yml up -d --wait
```

`--wait` returns once the `default` namespace is registered. The frontend on
`localhost:7233` rejects any call without a valid token.

## 3. Run the tests

```bash
uv run pytest tests/integration/test_temporal_docker_auth.py -m integration -v
```

The tests skip when the stack is down. Set `TEMPORAL_DOCKER_AUTH_REQUIRED=1` to
make that a failure, as CI does.

## Generate tokens by hand

```bash
# Admin token (1 hour), for CLI ops
uv run python local/temporal-auth/scripts/gen_token.py --permissions temporal-system:admin

# SDK connection token (1 hour). default:write is required: the Temporal Python
# SDK calls DescribeNamespace (ActionRead) at startup as well as the poll RPCs
# (ActionWorker), and default:worker omits ActionRead.
uv run python local/temporal-auth/scripts/gen_token.py --permissions default:write

# Short-lived token (5 seconds), to trigger expiry
uv run python local/temporal-auth/scripts/gen_token.py --ttl 5 --permissions default:write
```

Pass a token to the SDK with `create_temporal_client(..., api_key=TOKEN)`, with
`ATLAN_WORKFLOW_HOST=localhost`, `ATLAN_WORKFLOW_PORT=7233` and
`ATLAN_WORKFLOW_TLS_ENABLED=false`.

## Stop the stack

```bash
docker compose -f local/temporal-auth/docker-compose.yml down -v
```
