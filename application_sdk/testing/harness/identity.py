"""Run-id and unique-name minting, behind a clock seam.

Today ``BaseE2ETest`` mints these inline in ``setup_method``: a run id from
``GITHUB_RUN_ID`` falling back to ``int(time.time())``, and a unique connection
suffix from ``int(time.time())`` plus six random digits. Both read the wall clock
directly, which is why the names they produce cannot be asserted on — the only
available test is "it looks roughly right".

Putting the clock behind a seam makes the minted names a *function of inputs*, so
a test can pin the exact qualified name a run will produce. That matters more
than it sounds: the ephemeral qualified name is what ``teardown.py`` purges, and
a name the test cannot predict is a purge the test cannot verify.

Implementation is child B on FND-224.
"""

from __future__ import annotations

from collections.abc import Callable

from application_sdk.testing.harness._errors import HarnessNotBuiltError

__all__ = ["Minter"]


class Minter:
    """Mints the per-run identifiers a harness run needs.

    Args:
        clock: Wall-clock source, in whole seconds. Injected rather than read
            from :func:`time.time` so minted names are assertable. Note this is
            deliberately *not* the monotonic clock the wait budgets use: these
            identifiers want a value that is stable across processes and
            recognisable in a tenant's asset list.
        randbelow: Source of the random suffix, matching
            :func:`secrets.randbelow`'s signature.
        run_id_env: Ambient run identifier (a CI run id) to prefer over the
            clock, or ``None`` to always mint from the clock.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], int],
        randbelow: Callable[[int], int],
        run_id_env: str | None = None,
    ) -> None:
        self._clock = clock
        self._randbelow = randbelow
        self._run_id_env = run_id_env

    def run_id(self) -> int:
        """Return the identifier for this run.

        Returns:
            The ambient CI run id when there is one and it is numeric, else a
            clock reading.

        Raises:
            HarnessNotBuiltError: Always — implementation is child B on FND-224.
        """
        raise HarnessNotBuiltError(
            message="Minter.run_id is not implemented yet",
            operation="Minter.run_id",
            reason="child B on FND-224",
            issue="FND-224",
            component="harness_identity",
        )

    def unique_suffix(self) -> str:
        """Return a suffix that makes a name unique within a tenant.

        Returns:
            A clock reading concatenated with a zero-padded random component.
            Two runs starting in the same second must not collide, because a
            collision means one run purges the other's assets.

        Raises:
            HarnessNotBuiltError: Always — implementation is child B on FND-224.
        """
        raise HarnessNotBuiltError(
            message="Minter.unique_suffix is not implemented yet",
            operation="Minter.unique_suffix",
            reason="child B on FND-224",
            issue="FND-224",
            component="harness_identity",
        )
