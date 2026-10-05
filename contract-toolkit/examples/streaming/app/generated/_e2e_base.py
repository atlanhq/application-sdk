# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class StreamingGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "streaming"
    argo_package_name = "@atlan/streaming"
    argo_template_name = "atlan-streaming"
    app_service_url = "http://streaming.streaming-app.svc.cluster.local"
