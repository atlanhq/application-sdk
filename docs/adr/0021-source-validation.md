# ADR-0021: Source Validation — Canonical Probes, Ordered by Layer, Declared by the App

## Status
**Proposed**

The network trio (DNS → TCP → TLS) is prototyped in `application_sdk/handler/source_probes.py`. It is
not wired into any verdict yet; everything else here is design.

## Context

[ADR-0020](0020-artifact-validation.md) checks what an app *produces*. This ADR moves the same job one
step earlier in the supply chain, to the source the app reads from: catch a source that cannot work
before extraction runs, and say precisely why.

Every app already answers that question for itself in `Handler.preflight_check`, and the injected gate
acts on the answer. What fleet telemetry shows (a 30-day failure-taxonomy window and a 7-day window of
gate outcome rows) is that the answers are numerous, overlapping and hard to compare:

1. **There is no shared vocabulary.** The gate rows carry over two hundred distinct check names. Around
   twenty of them mean "can we reach the source" (`connectivity`, `connectivityCheck`, `connectionCheck`,
   `sourceAccess`, …), around twenty mean "were the credentials accepted", and about a dozen mean "did
   the filters match anything". No question can be asked of the fleet as a whole, because no two apps
   name the same check the same way.
2. **"Connectivity" checks conflate the network with authentication.** About half of all blocked-run
   check failures name a connectivity-family check, and roughly three quarters of *those* carry an AUTH
   code. A driver's single `connect()` resolves, connects, negotiates TLS and authenticates in one call,
   so the check cannot tell "nothing answered" from "the password is wrong".
3. **The SDK misclassifies the same way.** `BaseSQLClient.load` wraps every failure, including DNS,
   refused connections, TLS errors and credential *parse* errors, as `SqlClientAuthFailedError` (AUTH).
   A visible share of failed runs carries that code with a sample message that is not about
   authentication at all.
4. **Apps hand-roll network classification.** Several apps carry their own DNS, connect and
   login-timeout codes. Network-layer failures (name resolution, refused/filtered connections, TLS) make
   up about an eighth of all classified failed runs, and each app that classifies them does it
   differently.
5. **"Not run" rows pollute attribution.** One app emits a failed row with a `*_CHECK_NOT_RUN` code for
   every check it skipped after an earlier failure. Those rows appear on more blocked runs than any real
   failure of that app, so ranking failures by row count ranks the skip logic, not the source.

## Decision

### 1. One canonical name per question

The SDK owns a closed vocabulary of source-check names, exported as constants. Apps keep their own
checks for anything app-specific, but a check that answers a canonical question uses the canonical name.
That is what makes "how often is the network the problem?" one query instead of two hundred.

