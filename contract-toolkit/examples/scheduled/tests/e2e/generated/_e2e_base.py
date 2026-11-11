# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class ScheduledGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "scheduled"
    argo_package_name = "@atlan/scheduled"
    argo_template_name = "atlan-scheduled"
    app_service_url = "http://scheduled.scheduled-app.svc.cluster.local"
