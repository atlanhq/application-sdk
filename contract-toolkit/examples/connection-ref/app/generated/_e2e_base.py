# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import SQLAppE2ETest


class ConnectionRefGeneratedE2EBase(SQLAppE2ETest):
    connector_short_name = "connection-ref"
    argo_package_name = "@atlan/connection-ref"
    argo_template_name = "atlan-connection-ref"
    app_service_url = "http://connection-ref.connection-ref-app.svc.cluster.local"
    connection_type = "trino"
    connection_category = "database"
