#!/usr/bin/env python3
"""
Benchmark P0 queries on all Iceberg table configurations.

P0 Query: SELECT * WHERE atlan_argo_workflow_name = ? ORDER BY timestamp DESC LIMIT 1000

Measures:
- Query execution time
- Rows scanned
- Files scanned (if available)
- Result accuracy

Usage:
    python3 benchmarks/p0_queries.py --workflow-name "workflow-name" --runs 3
    python3 benchmarks/p0_queries.py --multi --output benchmark_results.json
"""

import sys
import time
import json
from pathlib import Path

import pyarrow.compute as pc

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_catalog, fq
from ingestion.schema import CONFIGURATIONS


def run_p0_query(table, workflow_name, workflow_node=None, limit=1000):
    """
    Run P0 query on a table using Iceberg predicate pushdown.
    
    This properly uses Iceberg's filtering so partitioning can skip irrelevant data.
    
    Query: SELECT * WHERE workflow_name = ? [AND workflow_node = ?] ORDER BY timestamp DESC LIMIT ?
    """
    from pyiceberg.expressions import EqualTo, And
    
    start_time = time.time()
    
    # Build row filter with predicate pushdown
    row_filter = EqualTo("atlan_argo_workflow_name", workflow_name)
    
    # Add node filter if specified
    if workflow_node:
        node_filter = EqualTo("atlan_argo_workflow_node", workflow_node)
        row_filter = And(row_filter, node_filter)
    
    # Create scan with PREDICATE PUSHDOWN - Iceberg will skip partitions!
    scan = table.scan(
        row_filter=row_filter,
        selected_fields=("timestamp", "level", "message", "atlan_argo_workflow_name", "atlan_argo_workflow_node")
    )
    
    # Get scan plan info
    plan_start = time.time()
    plan_files = list(scan.plan_files())
    num_files_scanned = len(plan_files)
    plan_time = time.time() - plan_start
    
    # Execute scan
    scan_start = time.time()
    arrow_table = scan.to_arrow()
    scan_time = time.time() - scan_start
    
    rows_after_filter = len(arrow_table)
    
    # Sort by timestamp DESC and limit
    sort_start = time.time()
    if rows_after_filter > 0:
        # Use PyArrow's sort_indices for efficiency
        indices = pc.sort_indices(arrow_table, sort_keys=[("timestamp", "descending")])
        # Take top 'limit' rows
        if len(indices) > limit:
            indices = indices[:limit]
        result_table = arrow_table.take(indices)
    else:
        result_table = arrow_table
    sort_time = time.time() - sort_start
    
    total_time = time.time() - start_time
    
    return {
        'plan_time': plan_time,
        'scan_time': scan_time,
        'sort_time': sort_time,
        'total_time': total_time,
        'files_scanned': num_files_scanned,
        'rows_after_filter': rows_after_filter,
        'rows_returned': len(result_table),
        'result_table': result_table
    }

def benchmark_all_configs(workflow_name, workflow_node=None, limit=1000, num_runs=3):
    """Benchmark P0 query on all configurations."""
    catalog = get_catalog()
    results = {}
    
    print(f"\n{'='*70}")
    print(f"Benchmarking P0 query:")
    print(f"  workflow_name = '{workflow_name}'")
    if workflow_node:
        print(f"  workflow_node = '{workflow_node[:60]}...'")
    print(f"  Limit: {limit} rows")
    print(f"  Runs per config: {num_runs}")
    print(f"{'='*70}")
    
    for config_name, config in CONFIGURATIONS.items():
        table_name = fq(config['table_name'])
        
        print(f"\n{config_name}: {table_name}")
        print(f"  Description: {config['description']}")
        
        try:
            table = catalog.load_table(table_name)
        except Exception as e:
            print(f"  ❌ Failed to load table: {e}")
            continue
        
        # Run query multiple times and average
        run_results = []
        for run in range(num_runs):
            result = run_p0_query(table, workflow_name, workflow_node, limit)
            run_results.append(result)
        
        # Calculate averages
        avg_result = {
            'config_name': config_name,
            'table_name': table_name,
            'plan_time': sum(r['plan_time'] for r in run_results) / len(run_results),
            'scan_time': sum(r['scan_time'] for r in run_results) / len(run_results),
            'sort_time': sum(r['sort_time'] for r in run_results) / len(run_results),
            'total_time': sum(r['total_time'] for r in run_results) / len(run_results),
            'files_scanned': run_results[0]['files_scanned'],  # Same for all runs
            'rows_after_filter': run_results[0]['rows_after_filter'],
            'rows_returned': run_results[0]['rows_returned']
        }
        
        results[config_name] = avg_result
        
        print(f"  ✅ Query complete (avg of {num_runs} runs):")
        print(f"     Total time: {avg_result['total_time']:.3f}s")
        print(f"     Plan: {avg_result['plan_time']:.3f}s")
        print(f"     Scan: {avg_result['scan_time']:.3f}s")
        print(f"     Sort: {avg_result['sort_time']:.3f}s")
        print(f"     Files scanned: {avg_result['files_scanned']}")
        print(f"     Rows after filter: {avg_result['rows_after_filter']:,}")
        print(f"     Rows returned: {avg_result['rows_returned']:,}")
    
    return results


