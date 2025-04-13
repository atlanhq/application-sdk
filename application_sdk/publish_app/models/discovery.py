import os
from typing import Any, Dict, List, Set, Tuple

import networkx as nx

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs.objectstore import ObjectStoreInput
from application_sdk.publish_app.models.plan import DiffStatus

logger = get_logger(__name__)


def discover_directories(input_data_path: str) -> Dict[str, Dict[str, Any]]:
    """
    Discover directories containing asset data.

    Args:
        input_data_path: Base path for input data.

    Returns:
        Dict mapping asset types to information about discovered files.
    """
    logger.info(f"Discovering directories in {input_data_path}")

    result: Dict[str, Dict[str, Any]] = {}

    try:
        diff_files_list: List[str] = []
        if input_data_path:
            logger.info(f"Listing files from object store path: {input_data_path}")
            try:
                diff_files_list = ObjectStoreInput.list_files_from_object_store(
                    input_data_path
                )
                logger.info(f"Found {len(diff_files_list)} files in object store")
                logger.debug(f"Files: {diff_files_list}")
            except Exception as e:
                logger.error(f"Error listing files from object store: {str(e)}")
                raise e

        # Process files from object store
        for file_path in diff_files_list:
            # Extract DiffStatus and typeName from file path
            path_parts = file_path.split("/")
            diff_status = None
            asset_type = None

            for part in path_parts:
                if part.startswith("diff_status="):
                    diff_status = part.split("=")[1]
                elif part.startswith("type_name="):
                    asset_type = part.split("=")[1]

            if not diff_status or not asset_type or diff_status == DiffStatus.NO_DIFF:
                continue

            try:
                # FIXME(inishchith): Can we know the record count without reading the file?
                # this will help in case of publishing
                # df = daft.read_parquet(file_path)
                # record_count = df.count_rows()

                # Create entry for this asset type and diff type if not exists
                if asset_type not in result:
                    result[asset_type] = {}

                if diff_status not in result[asset_type]:
                    result[asset_type][diff_status] = {
                        "directory_prefix": os.path.dirname(file_path),
                        "estimated_file_count": 0,
                        "estimated_total_records": None,
                        "files": [],
                    }

                # Update counts and add file details
                result[asset_type][diff_status]["estimated_file_count"] += 1
                result[asset_type][diff_status]["files"].append(
                    {"file_path": file_path, "estimated_records": None}
                )

            except Exception as e:
                logger.error(f"Error reading parquet file {file_path}: {str(e)}")
                continue
    except Exception as e:
        logger.error(f"Error discovering directories: {str(e)}")

    return result


async def analyze_dependencies(
    discovered_types: List[str],
) -> Tuple[Dict[str, List[str]], Set[str], List[str]]:
    """
    Analyze dependencies between asset types.

    Args:
        discovered_types: List of discovered asset types.

    Returns:
        Tuple containing dependency graph and self-referential types.
    """
    logger.info(f"Analyzing dependencies for types: {discovered_types}")

    # Default dependencies based on common patterns
    default_dependencies = {
        "Database": [],
        "Schema": ["Database"],
        "Table": ["Schema"],
        "View": ["Schema"],
        "MaterializedView": ["Schema"],
        "Column": ["Table", "View", "MaterializedView"],
        "TablePartition": ["Table"],
    }
    publish_order = [
        ["Database"],
        ["Schema"],
        ["Table", "View", "MaterializedView"],
        ["Column", "TablePartition"],
    ]

    # FIXME(inishchith): a shunt currently to test out basic flow
    return default_dependencies, set(), publish_order

    # Initialize dependency graph with discovered types
    dependency_graph: Dict[str, List[str]] = {t: [] for t in discovered_types}

    # Apply default dependencies where applicable
    for asset_type in discovered_types:
        if asset_type in default_dependencies:
            # Only include dependencies that exist in discovered types
            dependency_graph[asset_type] = [
                dep
                for dep in default_dependencies[asset_type]
                if dep in discovered_types
            ]

    # In a real implementation, we would get more detailed typedefs from pyatlan here
    # For now, we'll use a simple approach to determine self-referential types
    self_referential_types = {"Project"} & set(discovered_types)

    # Validate the dependency graph for cycles
    try:
        graph = nx.DiGraph()
        for node, edges in dependency_graph.items():
            if not edges:
                graph.add_node(node)
            else:
                for edge in edges:
                    graph.add_edge(node, edge)

        # Try to identify cycles
        try:
            cycles = list(nx.simple_cycles(graph))
            if cycles:
                logger.warning(f"Dependency cycles detected: {cycles}")
                # Break cycles by removing an edge
                for cycle in cycles:
                    if len(cycle) > 1:
                        graph.remove_edge(cycle[0], cycle[1])
                        logger.info(
                            f"Removed edge {cycle[0]} -> {cycle[1]} to break cycle"
                        )
        except nx.NetworkXNoCycle:
            pass

        # Get topological sort
        try:
            sorted_types = list(nx.topological_sort(graph))
            logger.info(f"Topological sort order: {sorted_types}")

            # Reconstruct dependency graph from the directed graph
            # to ensure it's consistent with the topological sort
            dependency_graph = {
                node: list(graph.successors(node)) for node in graph.nodes()
            }

        except nx.NetworkXUnfeasible:
            logger.error("Could not perform topological sort, graph has cycles")

    except Exception as e:
        logger.error(f"Error analyzing dependencies: {str(e)}")

    return dependency_graph, self_referential_types
