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

The seam is a constructor argument, never a patched global. Patching
``time.monotonic`` process-wide in an async test hands the same mock to the
asyncio event loop's own clock, which produces flaky ``StopIteration`` failures
that read as a bug in the code under test.

Also here: :func:`read_tenant_auth`, the other half of ``setup_method``'s
identity work — validating that the ambient environment actually carries a
tenant to talk to. It is a pure function of a mapping for the same reason the
clock is injected: an env read buried in a constructor is an env read no test
can drive.

Not here: resolving the ``$admin`` role GUID, which ``setup_method`` also does.
That is an Atlas read over the network, not a derivation from inputs, and its
home is :mod:`application_sdk.testing.harness.atlas` with the other Atlas reads.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from application_sdk.testing.harness._errors import MissingTenantEnvError

__all__ = [
    "ConnectionIdentity",
    "Minter",
    "TenantAuth",
    "read_tenant_auth",
]

#: Exclusive upper bound on the random half of a unique suffix. Six digits, so
#: the suffix is a fixed width and two runs landing in the same clock second
#: collide with probability 1e-6 rather than 1.
_RANDOM_SUFFIX_BOUND = 1_000_000

#: Width the random half is zero-padded to. Padding is what keeps the suffix a
#: fixed length: an unpadded 42 and an unpadded 420000 are the same number of
#: characters apart as two different seconds, so without it the clock half and
#: the random half are not separable by eye when reading a tenant's asset list.
_RANDOM_SUFFIX_WIDTH = 6


@dataclass(frozen=True, slots=True)
class ConnectionIdentity:
    """The ephemeral connection one harness run creates and then purges.

    Attributes:
        qualified_name: Atlan qualified name, ``default/<type>/<suffix>``. This
            is the string teardown purges under, so it is also the string a test
            has to be able to predict.
        display_name: Human-facing name on the same connection.
    """

    qualified_name: str
    display_name: str


@dataclass(frozen=True, slots=True)
class TenantAuth:
    """How a run authenticates against the tenant under test.

    Attributes:
        base_url: Tenant base URL, without a trailing slash.
        api_key: Atlan API key. Mandatory rather than optional because
            ``/automation/api/v1/*`` needs the realm-admin ``resource_access``
            role that only the API key's service account carries.
        oauth_client_id: Service-account client id, when one is configured.
        oauth_client_secret: Matching client secret.
    """

    base_url: str
    api_key: str
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None


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

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Minter:
        """Build a minter wired to the real clock and the ambient CI run id.

        Args:
            environ: Environment to read ``GITHUB_RUN_ID`` from. Passed in
                rather than read from :data:`os.environ`, so the one thing a
                caller cannot inject is the one thing that is genuinely
                ambient.

        Returns:
            A minter over :func:`time.time` and :func:`secrets.randbelow`.
        """
        return cls(
            clock=lambda: int(time.time()),
            randbelow=secrets.randbelow,
            run_id_env=environ.get("GITHUB_RUN_ID"),
        )

    def run_id(self) -> int:
        """Return the identifier for this run.

        Returns:
            The ambient CI run id when there is one and it is numeric, else a
            clock reading. Non-numeric is treated as absent rather than as an
            error: the run id is only ever used to scope names, so a locally-set
            ``GITHUB_RUN_ID=local`` should degrade to a clock reading, not fail
            the run before it starts.
        """
        ambient = self._run_id_env
        if ambient and ambient.isdigit():
            return int(ambient)
        return self._clock()

    def seed_version(self) -> int:
        """Return the version number to publish a seed DAG under.

        A bare clock reading, and deliberately **not** :meth:`run_id`. This
        number is how the harness later tells its own seed apart from the
        manifest AE published over it — ``BaseE2ETest._supersedes`` answers
        "did the tenant replace my seed?" by comparing the two — so two seeds of
        one workflow must not carry the same number. The ambient
        ``GITHUB_RUN_ID`` that :meth:`run_id` prefers is constant across every
        leg of one CI job, so seeding twice under it would report the second
        seed as the tenant's own manifest.

        Returns:
            The clock reading, in whole seconds. Opaque to AE beyond its
            ordering, which is why nothing here tries to make it meaningful.
        """
        return self._clock()

    def unique_suffix(self) -> str:
        """Return a suffix that makes a name unique within a tenant.

        Returns:
            A clock reading concatenated with a zero-padded random component.
            Two runs starting in the same second must not collide, because a
            collision means one run purges the other's assets. Kept purely
            numeric so Atlas never rejects the name for hyphens or alphabetic
            characters.
        """
        return (
            f"{self._clock()}"
            f"{self._randbelow(_RANDOM_SUFFIX_BOUND):0{_RANDOM_SUFFIX_WIDTH}d}"
        )

    def connection_identity(self, connection_type: str) -> ConnectionIdentity:
        """Mint the ephemeral connection identity for this run.

        The trailing segment must be unique per test *instance*: with the e2e
        matrix each suite runs as a separate parallel job whose setup can land in
        the same wall-clock second as another leg's, and rapid same-ref pushes
        overlap too. A shared qualified name would let one leg's teardown purge
        another leg's assets and mix its Atlas counts.

        Args:
            connection_type: Atlan catalog type segment — the connector's
                ``connection_type`` where it differs from its short name, else
                the short name.

        Returns:
            The qualified name and display name, both built from one suffix so
            they cannot disagree about which run they belong to.
        """
        suffix = self.unique_suffix()
        return ConnectionIdentity(
            qualified_name=f"default/{connection_type}/{suffix}",
            display_name=f"{connection_type}-{suffix}",
        )


def read_tenant_auth(environ: Mapping[str, str]) -> TenantAuth:
    """Read and validate the tenant credentials a run needs from the environment.

    Args:
        environ: Environment to read. Passed in rather than read from
            :data:`os.environ` so a test can drive every branch without mutating
            process state.

    Returns:
        The resolved credentials, with the base URL's trailing slash stripped so
        every downstream ``f"{base_url}/..."`` produces one separator rather than
        two. OAuth fields are ``None`` when unset — empty string and unset mean
        the same thing to the client, and only one of them can be passed on.

    Raises:
        MissingTenantEnvError: When ``ATLAN_BASE_URL`` or ``ATLAN_API_KEY`` is
            absent or blank.
    """
    base_url = environ.get("ATLAN_BASE_URL", "").strip().rstrip("/")
    api_key = environ.get("ATLAN_API_KEY", "").strip()
    missing = [
        name
        for name, value in (("ATLAN_BASE_URL", base_url), ("ATLAN_API_KEY", api_key))
        if not value
    ]
    if missing:
        raise MissingTenantEnvError(
            message=(
                f"The harness needs {' and '.join(missing)} to reach a tenant. "
                "ATLAN_API_KEY is mandatory because /automation/api/v1/* (AE "
                "workflow management) requires the realm-admin resource_access "
                "role that only the API key's service account carries."
            ),
            field=",".join(missing),
        )
    return TenantAuth(
        base_url=base_url,
        api_key=api_key,
        oauth_client_id=environ.get("SDR_CLIENT_ID", "").strip() or None,
        oauth_client_secret=environ.get("SDR_CLIENT_SECRET", "").strip() or None,
    )
