---
kind: function
name: docker-build-gate
description: >
  Builds the app's Dockerfile as the orthogonal gate after an I-series (container
  image) fix.  This is the gate whose absence is the stated reason the dockerfile
  area was suggest-only: `areas/dockerfile.prose.md` says propose-don't-apply holds
  "until a Dockerfile linting gate is wired in".  This is that gate — with it, an
  I-series fix is verified by the same discipline as every other area rather than
  routed to a human because nothing could check it.

  Deliberately a real `docker build`, not a linter.  The failure modes the area
  was worried about — layer ordering, entrypoint interactions, build-time vs
  run-time env separation — are build-time facts a linter cannot see.  A lint pass
  would restore the appearance of a gate without its substance.
---

### Parameters

- `scope` (string, required) — repository root path.
- `touched_files` (list of string, required) — every path the fix wrote.  Used to
  locate the Dockerfile that was edited, and to bound the revert on failure.

### Returns

- `passed` (boolean) — true only if the image built successfully.
- `exit_code` (integer) — raw exit code from `docker build`.
- `summary` (string) — last ~20 lines of combined stdout/stderr, for the residue
  report.
- `docker_absent` (boolean) — true if the `docker` CLI is unavailable or its
  daemon is not reachable.  When true, `passed` is always false and the gate
  reason is `cannot-verify`.

### Implementation

First, establish that the gate can actually run.  A gate that cannot run must
never report success — the whole point of wiring this in is that an I-series fix
stops being accepted on trust:

```sh
docker info >/dev/null 2>&1 || echo "docker-absent"
```

`docker info` rather than `which docker`: the binary being on `PATH` says nothing
about whether a daemon is listening, and a fix accepted because the client existed
but the daemon did not is exactly the false green this gate exists to prevent.

If docker is absent, set `docker_absent = true`, `passed = false`, and return
immediately.  The loop then reverts the edit and routes the finding to residue with
reason `cannot-verify` — the same outcome the area had before this gate existed, so
an environment without docker degrades to the old behaviour rather than to a lie.

Otherwise, resolve which Dockerfile to build.  Take the first entry in
`touched_files` whose basename is `Dockerfile` or which matches `Dockerfile.*`; if
none of the touched files is a Dockerfile, the fix did not change the image and
this gate has nothing to verify — return `passed = true`, `exit_code = 0`,
`summary = "docker-build gate: no Dockerfile among touched files; nothing to
build"`.

Build it, from the repository root so the build context matches CI:

```sh
cd <scope>
docker build --file <dockerfile> --tag conformance-remediation-gate:<rule_id> . 2>&1
```

Capture the exit code and the last 20 lines of combined output.

- exit 0 → `passed = true`.  The edit survives.
- exit ≠ 0 → `passed = false`.  Revert every path in `touched_files`; the summary
  must quote the build error so the residue report explains *why* the Dockerfile
  change was rejected rather than only that it was.

Always remove the tagged image afterwards, on both paths, so a long sweep does not
accumulate one image per remediated rule:

```sh
docker image rm -f conformance-remediation-gate:<rule_id> >/dev/null 2>&1 || true
```

### Notes

**Build, don't run.** The gate proves the image assembles; it does not start a
container. Runtime verification would need credentials and a live tenant, which no
remediation unit has, and an entrypoint that fails only at run time is beyond what
any local gate can establish.  I005 (`USER root` removal) and I001/I002/I004 (base
image, CMD/ENTRYPOINT, mode-env) are all build-time-visible, which is why building
is sufficient for the I-series specifically — this gate should not be borrowed by
an area whose failures are runtime-only.

**Cache is a feature, not a risk.** Docker layer caching makes repeated builds
across a sweep cheap, and it cannot mask the failure classes this gate targets: a
changed `FROM`, `USER`, `CMD` or `ENTRYPOINT` line invalidates its own layer and
everything after it, so the edited instruction is always re-executed.

This function is skipped for `suppress` outcomes — but note that every I-series
rule is BLOCK-tier with no suppress path, so in practice the dockerfile area only
ever reaches this gate through the `fix` branch.
