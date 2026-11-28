# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class ConnectionTreeGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "connection-tree"
    argo_package_name = "@atlan/connection-tree"
    argo_template_name = "atlan-connection-tree"
    app_service_url = "http://connection-tree.connection-tree-app.svc.cluster.local"
