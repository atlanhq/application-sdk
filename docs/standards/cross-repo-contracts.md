# Cross-Repo Contracts

Some values this SDK produces are read by code that does not live here — the
Automation Engine, the runtime scenario suite, Heracles. For those, "the tests
pass" is not the whole bar: a rename or a relocation can be locally invisible
and still red another repo's suite, or worse, silently mis-route production
work.

Each entry below names one such value, what reads it, and the test in this repo
that pins it. **Before changing anything on this list, read the entry.** If you
are adding a value that another repo will read, add an entry and a pinning test
with it.

## The served manifest's resolved `task_queue`

| | |
|---|---|
| **Produced by** | `resolve_manifest_tokens()` in `application_sdk/common/task_queue.py`, applied by the manifest route in `application_sdk/handler/service.py` |
| **Served at** | `GET`/`POST /workflows/v1/manifest` (and the deprecated unversioned `/manifest` alias) |
| **Key** | `task_queue`, at `dag.<node>.task_queue`, `dag.<node>.inputs.task_queue`, and top level on single-node manifests |
| **Read by** | The Automation Engine, which writes it into the DAG and submits work to it; from FND-224, the runtime scenario suite's contract tier, which diffs it against the TWD trigger, the KEDA metadata and the rerouter's formula |
| **Pinned by** | `TestServedManifestTaskQueueContract` in `tests/unit/handler/test_service.py` |

The value is **stamped, not derived**: the route hands `resolve_manifest_tokens`
the queue this process was configured with — the same value `create_worker`
receives — and that value is copied into the manifest verbatim. It is
deliberately not re-derived from the environment at serve time, because an
explicit `ATLAN_TASK_QUEUE` / `--task-queue` override is not reproducible by
re-derivation. Two paths deriving the same answer is a convention that holds
until someone's inputs differ; one path copying the other's answer is
structural.

That is the FND-195 fix, and the failure it removed is silent by construction:
when the served queue and the polled queue disagree, nothing errors. AE submits
to one queue, the worker polls another, and the run sits unclaimed until its 24h
heartbeat backstop (CONNECT-183; the same gap stripped failure attribution in
HYP-1954).

What this means in practice for a change to `task_queue.py` or the manifest
route:

- **Renaming the key, or the path it sits at, is a breaking change** for a
  consumer that cannot vote in this repo's CI. It needs coordinating, not just
  a passing test run.
- **Reverting to token substitution** — filling `{app_name}` /
  `{deployment_name}` in place rather than replacing the whole template —
  reintroduces the original divergence. The module docstring in
  `task_queue.py` explains why at length; read it before proposing it again.
- **The unset case must stay loud.** A missing app name resolves to `None` and
  leaves the literal `{app_name}` token visible, rather than manufacturing
  `atlan-default-<deployment>`, which reads as a legitimate queue and hangs the
  run silently.
