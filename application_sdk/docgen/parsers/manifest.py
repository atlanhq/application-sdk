import enum
from typing import Any, Dict, Optional, Tuple

import yaml

from application_sdk.docgen.models.manifest import (
    CustomerDocsManifest,
    InternalDocsManifest,
)


class ManifestFileKind(enum.Enum):
    CUSTOMER = "customer"
    INTERNAL = "internal"


class ManifestParser:
    def __init__(self, docs_directory: str) -> None:
        self.docs_directory = docs_directory
        self.customer_manifest_path = f"{docs_directory}/docs.customer.manifest.yaml"
        self.internal_manifest_path = f"{docs_directory}/docs.internal.manifest.yaml"

    def read_manifest_file(self, file_path: str) -> Dict[str, Any]:
        with open(file_path, "r") as f:
            manifest = yaml.safe_load(f)
            return manifest

    def parse(self) -> Tuple[CustomerDocsManifest, Optional[InternalDocsManifest]]:
        customer_manifest_dict = self.read_manifest_file(self.customer_manifest_path)

        internal_manifest_dict = None
        if self.internal_manifest_path:
            internal_manifest_dict = self.read_manifest_file(
                self.internal_manifest_path
            )
            if not internal_manifest_dict:
                internal_manifest_dict = None

        customer_manifest = CustomerDocsManifest(**customer_manifest_dict)

        if internal_manifest_dict:
            internal_manifest = InternalDocsManifest(**internal_manifest_dict)
        else:
            internal_manifest = None

        return customer_manifest, internal_manifest
