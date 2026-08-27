"""Ephemeral kubectl port-forward helper for K8s e2e tests.

The implementation moved to
:mod:`application_sdk.testing.harness.cluster._portforward` with the typed
cluster backend (FND-241, child E on FND-224). It moved for direction, not for
tidiness: :meth:`ClusterReader.http` sits on it, and a harness module cannot
import from the package child H re-expresses *over* it without making a
``harness -> e2e -> harness`` cycle. Same reason ``_poll`` and the AE error
leaves moved in the earlier children.

This module re-exports the *same function object*, so
``from application_sdk.testing.e2e.portforward import kube_http_call`` and every
existing call site are unchanged.

New code that makes more than one call to the same Service should reach for
:func:`~application_sdk.testing.harness.cluster.port_forward` instead — one
tunnel for the batch rather than one ``kubectl`` process per call.
"""

from application_sdk.testing.harness.cluster._portforward import (
    PortForward,
    kube_http_call,
    port_forward,
)

__all__ = ["PortForward", "kube_http_call", "port_forward"]
