"""LakehouseInterface — facade bundling reader, writer, and catalog client.

One entry point for apps that read from upstream namespaces and write to their
own namespace. Apps construct this once with their catalog and ``app_namespace``
and use ``interface.reader`` / ``interface.writer`` everywhere else.
"""

from __future__ import annotations

from pyiceberg.catalog import Catalog

from application_sdk.lakehouse.catalog_client import CatalogClient
from application_sdk.lakehouse.reader import LakehouseReader
from application_sdk.lakehouse.writer import LakehouseWriter


class LakehouseInterface:
    """Facade exposing reader, writer, and catalog client for one app."""

    def __init__(self, catalog: Catalog, app_namespace: str) -> None:
        self._client = CatalogClient(catalog)
        self._reader = LakehouseReader(self._client)
        self._writer = LakehouseWriter(self._client, app_namespace)
        self._app_namespace = app_namespace

    @property
    def app_namespace(self) -> str:
        return self._app_namespace

    @property
    def catalog_client(self) -> CatalogClient:
        return self._client

    @property
    def reader(self) -> LakehouseReader:
        return self._reader

    @property
    def writer(self) -> LakehouseWriter:
        return self._writer
