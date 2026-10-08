"""Full-DAG e2e base for system apps (connection-delete, popularity, publish, …).

A system app runs only inside a tenant, never in SDR mode, and its production
DAGs are usually declared by the connectors that call it rather than served by
the app itself. :class:`BaseE2ETest` cannot run such a suite as-is: it submits
through Heracles, which re-derives the graph from the named app's served
manifest — an app that serves none fails that submit outright, and one that does
gets its manifest republished over the harness's DAG.

:class:`SystemAppE2ETest` submits straight to AE instead
(:meth:`~application_sdk.testing.harness.automation_engine.AEClient.submit_published_version`):
AE starts the version the harness just published, with no manifest fetch, so
the graph that runs is exactly the one the suite seeded. Everything else — the
seed DAG from ``manifest_path``, node routing, polling, grading and teardown —
is :class:`BaseE2ETest`'s.

What a suite declares:

* ``connector_short_name`` — the app's name; the only required attribute.
  ``argo_package_name`` / ``argo_template_name`` name a Heracles envelope this
  path never sends, so they are optional here.
* ``manifest_path`` — a JSON file whose top-level ``dag`` is the graph to run:
  the app's own generated manifest when it has one, else a fixture copied from
  the connector manifest that declares the node.
* ``required_dag_nodes`` — the nodes that must succeed. The default names a
  connector's ``extract`` / ``publish`` and will not match a system app's DAG.
* Run-scoped arguments go through :meth:`BaseE2ETest._mustache_substitutions`
  exactly as for a connector: the seed DAG's exact-match ``{{...}}`` strings are
  replaced from the model it returns. Subclass the substitutions model with an
  aliased field per placeholder and return an instance from the override.
  Nothing else substitutes on this path, so every placeholder the DAG carries
  needs a field.

Node routing is unchanged: only a node named ``extract`` is pointed at the CI
worker, and every other node keeps its manifest task queue with
``{deployment_name}`` resolved to the tenant's — which, with the app installed
on the tenant, is where a system app's node runs.

A suite built on this class runs only on the system-app tenant pool; see
:mod:`application_sdk.testing.e2e.tenant_pool`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e.base import BaseE2ETest
from application_sdk.testing.e2e.tenant_pool import TenantPool

logger = get_logger(__name__)

__all__ = ["SystemAppE2ETest"]


class SystemAppE2ETest(BaseE2ETest):
    """Pytest base for a system app's full-DAG suite, submitted straight to AE."""

    _required_class_attrs: ClassVar[tuple[str, ...]] = ("connector_short_name",)
    _tenant_pool: ClassVar[TenantPool] = TenantPool.SYSTEM

    # Nothing republishes over the seed on this path, so there is no deployed
    # manifest to compare; see _assert_deployed_manifest_matches below.
    assert_deployed_manifest: ClassVar[bool] = False

    # A system app acts on connections other apps own. A suite whose DAG does
    # land a Connection (or lineage) says so, as a connector suite would.
    expect_connection: ClassVar[bool] = False
    expect_lineage: ClassVar[bool] = False

    async def _submit(self, payload: dict[str, Any], *, slug: str) -> str:
        """Start the version :meth:`_bootstrap_workflow` published, via AE.

        *payload* is the Heracles envelope :class:`BaseE2ETest` builds for every
        run; this path sends no envelope, so it is unused.
        """
        del payload
        return await self._ae.submit_published_version(slug)

    async def _assert_deployed_manifest_matches(self, slug: str) -> None:
        """No-op: AE ran the published seed, and nothing republished over it."""
        logger.info(
            "Deployed-manifest check not applicable for %s: submitted straight "
            "to AE against slug %s, so the executed DAG is the seeded one",
            type(self).__name__,
            slug,
        )
