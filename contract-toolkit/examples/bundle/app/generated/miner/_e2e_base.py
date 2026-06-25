# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class MinerGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "miner"
    argo_package_name = "@atlan/miner"
    argo_template_name = "atlan-miner"
    app_service_url = "http://miner.miner-app.svc.cluster.local"