| Layer | Name | Question |
| --- | --- | --- |
| network | `sourceDns` | does the configured host resolve to an address we may connect to? |
| network | `sourceTcp` | does something accept a connection on the port? |
| network | `sourceTls` | does a verified TLS handshake complete (`DIRECT` endpoints only)? |
| identity | `sourceCredentialFormat` | is the credential well-formed (parsable key, no scheme in the host)? |
| identity | `sourceAuth` | does the source accept the credential? |
| identity | `sourceIdentity` | is the authenticated principal the one expected (user, role)? |
| SQL | `sourceQuery` | does a trivial read-only statement run (`SELECT 1` or the dialect's)? |
| SQL | `sourceVersion` | is the server version in the dialect's supported range? |
| SQL | `sourceDefaultContext` | do the default database / warehouse / schema exist and accept use? |
| SQL | `sourceCatalogAccess` | can the role read every catalog view extraction needs? |
| SQL | `sourceFilterMatch` | do the include/exclude filters resolve to at least one object? |

Several endpoints (an API host and an OAuth host) are told apart by a suffix: `sourceTcp.oauth`.

### 2. Probes run in layer order, and a skipped probe is not a failed row

Network, then identity, then SQL. Each probe runs only if its prerequisites passed. A probe that does
not run is **omitted from the rows** and listed on the report's `skipped` axis instead (finding 5). A
probe that does not apply (a raw TLS handshake against a port that negotiates TLS inside its own
protocol) is listed as `unsupported`. A probe that raised is our defect: it is listed as `broken`,
produces no row, and never counts against the source. Every probe lands in exactly one of
`checks` / `skipped` / `unsupported` / `broken`, so no probe disappears silently. This is ADR-0020's "no
silent no-op" rule applied to probes.

Running the network layer first is what fixes finding 2. By the time `sourceAuth` runs, the network has
already been shown to work, so an AUTH code on it means authentication.

### 3. Every row is a typed `PreflightCheck`, and nothing leaks into it

Each probe returns a `PreflightCheck` with a typed `error`. Each distinct remedy gets its own leaf and
code (`application_sdk/handler/source_probe_errors.py`). Two rules apply to every row:

* **No customer host or address in a row.** Socket and certificate error text routinely embeds both,
  so no exception is passed as `cause`. Evidence is limited to values the SDK chose: an errno name, a
  certificate verification code, the port.
* **Certificate verification is never optional.** The TLS probe uses the SDK trust store
  (`get_ssl_context()`, which honours `SSL_CERT_DIR`) and refuses a context that does not verify. The
  remedy for an untrusted certificate is a trusted CA, never a bypass.

The DNS probe also refuses, before any connection, a host that resolves only to loopback, link-local
(which includes the cloud metadata endpoint), unspecified, multicast or reserved addresses. Private
ranges are allowed, because PrivateLink and peered VPCs resolve there. This catches a misconfigured or
probing connection setting. It is a detector, not a network control: the driver resolves the host again
on its own.

### 4. The runtime seam

```python
class SourceEndpoint(BaseModel):          # prototyped
    host: str; port: int; tls: TlsMode    # DIRECT | NEGOTIATED | NONE
    label: str | None                     # required and distinct when there are several

class SourceProbe(Protocol):              # proposed
    name: str                             # from the canonical vocabulary
    tier: CheckTier                       # WARMUP when it needs source compute
    requires: tuple[str, ...]             # canonical names that must pass first
    def applies(self, target: ProbeTarget) -> bool          # False -> "unsupported"
    async def run(self, target: ProbeTarget, state: ProbeState) -> PreflightCheck
```

**What the app declares.** The app declares *what* to reach and *what it needs*. It never declares
*how* to check, because the checks belong to the SDK.

| Declaration | Where | Default |
| --- | --- | --- |
| Endpoints | `Handler.source_endpoints(input) -> list[SourceEndpoint]` | `[]`, which emits `not_declared` (see section 5). `SqlApp`'s handler derives `host`/`port` from the credential |
| Dialect facts | a `SqlDialectProbes` declaration on the `BaseSQLClient` subclass: liveness statement, version query and supported range, identity query, `TlsMode` | none; SQL probes for an undeclared fact are `unsupported` |
| Catalog views extraction reads | the app's contract, beside its other declarations | none; `sourceCatalogAccess` is `unsupported` |

The SQL layer reuses what exists rather than adding a second path. `sourceFilterMatch` calls the same
`prepare_filters` / `get_database_names` code extraction uses, so the probe and the run cannot disagree
about what a filter means. `sourceCatalogAccess` reads each declared view with a statement that returns
no rows. Every SQL probe is read-only and runs under a statement timeout.

**Registry.** The SDK keeps one ordered registry. The network trio applies to every app that declares
an endpoint, and the SQL probes apply to every `SqlApp`. An app can add probes for its own questions
under its own names. It cannot replace an SDK probe, because then the canonical name would stop meaning
one thing.

**Where it runs.** On both surfaces that run `Handler.preflight_check`: `/check` and the injected gate.
The SDK runs the probes *before* the handler, inside a carved budget (20s for the network trio by
default, out of the gate's 150s). In hard mode, a failed network probe also spares the handler a driver
login timeout that would otherwise spend the rest of the budget.

### 5. Posture: warn-only first, graduated on measured agreement

This mirrors the preflight gate and ADR-0020:

* `App.source_probe_mode`, resolved once at worker build. Only the literal `"hard"` enforces.
* **Soft (default).** Probe rows go to their own outcome event and **not** into the handler's verdict.
  An app's verdict, attribution and dashboards are unchanged by adopting this.
* **Hard (per-app opt-in).** Probe rows are prepended to `PreflightOutput.checks`. A failure in a
  `GATE_BLOCKING_CATEGORIES` category makes the verdict `NOT_READY` through the existing attribution
  ladder (`_primary_failure`). Nothing new decides a block.
* **The outcome event.** A new pinned event, `Source probe outcome`, carries the check matrix in the
  existing `_check_matrix_json` shape plus counts for `skipped` / `unsupported` / `broken` /
  `not_declared`. It follows the usual three-part contract: an `events.py` constant, an attribute-key
  constant, and `_KNOWN_EXTRA_KEYS` entries.
* **Graduation.** Nothing graduates until two figures are measured from those events:
  * **Precision.** When a probe fails, does the run also fail?
  * **Recall.** When a run fails `SOURCE_UNAVAILABLE` downstream, did a probe see it first?

### 6. Catalog

Cost is the bound per probe. "Generic" means the SDK can run it with no app input beyond its
declarations.

| Check | Lives | Codes (category) | Cost | Scope |
| --- | --- | --- | --- | --- |
| `sourceDns` | runtime | `INVALID_INPUT_SOURCE_HOST_MALFORMED`, `INVALID_INPUT_SOURCE_ADDRESS_DISALLOWED`, `NOT_FOUND_SOURCE_HOST`, `SOURCE_UNAVAILABLE_DNS` | ≤5s, no credential | generic |
| `sourceTcp` | runtime | `SOURCE_UNAVAILABLE_TCP_REFUSED` / `_TIMEOUT` / `_UNREACHABLE` | ≤5s, no credential | generic |
| `sourceTls` | runtime | `PRECONDITION_SOURCE_TLS_CERTIFICATE`, `PRECONDITION_SOURCE_TLS_HANDSHAKE`, `SOURCE_UNAVAILABLE_TLS_TIMEOUT` | ≤10s, no credential | generic for `DIRECT`; dialect hook for `NEGOTIATED` |
| TLS never disabled | conformance S003 | — | static | generic |
| `sourceCredentialFormat` | runtime, before any network I/O | `INVALID_INPUT_CREDENTIALS_PARSE` (exists) | no I/O | generic for `BaseSQLClient`; per app otherwise |
| `sourceAuth` | runtime | `AUTH`, plus dialect subcodes for expired / locked / MFA-required | one connect | generic, dialect classifier |
| `sourceIdentity` | runtime | `PRECONDITION` | one query | dialect |
| `sourceQuery` | runtime | `SOURCE_UNAVAILABLE`, `TIMEOUT` | one statement | generic |
| `sourceVersion` | runtime | `PRECONDITION` | one query | dialect |
| `sourceDefaultContext` | runtime, `WARMUP` tier where compute is needed | `NOT_FOUND`, `PRECONDITION` | one query | dialect |
| `sourceCatalogAccess` | runtime | `PERMISSION` | one zero-row read per declared view | generic, app declares views |
| `sourceFilterMatch` | runtime | `PRECONDITION` | one catalog query | generic for SQL |
| Volume estimate | runtime, advisory | none: a measurement, not a failure | one bounded catalog count | generic for SQL |
| Session settings honoured | runtime | `PRECONDITION` | one query | dialect |
| Proxy / tunnel reachability | deferred | — | — | no config model declares one yet |

Notes on specific checks:

* **DNS.** NXDOMAIN is `NOT_FOUND` and not retried: the name authoritatively does not exist. A resolver
  timeout or server failure is `SOURCE_UNAVAILABLE` and retried, the same split the gate makes for any
  source fault (FND-3774).
* **TCP.** A connect timeout stays `SOURCE_UNAVAILABLE` rather than `TIMEOUT`. A firewall dropping
  packets cannot be told apart from a transient outage, and `TIMEOUT` never blocks.
* **Certificate expiry and volume estimate.** Both need an advisory severity that `PreflightCheck`
  lacks today. A failed row would block a working source. They wait for that field.

### 7. Ranked by expected impact

Ranked by the share of classified failed runs each check would catch *earlier* or *attribute more
precisely*, then by how many apps hand-roll it today, then by cost.

1. **Network trio.** About an eighth of failed runs are network-layer failures. Separating them is also
   what gives meaning to the connectivity-family rows that dominate blocked runs. Cheapest of all, needs
   no credential, and fully generic. Prototyped here.
2. **Credential format, then auth, as separate probes.** Rejected credentials are the largest single
   failure class. They are already caught, so the gain is routing: parse errors stop masquerading as
   AUTH, and expired / locked / MFA-required get their own codes. The `BaseSQLClient.load`
   misclassification is fixed as part of this, separately from this ADR.
3. **Catalog access.** Permission and missing-object failures are the next largest class, spread over
   dozens of app-specific check names. It is generic once the views are declared.
4. **Filters match at least one object.** A small share of failed runs, but every one of them extracts
   nothing, and a dozen apps reimplement it. One catalog query, reusing extraction's own filter code.
5. **Default context usable.** A small share. It overlaps the warmup tier, so it lands with the
   readiness work rather than ahead of it.

Not ranked:

* **Version range.** No measurable failure signal, and version checks never appear among block reasons.
* **Volume estimate.** Its value is sizing timeouts, which belongs to the timeouts project.
* **Session settings.** No evidence of failures.

### 8. Conformance rules

Each rule lands at `warn`, the catalog's only entry tier.

| ID | Name | Flags |
| --- | --- | --- |
| F021 | SourceEndpointUndeclared | an app that constructs a source client (`BaseSQLClient` subclass, HTTP client, driver `connect`) and declares no `source_endpoints` |
| F022 | HandRolledNetworkProbe | preflight-reachable `socket.getaddrinfo` / `socket.create_connection` / `ssl.wrap_socket` in app code, where the SDK probe applies. Complements F011, which flags the same calls when they are *unbounded* |
| F023 | NonCanonicalSourceCheckName | a `PreflightCheck(name=...)` literal that is a known synonym of a canonical name. The canonical vocabulary is the allowlist |
| F024 | NotRunReportedAsFailure | `PreflightCheck(passed=False)` used to report a check that did not run (a `*_NOT_RUN` / skipped code) |
| S003 | TlsVerificationDisabled | `verify=False`, `ssl.CERT_NONE`, `check_hostname = False`, `ssl._create_unverified_context`, or a literal driver option that disables verification (e.g. `TrustServerCertificate=yes`). Promoted to `block` once the fleet is clean |

## Options Considered

### Option 1: SDK-owned canonical probes, app-declared targets (Chosen)

**Pros:**
* One name per question makes the fleet measurable for the first time.
* Separates network, identity and SQL faults by construction, not by string matching.
* Apps declare data (endpoints, dialect facts, views), never check code, so a fix to a probe reaches
  every app.

**Cons:**
* A new declaration for every app with a network source (F021 tracks adoption).
* `NEGOTIATED` TLS needs a dialect hook before it is covered.

### Option 2: A shared helper apps call from their own `preflight_check`

**Pros:** no new runtime seam; apps adopt at their own pace.

**Cons:** names stay per-app, so finding 1 is not fixed. Ordering and the skip rule are per-app too,
so findings 2 and 5 aren't fixed either. A helper reproduces the current state with less duplicated
code. **Rejected** as the end state. It is the prototype's shape only because nothing is wired yet.

### Option 3: Improve the driver-exception classifier only

Walk to the root cause of every driver exception and classify by type instead of by string.

**Pros:** no new probes; it fixes finding 3 directly.

**Cons:** it still learns about the network only through a login attempt. A filtered port costs a full
driver login timeout before anything is known, and one `connect()` still cannot report which layer
failed when several are wrong at once. **Kept as a complement** (follow-up below), not an alternative.

### Option 4: Probe from outside the worker (a platform-side reachability service)

**Cons:** it measures a different network path from the one extraction uses, which is the path that
matters, and it puts a customer host into a second system. **Rejected.**

## Consequences

* "Why can't we reach this source?" gets one answer shape across every app, attributed to the layer
  that failed.
* Adopting the probes in soft mode changes nothing an app reports today. It only adds an event.
* Rows never carry a customer host, address or raw socket text. That is pinned by test.
* Certificate verification has no bypass in the SDK, and S003 makes a bypass elsewhere visible.
* `PreflightCheck` needs an advisory severity before certificate-expiry and volume probes can ship.

## Follow-ups

* `BaseSQLClient.load` wraps every failure as AUTH. Classify by root cause, and route parse errors to
  `INVALID_INPUT_CREDENTIALS_PARSE`.
* An advisory severity on `PreflightCheck`, so a row can warn without failing.
* A connection-config model for proxies and tunnels, so their reachability has something to read.

## References

* `application_sdk/handler/source_probes.py`, `source_probe_errors.py`: the network-trio prototype.
* [ADR-0020](0020-artifact-validation.md): the posture, outcome-event and "no silent no-op" rules
  mirrored here.
* [ADR-0013](0013-error-hierarchy-and-failure-taxonomy.md): the categories every row routes on.
* `application_sdk/handler/_preflight_outcome.py`: the attribution ladder hard mode reuses unchanged.
* `application_sdk/common/sql_filters.py`: the filter resolution `sourceFilterMatch` reuses.
* Conformance F011 (blocking probe) and F003 (untyped check failure): the rules F021–F024 sit beside.
