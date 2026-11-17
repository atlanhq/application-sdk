# Live-cluster e2e checks (manual)

These tests read a real Kubernetes API server and a real Temporal frontend. They
need VPN plus a logged-in `kubectl` context, which no GitHub-hosted runner has,
so **no workflow runs them**. Run them by hand when you change the code they
cover. CI only collects them (the `Test Tree Collection` job in
`.github/workflows/sdk-tests-reusable.yaml`), which catches a broken import or
a file that stops collecting, but not a behaviour change.

| File | Covers | Run |
|---|---|---|
| `test_cluster_reader_live.py` | `KubernetesReader` (`application_sdk.testing.harness.cluster`) | `E2E_CLUSTER_NAMESPACE=<namespace> uv run pytest tests/e2e/test_cluster_reader_live.py -m e2e -v` |
| `test_temporal_reader_live.py` | `TemporalServiceReader` (`application_sdk.testing.harness.temporal`) | `E2E_TEMPORAL_QUEUE=<queue> E2E_TEMPORAL_FRONTEND_NAMESPACE=<namespace> uv run pytest tests/e2e/test_temporal_reader_live.py -m e2e -v` |

The cluster reader needs the `harness` extra (`uv sync --all-extras`); without it
`test_cluster_reader_live.py` is skipped at collection. Each file skips itself
when its environment variable is unset. The module
docstrings list the optional overrides (`E2E_KUBE_CONTEXT`,
`E2E_TEMPORAL_FRONTEND_SERVICE`, `E2E_TEMPORAL_FRONTEND_PORT`, ...).

`conftest.py` is separate from both: it holds the `app_config` and
`handler_call` fixtures a downstream connector repo overrides for its own
`tests/e2e/`.
