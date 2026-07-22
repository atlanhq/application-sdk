# Safety — worked examples

Concrete anchors for the `safety` agent. Static secret/SQL/CVE scanning is owned
by conformance / codeql / trivy / grype and is EXCLUDED — these examples are the
runtime + fleet issues those scanners miss.

---

## SEC — credential reachable in an error/log surface
```python
# BAD — the raw key flows into a surface that gets forwarded/persisted
raise AuthError(f"auth failed for {api_key}")
```
```python
# GOOD — redact; never let the secret leave the process intact
raise AuthError(f"auth failed for key={api_key[:4]}***")
```
**skip-when:** the value is already redacted, or it's a test fixture credential.

## SEC — multi-tenant isolation gap
```python
# BAD — query not scoped to the caller's tenant; cross-tenant read
rows = store.query(f"SELECT * FROM assets WHERE id = {asset_id}")
```
```python
# GOOD — tenant scope is mandatory and parameterised
rows = store.query("SELECT * FROM assets WHERE id = ? AND tenant = ?", asset_id, tenant)
```
**skip-when:** the surface is genuinely tenant-agnostic (e.g. SDK-internal, no tenant data).
Security findings are Critical/High only — never Medium.

## SEC — path traversal at a boundary
```python
open(os.path.join(base, user_supplied))     # BAD — "../../etc/…" escapes base
```
```python
p = (base / user_supplied).resolve()        # GOOD — resolve then confirm containment
if not p.is_relative_to(base): raise ValueError("path escapes base")
```
**skip-when:** the path isn't user-influenced.

---

## PERF *(weekly)* — blocking / unbounded on a HOT path
```python
# BAD — loads the whole result set into memory on a per-row hot loop
rows = cursor.fetchall()
for r in rows: process(r)
```
```python
# GOOD — stream in bounded chunks
while batch := cursor.fetchmany(BATCH):
    for r in batch: process(r)
```
**skip-when (the worth check):** cold path (called ~3×/workflow), or zero callers.
A `ThreadPoolExecutor` per call in a low-frequency path is acceptable — don't flag.

## PERF *(weekly)* — N+1
```python
for id in ids:                       # BAD — one round-trip per id
    items.append(client.get(id))
```
```python
items = client.get_many(ids)         # GOOD — one batched call
```
**skip-when:** the collection is tiny and bounded, or no batch API exists (then it's a DX finding, not PERF).

## DEPDRIFT *(weekly)* — runtime/version staleness
Signal: a direct dep, or the **Dapr** / **Temporal** SDK, materially behind
upstream stable, or a pin (`==`) blocking a security-relevant minor. Report the
current → available versions. **skip-when:** the pin is intentional and
commented (known incompatibility), or trivy/grype already own it as a CVE.

## PERFTREND *(weekly)* — regression vs last weekly
Run the micro-bench suite; flag a metric that regressed past the threshold
versus the previous weekly run. **skip-when:** the delta is within noise, or the
regression is an intended trade-off (ticketed).
