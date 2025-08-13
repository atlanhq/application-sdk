import re
from typing import Any, Dict, Optional

from application_sdk.clients.temporal import TemporalWorkflowClient
from application_sdk.clients.workflow import WorkflowEngineType
from application_sdk.constants import APPLICATION_NAME


def get_workflow_client(
    engine_type: WorkflowEngineType = WorkflowEngineType.TEMPORAL,
    application_name: str = APPLICATION_NAME,
):
    """
    Get a workflow client based on the engine type.

    Args:
        engine_type: The type of workflow engine to use
        application_name: The name of the application

    Returns:
        A workflow client instance
    """
    if engine_type == WorkflowEngineType.TEMPORAL:
        return TemporalWorkflowClient(application_name=application_name)
    else:
        raise ValueError(f"Unsupported workflow engine type: {engine_type}")


def get_cluster_identifier(self, aws_client) -> str:
    """
    Retrieve the cluster identifier from AWS Redshift clusters.

    Args:
        aws_client: Boto3 Redshift client instance

    Returns:
        str: The cluster identifier

    Raises:
        RuntimeError: If no clusters are found
    """
    clusters = aws_client.describe_clusters()

    for cluster in clusters["Clusters"]:
        cluster_identifier = cluster.get("ClusterIdentifier")
        if cluster_identifier:
            # Optionally, you can add logic to filter clusters if needed
            # we are reading first clusters ID if not provided
            return cluster_identifier  # Just return the string
    raise RuntimeError("No clusters found with a valid ClusterIdentifier.")


def extract_region_from_host(self, host: str) -> Optional[str]:
    """
    Extract AWS region from Redshift host string.

    Args:
        host: The Redshift host string

    Returns:
        Optional[str]: The extracted region or None if not found
    """
    # Matches: .<region>.<service>.amazonaws.com or -<region>.amazonaws.com
    match = re.search(r"\.([a-z]{2}-[a-z]+-\d)\.", host)
    if match:
        return match.group(1)
    # Some services may use - instead of . (rare)
    match = re.search(r"-([a-z]{2}-[a-z]+-\d)\.", host)
    if match:
        return match.group(1)
    return None


def get_cluster_credentials(
    self, aws_client, credentials: Dict[str, Any], extra: Dict[str, Any]
) -> Dict[str, str]:
    """
    Retrieve cluster credentials using IAM authentication.

    Args:
        aws_client: Boto3 Redshift client instance
        credentials: Dictionary containing connection credentials

    Returns:
        Dict[str, str]: Dictionary containing DbUser and DbPassword
    """
    database = extra["database"]
    cluster_identifier = credentials.get("cluster_id") or self.get_cluster_identifier(
        aws_client
    )
    return aws_client.get_cluster_credentials_with_iam(
        DbName=database,
        ClusterIdentifier=cluster_identifier,
        DurationSeconds=3600,
    )
