# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import SQLAppE2ETest


class FaninGeneratedE2EBase(SQLAppE2ETest):
    connector_short_name = "fanin"
    argo_package_name = "@atlan/fanin"
    argo_template_name = "atlan-fanin"
    app_service_url = "http://fanin.fanin-app.svc.cluster.local"
    connection_type = "trino"
    connection_category = "database"
