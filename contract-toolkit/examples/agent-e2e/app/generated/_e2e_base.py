# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class AgentE2eGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "agent-e2e"
    argo_package_name = "@atlan/agent-e2e"
    argo_template_name = "atlan-agent-e2e"
    app_service_url = "http://agent-e2e.agent-e2e-app.svc.cluster.local"
    connection_type = "api"
    connection_category = "API"
