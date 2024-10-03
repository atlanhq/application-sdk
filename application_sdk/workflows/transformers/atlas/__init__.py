from typing import Any, Dict, Optional

from application_sdk.workflows.transformers import TransformerInterface


class AtlasTransformer(TransformerInterface):
    @staticmethod
    def transform_metadata(
        connector_name: str, connector_type: str, typename: str, data: Dict[str, Any]
    ) -> Optional[Any]:
        raise NotImplementedError
