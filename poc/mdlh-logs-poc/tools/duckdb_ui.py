#!/usr/bin/env python3
"""
DuckDB Query UI for Iceberg tables with P0 query support.

Usage:
    cd tools
    streamlit run duckdb_ui.py
"""

import sys
from pathlib import Path

import streamlit as st
import duckdb
import pandas as pd
import time

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

# Page config
st.set_page_config(
    page_title="Workflow Log Explorer",
    page_icon="📋",
    layout="wide"
)

st.title("📋 Workflow Log Explorer")

# Import config after streamlit setup
from config import (
    get_catalog, fq, NAMESPACE,
    POLARIS_CATALOG_URI, CATALOG_NAME
)
from pyiceberg.expressions import EqualTo, And
import pyarrow.compute as pc


@st.cache_resource
def get_iceberg_catalog():
    """Get cached Iceberg catalog connection."""
    return get_catalog()


@st.cache_data(ttl=300)
def get_table_list():
    """Get list of tables in namespace."""
    catalog = get_iceberg_catalog()
    tables = catalog.list_tables(NAMESPACE)
    return [f"{ns}.{name}" for ns, name in tables]


@st.cache_data(ttl=60)
def get_workflow_names(table_name):
    """Get distinct workflow names for dropdown."""
    catalog = get_iceberg_catalog()
    table = catalog.load_table(table_name)
    
    # Scan just the workflow name column
    scan = table.scan(selected_fields=("atlan_argo_workflow_name",))
    arrow_table = scan.to_arrow()
    
    if len(arrow_table) == 0:
        return []
    
    # Get unique values
    unique_names = arrow_table.column("atlan_argo_workflow_name").unique().to_pylist()
    return sorted([n for n in unique_names if n])


def run_p0_query(table_name, workflow_name, workflow_node=None, limit=1000):
    """Run P0 query with Iceberg predicate pushdown."""
    catalog = get_iceberg_catalog()
    table = catalog.load_table(table_name)
    
    # Build filter
    row_filter = EqualTo("atlan_argo_workflow_name", workflow_name)
    if workflow_node:
        row_filter = And(row_filter, EqualTo("atlan_argo_workflow_node", workflow_node))
    
    # Scan with predicate pushdown
    start_time = time.time()
    
    scan = table.scan(
        row_filter=row_filter,
        selected_fields=("timestamp", "level", "message", "logger_name", 
                        "atlan_argo_workflow_name", "atlan_argo_workflow_node")
    )
    
    # Get plan info
    plan_files = list(scan.plan_files())
    files_scanned = len(plan_files)
    
    # Execute scan
    arrow_table = scan.to_arrow()
    rows_after_filter = len(arrow_table)
    
    # Sort and limit
    if rows_after_filter > 0:
        indices = pc.sort_indices(arrow_table, sort_keys=[("timestamp", "descending")])
        if len(indices) > limit:
            indices = indices[:limit]
        result_table = arrow_table.take(indices)
    else:
        result_table = arrow_table
    
    query_time = time.time() - start_time
    
    return result_table.to_pandas(), {
        'query_time': query_time,
        'files_scanned': files_scanned,
        'rows_after_filter': rows_after_filter,
        'rows_returned': len(result_table)
    }


# Sidebar
st.sidebar.header("Connection Info")
st.sidebar.text(f"Catalog: {CATALOG_NAME}")
st.sidebar.text(f"Namespace: {NAMESPACE}")

# Main content
tab1, tab2 = st.tabs(["P0 Query", "SQL Query"])

with tab1:
    st.header("P0: Container Logs Query")
    
    # Table selection
    tables = get_table_list()
    selected_table = st.selectbox("Select Table", tables, 
                                  index=tables.index(f"{NAMESPACE}.workflow_logs_wf_month") if f"{NAMESPACE}.workflow_logs_wf_month" in tables else 0)
    
    # Workflow selection
    workflow_names = get_workflow_names(selected_table)
    if workflow_names:
        selected_workflow = st.selectbox("Workflow Name", workflow_names)
        
        # Node filter (optional)
        workflow_node = st.text_input("Workflow Node (optional)")
        
        # Limit
        limit = st.slider("Limit", 100, 5000, 1000)
        
        if st.button("Fetch Logs"):
            with st.spinner("Querying Iceberg..."):
                df, metrics = run_p0_query(
                    selected_table, 
                    selected_workflow, 
                    workflow_node if workflow_node else None,
                    limit
                )
            
            # Show metrics
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Query Time", f"{metrics['query_time']:.2f}s")
            col2.metric("Files Scanned", metrics['files_scanned'])
            col3.metric("Rows Filtered", f"{metrics['rows_after_filter']:,}")
            col4.metric("Rows Returned", f"{metrics['rows_returned']:,}")
            
            # Show data
            st.dataframe(df, use_container_width=True)
            
            # Download button
            csv = df.to_csv(index=False)
            st.download_button("Download CSV", csv, "logs.csv", "text/csv")
    else:
        st.warning("No workflow names found in table")

with tab2:
    st.header("Custom SQL Query")
    st.warning("Note: Full table scans can be slow. Use P0 tab for filtered queries.")
    
    sql = st.text_area("SQL Query", "SELECT * FROM workflow_logs LIMIT 100")
    
    if st.button("Run Query"):
        with st.spinner("Running query..."):
            try:
                catalog = get_iceberg_catalog()
                table = catalog.load_table(selected_table)
                arrow_table = table.scan().to_arrow()
                
                con = duckdb.connect(":memory:")
                con.register("workflow_logs", arrow_table)
                
                result = con.execute(sql).fetchdf()
                st.dataframe(result, use_container_width=True)
            except Exception as e:
                st.error(f"Query error: {e}")
