# performance: Event loop, memory, timeouts, progress
- Flag: blocking file/DB/cloud-SDK calls in async code or a `@task` not in `self.run_in_thread(...)` (with its own timeout).
- Flag: HTTP clients/DB connections created per item; HTTP calls or source queries with no timeout.
- Flag: unbounded whole-dataset loads (`json.load`, `f.read()`, all pages in one list); stream or use `RollingFileWriter`.
- Flag: N+1 calls where a batch exists; independent awaits run serially; `gather` over thousands of tasks without a `Semaphore`.
- Flag: long loops or opaque awaits reporting no progress: `self.heartbeat(...)`, or bound with `self.holding_progress(label, timeout=...)` (`run_in_thread` is already held). Stalls only warn unless `progress_watchdog="enforce"`.
- Flag: `duckdb`/`pandas`/`daft` at module top level (lazy import with `# noqa: PLC0415` and a reason).
- Flag new ones (CI only warns): blocking calls in `async def` (P023), `to_thread`/`run_in_executor(None)` (P031), a bare process pool (P036), stdlib `json` in hot paths (O001), INFO in loops (L006), eager debug args (L008).
- Severity: high if it blocks the loop, hangs, or can OOM on real data; medium otherwise; low for micro-optimisations.
