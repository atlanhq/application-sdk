# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import BaseE2ETest


class TreeFiltersGeneratedE2EBase(BaseE2ETest):
    connector_short_name = "tree-filters"
    argo_package_name = "@atlan/tree-filters"
    argo_template_name = "atlan-tree-filters"
    app_service_url = "http://tree-filters.tree-filters-app.svc.cluster.local"
