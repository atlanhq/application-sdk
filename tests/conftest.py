"""Global test configuration."""

import os

# Disable the Dapr observability sink globally for all unit tests.
# Without this, metrics flushing tries to connect to the Dapr sidecar
# (http://127.0.0.1:3500), which isn't running in unit test environments
# and causes 60-second timeouts per test.
os.environ.setdefault("ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK", "false")

# mutmut sandbox compat (mutation testing, `poe mutation-diff`): mutmut's
# record_trampoline_hit() resolves the *relative* [tool.mutmut] source_paths
# against the current working directory on every mutated-function call during
# its stats phase, so any test that monkeypatch.chdir()s into a tmp_path
# crashes with FileNotFoundError (mutmut 3.6.0; resolve happens even though
# its result is only used by the max_stack_depth feature, which we leave at
# the -1 default). Replace it with the equivalent minus the cwd-sensitive
# resolve. Only active under `mutmut run` (MUTANT_UNDER_TEST is set there);
# plain pytest runs never import mutmut.
if os.environ.get("MUTANT_UNDER_TEST") is not None:
    import mutmut
    import mutmut.mutation.trampoline as _trampoline

    def _record_trampoline_hit_without_cwd_resolve(name: str) -> None:
        assert not name.startswith(
            "src."
        ), "Failed trampoline hit. Module name starts with `src.`, which is invalid"
        mutmut._stats.add(name)

    _trampoline.record_trampoline_hit = _record_trampoline_hit_without_cwd_resolve
