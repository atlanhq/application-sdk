# Correctness & Quality — worked examples

Concrete anchors for the `correctness-quality` agent. One BAD → GOOD pair and a
**skip-when** (false-positive guard) per check. Illustrative, not exhaustive —
apply judgment; the tier + confidence bar in `check-registry.md` still governs.

---

## BUG — determinism in `run()` / `@entrypoint`
```python
# BAD — non-deterministic call in workflow code; replays diverge
@entrypoint
async def run(self, i: Input) -> Output:
    run_id = str(uuid.uuid4())          # random in workflow
    ts = datetime.now()                 # wall-clock in workflow
```
```python
# GOOD — generate in a @task (activity), pass the value in
@entrypoint
async def run(self, i: Input) -> Output:
    run_id = await self.make_run_id()   # @task returns a stable value
```
**skip-when:** the code is inside a `@task`/activity (I/O + nondeterminism belong there).

## BUG — blocking call in async without `run_in_thread`
```python
# BAD — sync driver call blocks the event loop inside a @task
@task
async def fetch(self, i: Input) -> Output:
    rows = self._cursor.execute(sql).fetchall()
```
```python
# GOOD
rows = await self.run_in_thread(lambda: self._cursor.execute(sql).fetchall())
```
**skip-when:** the call is already non-blocking / awaited, or it's a cold one-shot in setup.

## BUG — mutable default argument
```python
def __init__(self, tags: list = []):        # BAD — shared across instances
def __init__(self, tags: list | None = None):  # GOOD — set inside, or Field(default_factory=list)
```
**skip-when:** the default is immutable (tuple, frozenset, None, literal).

## BUG — swallowed exception hiding a failure
```python
try:                          # BAD — failure vanishes, no signal
    self._commit()
except Exception:
    pass
```
```python
except Exception as e:        # GOOD — log with context and re-raise typed
    logger.error("commit failed for %s", entity_id)
    raise CommitError(...) from e
```
**skip-when:** the swallow is deliberate and documented (best-effort cleanup with a WARNING).

---

## LOG — signal quality (NOT the CI-gated level lint)
```python
except TimeoutError:                 # BAD — stack trace dropped, cause lost
    raise RuntimeError("failed")
```
```python
except TimeoutError as e:            # GOOD — chain preserves the cause
    raise ExtractionError("upstream timed out") from e
```
**skip-when:** the level/format issue is what ruff or the conformance L-series already gates — that's excluded, not ours.

## TYPES — public-surface erosion
```python
def resolve(self, ref) -> dict:               # BAD — untyped param, dict return on a public API
def resolve(self, ref: CredentialRef) -> Credentials:   # GOOD
```
**skip-when:** private/underscore symbol, or a genuinely dynamic boundary that's documented.

## APICOMPAT — silent public break
```python
# BAD — exported symbol removed / renamed with no shim; connectors break on upgrade
# was: def upload_file(key, local_path)
def upload(key, path):                # renamed, no deprecation
```
```python
# GOOD — keep the old name as a deprecating shim for one release
def upload_file(key, local_path):
    warnings.warn("use upload()", DeprecationWarning, stacklevel=2)
    return upload(key, local_path)
```
**skip-when:** symbol isn't exported (not in any `__all__`), or the break is intended and ticketed.

---

## DOCS — docstring drift
Flag when the signature and the docstring disagree (a param renamed/removed, a
new `raises`, an example that no longer imports). **skip-when:** trivial wording;
the doc is accurate, just terse.

## TEST — quality, not existence
```python
assert result                         # BAD — passes for any truthy value
assert result.status == "SUCCESS" and len(result.rows) == 3   # GOOD — specific
```
**skip-when:** the vague assert is a smoke check explicitly labelled as such.

## STALE — dead deprecation shim
`warnings.warn(..., DeprecationWarning)` > 3 months old **and** `rg` shows zero
remaining callers → remove it. **skip-when:** callers still exist, or it's a
documented long-support shim.

## MANIFEST — content drift
A public signature changed but `docs/agents/sdk-capabilities.md` shows the old
shape → regenerate via `/capability-manifest`. **skip-when:** manifest matches.

---

## ARCH *(weekly)* — dependency direction
```python
# BAD — infrastructure/ importing from app/ (arrow points backwards)
# application_sdk/infrastructure/state.py
from application_sdk.app.base import App
```
Allowed direction is `app/ → execution/ → infrastructure/`. **skip-when:** it's a
type-only import under `TYPE_CHECKING`, or the seam is an accepted Protocol.

## FLAKY *(weekly)* — retry-pass tests
From CI history, a test that failed then passed on re-run with no code change.
Report the test + its flakiness rate. **skip-when:** the flake was an
infra/runner outage, not the test.
