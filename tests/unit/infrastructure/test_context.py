"""Tests for InfrastructureContext._temporal_client and get_temporal_client()."""

from __future__ import annotations

import dataclasses

from application_sdk.infrastructure.context import (
    InfrastructureContext,
    clear_infrastructure,
    get_temporal_client,
    set_infrastructure,
)


class TestTemporalClientOnContext:
    def teardown_method(self) -> None:
        clear_infrastructure()

    def test_field_defaults_to_none(self) -> None:
        ctx = InfrastructureContext()
        assert ctx._temporal_client is None

    def test_get_returns_none_when_no_context(self) -> None:
        clear_infrastructure()
        assert get_temporal_client() is None

    def test_get_returns_none_when_client_not_stashed(self) -> None:
        set_infrastructure(InfrastructureContext())
        assert get_temporal_client() is None

    def test_get_returns_stashed_client(self) -> None:
        sentinel = object()
        set_infrastructure(InfrastructureContext(_temporal_client=sentinel))
        assert get_temporal_client() is sentinel

    def test_replace_sets_client_and_preserves_other_fields(self) -> None:
        # Mirrors the main.py stash: replace() on the frozen context adds the
        # client without disturbing the already-built infra services, and leaves
        # the original instance untouched.
        storage = object()
        base = InfrastructureContext(storage=storage)
        client = object()

        updated = dataclasses.replace(base, _temporal_client=client)

        assert updated._temporal_client is client
        assert updated.storage is storage
        assert base._temporal_client is None
