---
id: FP10
applies-to: [app]
---
# Use the SDK's mechanism; don't rebuild it

Principle: When the SDK already does something (retry and backoff, storage and transfer,
credentials, config, logging, temp files, heartbeats, client lifecycle, offloading), the app calls
it. A local copy is allowed only for a stated SDK gap: the missing capability is named and the
copy is marked for removal.
Why: Every private copy is a place where a fix made once in the SDK never arrives, and where
behaviour drifts from the rest of the fleet on the inputs its author did not think of.
Ask:
- Does new code do by hand something the SDK has a public helper for? Look in the SDK before accepting "the SDK cannot do this".
- Where the local version differs from the SDK's, which inputs make them disagree (an error the SDK retries, a path it normalises, a case where it warns and carries on)?
- If the gap is real, does the PR name the missing capability, link the SDK issue and mark the local code as temporary?
Don't flag: copies conformance names: `temporalio` and private imports (`P004`, `P005`, `B008`),
own storage clients and transfers (`P009`, `P044`, `P042`), own worker or server start-up (`P017`,
`P018`), raw HTTP to Atlan (`P019`), own thread or process pools (`P031`, `P036`), own credential
routing (`P053`), own persistent prefix or connection-name parsing (`P048`, `P049`), deprecated
symbols (`B001`). Do not flag thin wrappers that only call the SDK.
Severity: High when the copy changes behaviour on a production path (retries, storage,
credentials). Medium for duplication that behaves the same today.
