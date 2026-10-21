# Validation recipe

Read from the `api-server-consolidation-migration` skill, steps 4 to 6. This
sequence was run end to end for atlan-mysql-app #778 on application-sdk #4058
and common-app-server #121. Use synthetic or vaulted test credentials; never
commit, paste or log them.

## 1. App side: check, build, install alone, probe

From the app repo root, with `<sdk>` an application-sdk checkout at the commit
the app pins:

```bash
python3 <sdk>/.github/scripts/gen_app_api.py check
python3 <sdk>/.github/scripts/gen_app_api.py build --out /tmp/dist-api
uv venv /tmp/api-only
uv pip install --python /tmp/api-only /tmp/dist-api/*.whl httpx2==2.13.1
/tmp/api-only/bin/python <sdk>/.github/scripts/probe_app_api_member.py \
    --name <app> --package atlan_<app>_api
```

The built wheel is installed **alone**, with only `httpx2` (the test client's
transport, pinned as the `api-member` job pins it). Never install the full
SDK into that venv.

`check` fails on: an absolute `from app` import among the shipped files; a
worker-only import `fix` would move; any line matching `APPLICATION_NAME` /
`ATLAN_APPLICATION_NAME` (comments included); logging in the handler module
(P060); a `compute_manifest` hook in `app/*/core.py`.

`build` (and `stage --out DIR`, which the host uses) writes the closure, the
`data` files and `app/generated/` (`*.json`, `_input.py`) as
`atlan_<app>_api/`, with a generated `__init__.py` (`handler`,
`get_asgi_app()`) and `pyproject.toml` (entry points `atlan.app_api` and
`atlan.app_server`). The api requirement copies the git pin when there is one,
else is floored at the SDK version in `uv.lock`, keeping the app's upper bound.

The probe mounts the package exactly as the host does and fails when:

- the full `atlan-application-sdk` is installed (`application_sdk.main` is
  importable);
- the entry-point object is not a `Handler`, or `app.state.app_name` is not
  `<app>`;
- `GET /health` is not 200;
- a non-object body on `POST /workflows/v1/auth` is not 422;
- an empty-credentials auth call answers a bare 500 instead of a JSON envelope;
- with generated contract files: `GET /workflows/v1/configmaps` is not 200,
  `POST /manifest` is not 200 or still carries `{app_name}` /
  `{deployment_name}`, or `GET /workflows/v1/input-contract` answers 5xx;
- `GET /workflows/v1/config/...` with no object store does not answer 503;
- `/check` or `/metadata` with empty credentials answer a bare 500, or raise an
  `ImportError` (a worker-only import that only breaks on the host).

Pass: `api member atlan_<app>_api mounts as '<app>' and passes the host matrix`.

## 2. Host side: test image from overrides

In a common-app-server branch, with the app already listed in
`hosted-api-apps.toml` (see `routes-and-host.md`):

1. Put the unreleased commits in `hosted-api-apps.overrides.toml`: `rev` only,
   full SHAs, only for `[sdk]` or apps the release pins list.
2. Stage and probe:
   ```bash
   ATLAN_API_APPS_ALLOW_OVERRIDES=1 python scripts/stage_api_apps.py
   # relock (section 4), then:
   uv sync --locked
   python scripts/stage_api_apps.py probe --python .venv/bin/python
   uv run python -m pytest tests/unit -q
   ```
3. Build a non-production image with
   `docker build --build-arg ATLAN_API_APPS_ALLOW_OVERRIDES=1 ...`. Without the
   variable the stage script refuses an overrides file, and the
   `check-release-pins` CI job fails while the file exists, so it never merges.
4. Run the host locally against the full locked environment (every hosted app):
   ```bash
   uv run uvicorn common_api_server.main:app --port 8000
   H='Host: <app>.<app>-app.svc.cluster.local:8000'
   curl -s localhost:8000/server/ready                                  # <app> listed, failed empty
   curl -s -H "$H" -XPOST localhost:8000/workflows/v1/auth -d @creds.json
   curl -s -H "$H" -XPOST localhost:8000/workflows/v1/check -d @check.json
   curl -s -H "$H" -XPOST localhost:8000/workflows/v1/metadata -d @creds.json
   curl -s -H "$H" localhost:8000/openapi.json | jq -r .info.title      # must be <app>
   curl -s -H 'Host: nope.nope-app.svc.cluster.local' localhost:8000/x  # 503 app_not_hosted
   ```
   The log must say `Hosting N of N app server(s)`. Bad credentials return a
   typed 4xx that does not echo the secret; `/check` returns 200 with a
   verdict, never a 500.

## 3. Tenant test

Do not patch the tenant's live `common-api-server` Deployment: it serves other
apps. Run a throwaway pod from the test image in the app's namespace
(`<app>-app`, where its secret references resolve), with the host
Deployment's env **spec** copied (references, never values) and the host's
uvicorn entry point. Probe by Host header and compare status **and body
shape** with the app's own server; a 200 with empty data hides a broken
route. `kubectl logs <pod> | grep -c ModuleNotFoundError` must be 0. Delete
the pod afterwards and leave the tenant as you found it.

The tenant test must cover, through the host:

- test credentials;
- test and update an existing connection (auth, then
  `POST /workflows/v1/config/{guid}?type=credentials`);
- preflight;
- metadata (filter dropdowns);
- configmaps (setup form);
- `POST /manifest`, with the task queue stamped for the app's worker;
- a workflow run that scales the worker through KEDA and publishes assets.

## 4. Relock

A machine-wide uv config can rewrite every index URL in a lock to a
credentialed registry proxy that CI cannot reach (it happened once on a host
lock). Relock with that config bypassed, in the app (steps 4 and 7) and on the
host:

```bash
UV_NO_CONFIG=1 UV_DEFAULT_INDEX=https://pypi.org/simple uv lock
grep -o 'registry = "[^"]*"' uv.lock | sort -u   # must print only https://pypi.org/simple
```

Use the repo's own uv version. A lock diff of thousands of lines with every
source rewritten means the config was not bypassed; discard it and relock.
