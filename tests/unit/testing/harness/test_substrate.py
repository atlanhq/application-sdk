"""Unit tests for substrate selection (child I on FND-224).

The property under test is that substrate is **declared, never detected**. The
tempting alternative — "is there a usable kubeconfig?" — is almost always yes on
a developer's machine, so a harness that answered it by reaching for the ambient
context would read whichever cluster ``kubectl`` last pointed at. Every test here
therefore asserts on what the declaration produced, and the local substrate's
test asserts a *refusal* rather than a fallback.
"""

from __future__ import annotations

import pytest

from application_sdk.testing.harness import (
    HarnessNotBuiltError,
    Substrate,
    SubstrateHasNoClusterError,
    cluster_reader_for,
)
from application_sdk.testing.harness.cluster import ClusterReader, CustomResourceReader


def test_the_kubeconfig_substrate_builds_a_reader_without_connecting() -> None:
    """Construction reads no kubeconfig: the API bundle is built per thread on
    first read, so an unusable context surfaces at the read that needed it."""
    reader = cluster_reader_for(Substrate.KUBECONFIG, kube_context="e2e-gcp")
    assert isinstance(reader, ClusterReader)
    assert isinstance(reader, CustomResourceReader)
    assert reader.kube_context == "e2e-gcp"


def test_no_context_means_the_kubeconfigs_current_one() -> None:
    assert cluster_reader_for(Substrate.KUBECONFIG).kube_context is None


def test_the_local_substrate_refuses_rather_than_falling_back() -> None:
    """A reader that failed on first use, or one that quietly read the ambient
    cluster, are both worse than a refusal at the seam."""
    with pytest.raises(SubstrateHasNoClusterError) as caught:
        cluster_reader_for(Substrate.LOCAL)
    assert caught.value.substrate == "local"
    assert "Substrate.KUBECONFIG" in str(caught.value)


def test_the_in_cluster_substrate_names_its_child_issue() -> None:
    """An unbuilt backend is an ``UNIMPLEMENTED`` leaf carrying the issue as a
    field, so an audit of what is left can enumerate it rather than grep prose."""
    with pytest.raises(HarnessNotBuiltError) as caught:
        cluster_reader_for(Substrate.IN_CLUSTER)
    assert caught.value.issue == "FND-248"
    assert isinstance(caught.value, NotImplementedError)


def test_the_substrate_values_are_stable_strings() -> None:
    """A scenario suite reads its substrate out of a config file, so the wire
    values are part of the contract, not just the member names."""
    assert [member.value for member in Substrate] == [
        "local",
        "kubeconfig",
        "in_cluster",
    ]
