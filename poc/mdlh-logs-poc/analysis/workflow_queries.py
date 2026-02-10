#!/usr/bin/env python3
"""
P0, P1, P2 Query Pattern Implementations

P0: Container logs by workflow (IMPLEMENTED)
P1: Live streaming logs (STUB)
P2: Log download preparation (STUB)

Usage:
    python3 analysis/workflow_queries.py
"""

import sys
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

import pyarrow.compute as pc

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_catalog, fq
from pyiceberg.expressions import EqualTo, And, GreaterThan


class WorkflowLogQueries:
    """Query patterns for workflow logs."""
    
    def __init__(self, table_name: str = "Workflow_Log_Test.workflow_logs_wf_month"):
        self.catalog = get_catalog()
        self.table = self.catalog.load_table(table_name)
    
    def p0_container_logs(
        self, 
        workflow_name: str, 
        workflow_node: Optional[str] = None,
        limit: int = 1000
    ) -> Dict[str, Any]:
        """
        P0: Get container logs for a specific workflow.
        
        This is the primary query pattern - fetch logs for debugging a workflow.
        
        Query: SELECT * WHERE workflow_name = ? [AND node = ?] ORDER BY timestamp DESC LIMIT ?
        """
        start_time = time.time()
        
        # Build filter
        row_filter = EqualTo("atlan_argo_workflow_name", workflow_name)
        if workflow_node:
            row_filter = And(row_filter, EqualTo("atlan_argo_workflow_node", workflow_node))
        
        # Scan with predicate pushdown
        scan = self.table.scan(
            row_filter=row_filter,
            selected_fields=("timestamp", "level", "message", "logger_name",
                           "atlan_argo_workflow_name", "atlan_argo_workflow_node")
        )
        
        # Execute
        arrow_table = scan.to_arrow()
        
        # Sort and limit
        if len(arrow_table) > 0:
            indices = pc.sort_indices(arrow_table, sort_keys=[("timestamp", "descending")])
            if len(indices) > limit:
                indices = indices[:limit]
            result = arrow_table.take(indices)
        else:
            result = arrow_table
        
        return {
            'logs': result.to_pylist(),
            'count': len(result),
            'query_time_ms': (time.time() - start_time) * 1000
        }
    
    def p1_live_streaming(
        self,
        workflow_name: str,
        since_timestamp: float,
        batch_size: int = 100
    ) -> Dict[str, Any]:
        """
        P1: Live streaming logs (polling pattern).
        
        Client polls periodically with the last seen timestamp to get new logs.
        
        Query: SELECT * WHERE workflow_name = ? AND timestamp > ? ORDER BY timestamp ASC LIMIT ?
        
        STUB: This is the ideation phase - implementation needed.
        """
        # Build filter
        row_filter = And(
            EqualTo("atlan_argo_workflow_name", workflow_name),
            GreaterThan("timestamp", since_timestamp)
        )
        
        # Note: In production, this would use:
        # 1. Cursor-based pagination
        # 2. Long-polling or SSE for near-realtime
        # 3. Caching of recently accessed workflows
        
        raise NotImplementedError(
            "P1 live streaming is not yet implemented. "
            "Current approach: client polls P0 with increasing timestamps."
        )
    
    def p2_download_preparation(
        self,
        workflow_name: str,
        output_format: str = "csv"
    ) -> Dict[str, Any]:
        """
        P2: Prepare logs for download.
        
        For large log sets, prepare a download file and return a URL.
        
        STUB: This is the ideation phase - implementation needed.
        """
        # In production, this would:
        # 1. Query full log set (no limit)
        # 2. Write to S3 as CSV/JSON
        # 3. Return pre-signed URL for download
        # 4. Clean up after TTL
        
        raise NotImplementedError(
            "P2 log download is not yet implemented. "
            "Would require: S3 write, pre-signed URL generation."
        )


def demo_p0():
    """Demonstrate P0 query."""
    print("=" * 60)
    print("P0 Query Demo: Container Logs")
    print("=" * 60)
    
    queries = WorkflowLogQueries()
    
    test_workflow = "atlan-salesforce-1671795233-cron-1766791800"
    
    print(f"\nQuerying logs for: {test_workflow}")
    result = queries.p0_container_logs(test_workflow, limit=10)
    
    print(f"\nResults: {result['count']} logs in {result['query_time_ms']:.1f}ms")
    
    if result['logs']:
        print("\nSample (first 3):")
        for log in result['logs'][:3]:
            print(f"  [{log['level']}] {log['message'][:60]}...")


if __name__ == "__main__":
    demo_p0()
