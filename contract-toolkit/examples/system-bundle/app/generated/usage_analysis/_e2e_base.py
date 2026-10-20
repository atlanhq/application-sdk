# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e import SystemAppE2ETest


class UsageAnalysisGeneratedE2EBase(SystemAppE2ETest):
    connector_short_name = "system-bundle"
    # This entrypoint generates no manifest.json: its callers declare its DAG node.
    # Set manifest_path in the suite to a fixture DAG copied from a calling
    # connector's manifest, and required_dag_nodes to the nodes it runs.
    entrypoint = "usage-analysis"
