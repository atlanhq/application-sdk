# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import SQLAppE2ETest


class PublishControlsGeneratedE2EBase(SQLAppE2ETest):
    connector_short_name = "publish-controls"
    argo_package_name = "@atlan/publish-controls"
    argo_template_name = "atlan-publish-controls"
    app_service_url = "http://publish-controls.publish-controls-app.svc.cluster.local"
    connection_type = "trino"
    connection_category = "database"
