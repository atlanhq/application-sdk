"""Config store + /config endpoint contract tests.

Covers the two things the consolidated host depends on:
  1. per-app key scoping (multi-app hosts must not collide config trees), and
  2. the S3 store honoring the LocalFile contract (missing → None; save raises).
No boto3 required — the S3 client is faked.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from server_sdk.config.s3 import S3ConfigStore
from server_sdk.config.store import config_objectstore_key


# ---------------------------------------------------------------- key scoping
def test_key_uses_app_name_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "common-app-server")
    assert (
        config_objectstore_key("guid1", "credentials", app_name="redshift")
        == "persistent-artifacts/apps/redshift/credentials/guid1/config.json"
    )


def test_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "snowflake")
    assert (
        config_objectstore_key("guid2")
        == "persistent-artifacts/apps/snowflake/workflows/guid2/config.json"
    )


def test_two_apps_never_collide() -> None:
    k1 = config_objectstore_key("same-guid", "credentials", app_name="redshift")
    k2 = config_objectstore_key("same-guid", "credentials", app_name="snowflake")
    assert k1 != k2


@pytest.mark.parametrize("bad", ["../etc", "a b", "", "x" * 129])
def test_key_rejects_bad_ids(bad: str) -> None:
    with pytest.raises(ValueError):
        config_objectstore_key(bad, app_name="redshift")


def test_key_rejects_bad_app_name() -> None:
    with pytest.raises(ValueError):
        config_objectstore_key("guid", app_name="../sneaky")


# ---------------------------------------------------------------- S3 store
class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _NoSuchKey(Exception):
    def __init__(self) -> None:
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.fail_puts = False

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        try:
            return {"Body": _FakeBody(self.objects[(Bucket, Key)])}
        except KeyError:
            raise _NoSuchKey() from None

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
        if self.fail_puts:
            raise RuntimeError("AccessDenied")
        self.objects[(Bucket, Key)] = Body


def test_s3_roundtrip() -> None:
    fake = _FakeS3()
    store = S3ConfigStore("bkt", client=fake)
    key = config_objectstore_key("guid", "credentials", app_name="redshift")
    asyncio.run(store.save(key, {"a": 1}))
    assert asyncio.run(store.load(key)) == {"a": 1}
    assert ("bkt", key) in fake.objects


def test_s3_missing_is_none() -> None:
    store = S3ConfigStore("bkt", client=_FakeS3())
    assert (
        asyncio.run(store.load("persistent-artifacts/apps/x/workflows/y/config.json"))
        is None
    )


def test_s3_save_failure_raises() -> None:
    fake = _FakeS3()
    fake.fail_puts = True
    store = S3ConfigStore("bkt", client=fake)
    with pytest.raises(RuntimeError):
        asyncio.run(store.save("k", {"a": 1}))


def test_from_env_none_without_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("S3_BUCKET", raising=False)
    assert S3ConfigStore.from_env() is None
