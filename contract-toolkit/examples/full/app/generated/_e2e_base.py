# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import SQLAppE2ETest


class FullFeaturedGeneratedE2EBase(SQLAppE2ETest):
    connector_short_name = "full-featured"
    argo_package_name = "@atlan/full-featured"
    argo_template_name = "atlan-full-featured"
    app_service_url = "http://full-featured.full-featured-app.svc.cluster.local"
    connection_type = "postgres"
    connection_category = "database"
