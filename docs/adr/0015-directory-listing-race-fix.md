# ADR-0015: Directory Listing Race Fix

## Status
**Accepted**

## Context

The directory branch of `application_sdk.storage.transfer.upload()` —
along with two sibling sites in `storage.reference.persist_file_reference`
and `contracts.types.FileReference.from_local` — enumerated local
directories via `pathlib.Path.rglob("*")` and trusted the result.

On macOS APFS under concurrent I/O load (multiple workflows, Dapr +
Temporal, integration test suites on a developer laptop), `rglob("*")`
intermittently returned an empty iterator for a directory that
demonstrably contained files. The upload then completed successfully
with `file_count=0` and `reason="skipped:hash_match"`. Downstream
consumers branching on `file_count == 0` interpreted this as a
legitimate quiet-day empty run and dropped entire pipeline stages
without any telemetry signal.

Two independent failure classes compounded:

1. **CPython pathlib silently swallows `OSError` mid-walk**
   ([cpython#146646](https://github.com/python/cpython/issues/146646),
   [cpython#68308](https://github.com/python/cpython/issues/68308)).
   `pathlib.Path.rglob` returns whatever was found before the transient
   error, indistinguishable from a clean empty listing. The
   suppression was intentional but undocumented until Python 3.13.

2. **APFS directory btree visibility lag.** On macOS under concurrent
   write pressure, the directory metadata for a freshly-written file
   can lag the write itself returning to userspace. `fsync(2)` on
   Darwin only flushes kernel buffers — not the drive's write cache
   — so it is insufficient to force visibility. The Darwin-specific
   `fcntl(F_FULLFSYNC=51)` is the documented primitive for this.

The three vulnerable sites share the same pattern: "list a directory
immediately after it was written, by code in this process." Any fix
must be a primitive used uniformly across all three.

## Decision

Introduce a private `safe_list_directory(path)` primitive in
`application_sdk/common/_listing.py` that composes two defenses, and
migrate the three vulnerable sites to it. The primitive lives in
`common/` rather than `storage/` because it has no `storage`-specific
state and one of the three callers (`contracts.types.FileReference.from_local`)
needs to import it — placing it in `common/` keeps the dependency
direction clean (callers → common, which is the layering convention
also followed by `common/file_ops.py`, `common/path.py`,
`common/concurrency.py`).

The primitive:

1. Opens the directory as a POSIX FD and issues a metadata barrier
   (`F_FULLFSYNC` on Darwin, `os.fsync` on Linux, no-op on Windows)
   to force the kernel to commit pending directory updates before the
   listing reads them.
2. Walks the tree with `os.scandir` recursively rather than
   `pathlib.Path.rglob`. `os.scandir` surfaces `OSError` from the
   underlying syscalls rather than silently absorbing it, so a
   genuine listing problem becomes a raised exception that the caller
   can react to.

Callers continue to pass a path. No file lists cross the Temporal
payload boundary; no API change is required at any of the three
migrated sites.

## Alternatives considered

Nine backwards-compatible options were surveyed before this design
landed (see the research doc attached to PR #2137 for the full
discussion of each option's mechanism, real-world precedent, and
trade-offs):

| # | Option | Why not (alone) |
|---|--------|-----------------|
| 1 | Disambiguation on empty (log + reason="empty") | Surfaces the failure but doesn't fix the race |
| 2 | Switch `Path.rglob` to `os.scandir` recursion | Closes the pathlib class only |
| 3 | fsync barrier on the directory FD | Closes the metadata class only |
| 4 | Multi-syscall reconciliation + retry | Probabilistic; not deterministic |
| 5 | Combined safe-list primitive (2 + 3 + optional 4) | **chosen** |
| 6 | Kernel event watch (FSEvents / inotify) | Lifecycle complexity disproportionate to bug |
| 7 | Sentinel / `_SUCCESS` marker file | Requires producer cooperation |
| 8 | Atomic stage-then-publish via rename | Requires producer cooperation |
| 9 | Stream-as-produced upload API | Architectural shift; large scope |

The previous attempt (PR #2048) added an opt-in `explicit_files`
parameter to `UploadInput`. That approach was rejected during review:
it puts the burden on every caller, doesn't close the race for
callers who just supply a path, and routes a file list through the
Temporal payload boundary (which is 2MB-capped and otherwise free of
this coupling). PR #2048 is closed; this ADR records the design that
replaced it.

The pure pathlib-only fix (option 2 alone) and the pure fsync-only fix
(option 3 alone) are each insufficient because the two failure classes
they address are independent. Composing both — option 5 — closes both
classes through a single primitive used at all three vulnerable sites.

## Industry precedents

Every mature system that has confronted directory-listing races in
production has converged on one of three patterns: a marker file
written last (Hadoop `_SUCCESS`), an explicit manifest of intended
files (Apache Iceberg, Delta Lake, Spark Magic Committer), or
atomic publish via `rename` (Maildir, atomicwrites, Git's
`index.lock`). The transparent fix this ADR adopts is closer in
shape to "patch the listing primitive itself" — but the existence of
those three patterns is the broader signal that listing-and-hoping
is a known antipattern.

Specific primitives this design relies on:

- **`F_FULLFSYNC` on Darwin** —
  [Apple's `fsync(2)` man page](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fsync.2.html)
  documents that "F_FULLFSYNC asks the drive to flush all buffered
  data to permanent storage. Applications, such as databases, that
  require a strict ordering of writes should use F_FULLFSYNC."
  SQLite (`PRAGMA fullfsync`) and PostgreSQL on Darwin use it for
  the same visibility guarantee.

- **`os.scandir` surfaces errors** — Black, ruff, and mypy all bypass
  `pathlib`'s recursive walk specifically because of the silent
  `OSError` suppression. `os.scandir` is the standard recommended
  primitive for "give me directory entries with explicit error
  handling."

## Platform support

| Platform | Barrier | Listing |
|----------|---------|---------|
| macOS    | `fcntl(F_FULLFSYNC=51)`, fallback to `fsync` | `os.scandir` recursion |
| Linux    | `os.fsync(dir_fd)`                          | `os.scandir` recursion |
| Windows  | no-op (NTFS not known to exhibit this race; no POSIX dir-FD) | `os.scandir` recursion |

The fsync barrier is best-effort: if the FD-open or fsync raises an
`OSError`, the primitive proceeds to the scandir step, which is the
actual correctness layer. Both swallow sites in `_listing.py` carry
a `# conformance: ignore[E002]` directive explaining the intentional
fallthrough, per the codebase convention used in
`handler/service.py`, `observability/_objectstore_metric_exporter.py`,
and elsewhere.

This is consistent with [ADR-0011](0011-logging-level-guidelines.md)'s
"never swallow exceptions silently" guideline: the swallow is bounded
to a defense-in-depth barrier that the surface-level scandir call
will replace with a real exception if the listing itself is broken.

## Consequences

**Positive**

- The `file_count == 0` silent-failure mode is closed. Real listing
  errors now surface as `OSError`; legitimately-empty directories
  still return zero count without an error.
- All three race sites converge on a single primitive. Any future
  fix only needs to land in one place.
- No public API surface change. The primitive is underscored
  (private). No caller is asked to enumerate file names; no file
  lists cross the Temporal payload boundary.
- Behavior is identical on Linux and Windows to a degree better than
  before (no silent `OSError` swallow either). The fix benefits the
  Linux/Windows paths as well — the macOS APFS metadata gap is just
  the highest-visibility manifestation.

**Negative**

- Adds a small constant overhead per directory listing on macOS:
  ~3.5–4ms from the `F_FULLFSYNC` round-trip. Numbers below.
- A new internal primitive that must be maintained. The primitive is
  ~60 lines including docstrings; surface area is small.

## Benchmark

Median of 20 runs per cell, Darwin 25.3.0 / Python 3.12.2:

| files | depth | rglob (ms) | safe_list (ms) | Δ (ms) | factor |
|------:|------:|-----------:|---------------:|-------:|-------:|
|    10 |     1 |       0.16 |           3.96 |  +3.79 | 24.26x |
|    10 |     3 |       1.54 |           5.17 |  +3.62 |  3.35x |
|   100 |     1 |       0.51 |           5.59 |  +5.08 | 10.91x |
|   100 |     3 |       2.65 |           7.26 |  +4.61 |  2.74x |
|  1000 |     1 |       3.93 |           8.67 |  +4.74 |  2.21x |
|  1000 |     3 |       5.13 |           9.26 |  +4.13 |  1.80x |

The added cost is essentially a constant +4ms regardless of
directory size — the F_FULLFSYNC round-trip dominates on small
trees, and the iterative `os.scandir` descent is competitive with
or faster than `Path.rglob` at scale. The multiplier looks dramatic
on the smallest cell only because rglob there takes 0.16ms; in
absolute terms the cost is identical to the 1000-file case. On
Linux the barrier is plain `fsync` (cheaper than F_FULLFSYNC); on
Windows there is no barrier at all.

### Reproducing the numbers

The methodology is intentionally minimal so the numbers reflect the
listing cost itself, not framework overhead:

```python
import statistics, tempfile, time
from pathlib import Path
from application_sdk.common._listing import safe_list_directory

rglob_t, new_t = [], []
for _ in range(20):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for i in range(1000):  # vary per cell
            (root / f"f{i:05d}.txt").write_bytes(b"x")
        t = time.perf_counter()
        _ = [p for p in root.rglob("*") if p.is_file()]
        rglob_t.append(time.perf_counter() - t)
        t = time.perf_counter()
        _ = safe_list_directory(root)
        new_t.append(time.perf_counter() - t)

print(f"rglob median:     {statistics.median(rglob_t) * 1000:6.2f} ms")
print(f"safe_list median: {statistics.median(new_t) * 1000:6.2f} ms")
```

Vary the inner loop's file count (10 / 100 / 1000) and add nested
subdirectories for the depth-3 cells. No pytest, no plugins, no
fixtures.

## Implementation

- `application_sdk/common/_listing.py` — the primitive. Walk is an
  iterative `os.scandir` descent (explicit stack, one open directory
  FD at a time, no recursion-limit ceiling) rather than a recursive
  generator — matches the iterative approach used internally by
  CPython's `pathlib.rglob`, `black`, and `ruff`.
- `application_sdk/storage/transfer.py` — migrated; directory branch
  of `upload()`. Wraps the sync `safe_list_directory` in
  `asyncio.to_thread` to keep the blocking fsync + scandir off the
  event loop.
- `application_sdk/storage/reference.py` — migrated;
  `persist_file_reference` directory path. Same `asyncio.to_thread`
  wrapping rationale.
- `application_sdk/contracts/types.py` — migrated;
  `FileReference.from_local` file-count probe. Sync caller, called
  directly (no `to_thread`).
- `tests/unit/common/test_listing.py` — direct tests pinning the
  primitive's contract: happy paths, recursion, symlink exclusion,
  cyclic-symlink termination, OSError surfacing on missing/non-dir
  paths, F_FULLFSYNC + fallback path on Darwin, best-effort fsync
  swallow, and iterative-descent invariants (substantial depth +
  peak-one-open-FD verified via a tracking `os.scandir` wrapper +
  order-agnostic sibling visit).
- `tests/unit/storage/test_transfer.py`,
  `tests/unit/storage/test_reference.py`,
  `tests/unit/contracts/test_types.py`,
  `tests/integration/test_storage_io.py` — TDD migration tests that
  mock `Path.rglob` to return empty (or partial) and assert each
  caller reports the correct file count. Tests fail on the pre-fix
  code and pass after migration.

## Related

- **ADR-0011** — Logging Level Guidelines (silent-swallow conformance
  discussion); the `# conformance: ignore[E002]` directives in
  `_listing.py` are justified per the "best-effort, surfaced
  elsewhere" carve-out.
- **PR #2137** — implementation of this ADR.
- **PR #2048** (closed) — earlier `explicit_files` attempt, replaced
  by this design.
- **Linear PART-1148** — issue tracker.
