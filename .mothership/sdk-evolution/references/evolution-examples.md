# Evolution — worked examples

Concrete anchors for the `evolution` agent. These are opportunities/cruft, not
bug claims — the bar is higher (confidence ≥ 85) and every finding states a
concrete benefit + effort.

---

## CENTRAL — same logic reinvented in ≥ 3 places
```python
# BAD — retry-with-backoff hand-rolled differently in 4 modules
for attempt in range(3):
    try: return call()
    except Exception: time.sleep(2 ** attempt)
```
```python
# GOOD — one canonical utility, all callers migrated
from application_sdk.common.retry import retry_with_backoff
return await retry_with_backoff(call, attempts=3)
```
**skip-when:** the implementations differ for good reason, or it's < 3 sites.

## DX — API ergonomics
```python
def x(self, t, r, cb, o, f, m): ...   # BAD — cryptic, > 5 required params
def extract(self, *, timeout_seconds: int, retries: int = 3) -> Result: ...  # GOOD
```
**skip-when:** the signature is constrained by a stable public contract (changing it is APICOMPAT, not DX).

## CONF — a recurring pattern that no rule catches yet
When ≥ 3 findings this run share one *detectable* pattern the CI doesn't gate →
propose a conformance rule instead of N point fixes. Ship **rule + remediation +
catalog entry in the same PR**; check `packages/conformance` first so you don't
duplicate. **skip-when:** a rule already exists, or the pattern isn't reliably detectable.

## TEMPORAL *(weekly)* — more idiomatic workflow modelling
```python
# BAD — busy-polling external state from workflow code
while not await self.check_ready():
    await asyncio.sleep(30)
```
```python
# GOOD — signal-driven; the workflow waits, no polling
await workflow.wait_condition(lambda: self._ready)   # set by a @signal
```
Also: child workflows / `continue-as-new` for unbounded history; correct
heartbeat + retry-policy defaults. **Exit is an ADR PR** against `docs/adr/`.
**skip-when:** polling is genuinely required and bounded.

---

## BOILERPLATE *(weekly, v3 apps)* — app reinvents what the SDK provides
```python
# BAD — app hand-rolls credential resolution the SDK already does
secret = requests.get(f"{vault}/v1/secret/{name}").json()["data"]
```
```python
# GOOD — app uses the SDK seam; delete the hand-rolled code
creds = await self.resolve_credentials(cred_ref)   # SDK CredentialResolver
```
Common targets: retry, pagination, credential/state/object-store, logging setup,
heartbeating, typed errors, config parsing. Raise a migration PR that **deletes
the app code**. If it recurs across apps → also add an app-scope conformance
rule. **skip-when:** the app's variant does something the SDK genuinely can't yet
(that's an SDK-gap → evolution DESIGN, not a delete).

## APPHEALTH *(weekly, v3 apps)* — is the app running?
Signal: the app's own CI is red against the current SDK, it fails to boot
(import/contract error), or it's pinned to an old SDK major and drifting. Report
per-app status on the parent ticket. **skip-when:** red for an infra/runner
reason unrelated to the SDK.

## FLEET *(weekly)* — adoption laggards
From `/audit-consumers` data: which v3 apps are N SDK versions behind. Surface as
a table. **skip-when:** app is current or intentionally frozen.

## TOOLKIT *(weekly)* — contract-toolkit change
Route through `toolkit-feature-workflow` (downstream-compat validation) before
proposing. **skip-when:** touches generated contracts without the compat check —
then it's DESIGN, not FIX.

## EXAMPLE / DOCSITE *(weekly)* — drift
EXAMPLE: `contract-toolkit/examples/` references a removed/renamed symbol.
DOCSITE: a docs.atlan.com guide does the same. **skip-when:** the reference is
still valid, or intentionally shows a deprecated-but-supported path.