def run_multi_benchmark(test_cases, limit=1000, num_runs=3):
    """Run benchmarks for multiple test cases."""
    all_results = {}
    
    for i, test_case in enumerate(test_cases):
        workflow_name = test_case['workflow_name']
        workflow_node = test_case.get('workflow_node')
        case_name = test_case.get('name', f"test_{i+1}")
        
        print(f"\n{'#'*70}")
        print(f"# TEST CASE {i+1}/{len(test_cases)}: {case_name}")
        print(f"{'#'*70}")
        
        results = benchmark_all_configs(workflow_name, workflow_node, limit, num_runs)
        all_results[case_name] = {
            'workflow_name': workflow_name,
            'workflow_node': workflow_node,
            'results': results
        }
    
    return all_results

def save_results(results, output_file):
    """Save benchmark results to JSON file."""
    # Remove result_table from results (not JSON serializable)
    serializable_results = {}
    for config_name, result in results.items():
        serializable_results[config_name] = {k: v for k, v in result.items() if k != 'result_table'}
    
    with open(output_file, 'w') as f:
        json.dump(serializable_results, f, indent=2)
    
    print(f"\n✅ Results saved to: {output_file}")

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Benchmark P0 queries on all configurations')
    parser.add_argument('--workflow-name', help='Workflow name to query (single test)')
    parser.add_argument('--workflow-node', help='Workflow node to query (optional)')
    parser.add_argument('--multi', action='store_true', help='Run multiple predefined test cases')
    parser.add_argument('--limit', type=int, default=1000, help='Result limit (default: 1000)')
    parser.add_argument('--runs', type=int, default=3, help='Number of runs per config (default: 3)')
    parser.add_argument('--output', default='benchmarks/results/benchmark_results.json', help='Output JSON file')
    
    args = parser.parse_args()
    
    if args.multi:
        # Define multiple test cases
        test_cases = [
            {
                'name': 'salesforce_workflow_only',
                'workflow_name': 'atlan-salesforce-1671795233-cron-1766791800',
                'workflow_node': None
            },
            {
                'name': 'salesforce_with_node',
                'workflow_name': 'atlan-salesforce-1671795233-cron-1766791800',
                'workflow_node': 'atlan-salesforce-1671795233-cron-1766791800(0).run(0)[6].extract(0).describe-reports(1:1)(1)'
            },
            {
                'name': 'analytics_workflow_only',
                'workflow_name': 'atlan-analytics-cron-1766801400',
                'workflow_node': None
            },
            {
                'name': 'analytics_with_node',
                'workflow_name': 'atlan-analytics-cron-1766801400',
                'workflow_node': 'atlan-analytics-cron-1766801400(0).custom-metadata-aggs(0)'
            },
            {
                'name': 'redshift_workflow_only',
                'workflow_name': 'atlan-redshift-miner-1659011583-cron-1767416400',
                'workflow_node': None
            },
        ]
        
        all_results = run_multi_benchmark(test_cases, args.limit, args.runs)
        
        # Print summary
        print(f"\n{'='*80}")
        print("MULTI-TEST SUMMARY")
        print(f"{'='*80}")
        
        for case_name, case_data in all_results.items():
            print(f"\n{case_name}:")
            print(f"  Workflow: {case_data['workflow_name']}")
            if case_data['workflow_node']:
                print(f"  Node: {case_data['workflow_node'][:50]}...")
            
            sorted_results = sorted(case_data['results'].items(), key=lambda x: x[1]['total_time'])
            best_config = sorted_results[0][0]
            best_time = sorted_results[0][1]['total_time']
            print(f"  🏆 Best: {best_config} ({best_time:.3f}s)")
        
        # Save all results
        save_results(all_results, args.output)
    
    elif args.workflow_name:
        results = benchmark_all_configs(args.workflow_name, args.workflow_node, args.limit, args.runs)
        
        # Print comparison
        print(f"\n{'='*60}")
        print("PERFORMANCE COMPARISON")
        print(f"{'='*60}")
        
        sorted_results = sorted(results.items(), key=lambda x: x[1]['total_time'])
        
        print(f"\n{'Config':<25} {'Total Time':<12} {'Files Scanned':<15} {'Rows After Filter':<18} {'Rows Returned':<15}")
        print("-" * 85)
        
        for config_name, result in sorted_results:
            print(f"{config_name:<25} {result['total_time']:<12.3f} {result['files_scanned']:<15} {result['rows_after_filter']:<18,} {result['rows_returned']:<15,}")
        
        # Save results
        save_results(results, args.output)
    else:
        parser.print_help()
        print("\nError: Either --workflow-name or --multi is required")

if __name__ == "__main__":
    main()
