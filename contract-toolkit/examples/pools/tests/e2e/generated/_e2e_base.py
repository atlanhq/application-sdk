# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class PoolsExampleGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "pools-example"
    argo_package_name = "@atlan/pools-example"
    argo_template_name = "atlan-pools-example"
    app_service_url = "http://pools-example.pools-example-app.svc.cluster.local"
