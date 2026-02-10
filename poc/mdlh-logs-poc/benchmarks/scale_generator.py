#!/usr/bin/env python3
"""
Scale Testing Framework for Workflow Logs

Generates realistic test data at production scale to test P0 query performance.

Usage:
    python3 benchmarks/scale_generator.py
"""

import sys
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any

import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_catalog, fq, ENTITY_TABLE


class WorkflowLogGenerator:
    """Generate realistic workflow log data for scale testing."""

    def __init__(self):
        # Realistic data based on your current logs
        self.workflow_names = [
            'atlan-mssql-prod-main-whzpt',
            'atlan-mssql-prod-extract-5lvj7',
            'atlan-mssql-prod-transform-8x9k2',
            'atlan-mssql-prod-load-3m4n5',
            'atlan-postgres-prod-sync-p7q8r',
            'atlan-mongodb-prod-backup-s2t3u',
            'atlan-redshift-prod-analyze-v4w5x',
            'atlan-snowflake-prod-query-y6z7a'
        ]

        self.workflow_nodes = [
            'run(0)[6].interim-extract-and-transform(0)[0].extraction-and-transformation',
            'run(0)[6].interim-extract-and-transform(0)[1].data-validation',
            'run(0)[6].interim-extract-and-transform(0)[2].schema-mapping',
            'run(0)[7].final-load(0)[0].bulk-insert',
            'run(0)[7].final-load(0)[1].constraint-validation',
            'run(0)[8].post-processing(0)[0].statistics-update'
        ]

        self.logger_names = [
            'app.utils', 'app.database', 'app.transform', 'app.validation',
            'py4j.java_gateway', 'urllib3.connectionpool', 'botocore', 'kafka'
        ]

        self.levels = ['INFO', 'WARNING', 'ERROR']
        self.level_weights = [0.85, 0.10, 0.05]  # Mostly INFO, some WARNING, few ERROR

    def generate_log_entry(self, base_timestamp: float, workflow_idx: int) -> Dict[str, Any]:
        """Generate a single realistic log entry."""

        # Add some time jitter (±30 seconds)
        timestamp = base_timestamp + random.uniform(-30, 30)

        level = random.choices(self.levels, weights=self.level_weights)[0]

        # Generate realistic messages based on level
        if level == 'ERROR':
            messages = [
                'Connection timeout to database',
                'Schema validation failed',
                'Bulk insert failed with constraint violation',
                'Network error during S3 upload',
                'Memory limit exceeded during transformation'
            ]
        elif level == 'WARNING':
            messages = [
                'Slow query detected (>5s)',
                'High memory usage detected',
                'Connection pool nearly exhausted',
                'Large result set may impact performance',
                'Deprecated API usage detected'
            ]
        else:  # INFO
            messages = [
                f'Database \'{random.choice(["MyDatabase", "ProductionDB", "AnalyticsDB"])}\' - ALL schemas in scope',
                f'Database \'{random.choice(["MyDatabase", "ProductionDB", "AnalyticsDB"])}\' - Found schemas: [...]',
                f'Processing table \'{random.choice(["users", "orders", "products", "logs"])}\' with {random.randint(1000, 100000)} rows',
                f'Extracted {random.randint(100, 10000)} records from source',
                f'Transformed {random.randint(50, 5000)} records successfully',
                f'Loaded {random.randint(100, 10000)} records to target',
                f'Workflow step completed in {random.uniform(0.1, 30):.2f}s'
            ]

        message = random.choice(messages)

        # Generate trace ID (some entries have it, some don't)
        trace_id = f"atlan-{random.choice(['mssql', 'postgres', 'mongodb', 'redshift'])}-{int(timestamp)}-{random.choice(['abc', 'def', 'ghi', 'jkl'])}" if random.random() < 0.7 else None

        # Generate struct data (simplified version of your extra field)
        extra_data = {
            'workflow_id': f"{self.workflow_names[workflow_idx].split('-')[1]}-{int(timestamp)}",
            'activity_id': str(random.randint(1, 10)),
            'request_id': f"{random.choice(['a1b2', 'c3d4', 'e5f6'])}-{random.randint(1000, 9999)}",
            'duration_ms': str(random.randint(100, 30000)) if random.random() < 0.6 else None,
            'status_code': str(random.choice([200, 201, 400, 404, 500])) if random.random() < 0.3 else None
        }

        return {
            'timestamp': timestamp,
            'level': level,
            'logger_name': random.choice(self.logger_names),
            'message': message,
            'file': f'app/{random.choice(["database", "transform", "utils", "api"])}.py',
            'line': random.randint(50, 500),
            'function': random.choice(['execute_query', 'transform_data', 'validate_schema', 'process_batch', 'connect_db']),
            'argo_workflow_run_id': None,  # Will be set for some entries
            'argo_workflow_run_uuid': None,
            'trace_id': trace_id,
            'atlan_argo_workflow_name': self.workflow_names[workflow_idx],
            'atlan_argo_workflow_node': random.choice(self.workflow_nodes),
            'extra': extra_data
        }

    def generate_batch(self, num_rows: int, start_time: datetime, workflow_distribution: List[float] = None) -> pa.Table:
        """Generate a batch of log entries."""

        if workflow_distribution is None:
            # Equal distribution across workflows
            workflow_distribution = [1.0 / len(self.workflow_names)] * len(self.workflow_names)

        entries = []

        for i in range(num_rows):
            # Select workflow based on distribution
            workflow_idx = random.choices(range(len(self.workflow_names)), weights=workflow_distribution)[0]

            # Add time progression (logs arrive over time)
            time_offset = timedelta(seconds=i * random.uniform(0.1, 5))  # 0.1-5 seconds between logs
            entry_time = start_time + time_offset

            entry = self.generate_log_entry(entry_time.timestamp(), workflow_idx)
            entries.append(entry)

        # Convert to PyArrow table
        schema = pa.schema([
            ('timestamp', pa.float64()),
            ('level', pa.string()),
            ('logger_name', pa.string()),
            ('message', pa.string()),
            ('file', pa.string()),
            ('line', pa.int64()),
            ('function', pa.string()),
            ('argo_workflow_run_id', pa.string()),
            ('argo_workflow_run_uuid', pa.string()),
            ('trace_id', pa.string()),
            ('atlan_argo_workflow_name', pa.string()),
            ('atlan_argo_workflow_node', pa.string()),
            ('extra', pa.struct({
                'workflow_id': pa.string(),
                'activity_id': pa.string(),
                'request_id': pa.string(),
                'duration_ms': pa.string(),
                'status_code': pa.string()
            }))
        ])

        return pa.Table.from_pylist(entries, schema=schema)

class ScaleTestRunner:
    """Run scale tests with generated data."""

    def __init__(self):
        self.generator = WorkflowLogGenerator()

    def test_p0_scaling(self, test_sizes: List[int]):
        """Test P0 query performance at different scales."""

        print("=" * 60)
        print("🧪 P0 SCALE TESTING")
        print("=" * 60)

        results = []

        for num_rows in test_sizes:
            print(f"\n--- Testing with {num_rows:,} rows ---")

            # Generate test data
            start_time = datetime.now() - timedelta(hours=1)
            table = self.generator.generate_batch(num_rows, start_time)

            # Test P0 query (using first workflow)
            test_workflow = self.generator.workflow_names[0]
            print(f"Testing P0 query: workflow_name = '{test_workflow}'")

            # Apply filter
            start_filter = time.time()
            name_col = table.column('atlan_argo_workflow_name')
            mask = pc.equal(name_col, test_workflow)
            filtered_table = pc.filter(table, mask)
            filter_time = time.time() - start_filter

            # Sort results (manual sorting for PyArrow compatibility)
            start_sort = time.time()
            if len(filtered_table) > 0:
                # Create timestamp-index pairs and sort manually
                timestamp_pairs = []
                timestamps = filtered_table.column('timestamp')
                for i in range(len(timestamps)):
                    ts = timestamps[i].as_py()
                    if ts is not None:
                        timestamp_pairs.append((ts, i))

                timestamp_pairs.sort(key=lambda x: x[0], reverse=True)
                top_indices = [idx for _, idx in timestamp_pairs[:1000]]
                sorted_table = filtered_table.take(top_indices)
            else:
                sorted_table = filtered_table
            sort_time = time.time() - start_sort

            total_time = filter_time + sort_time
            memory_mb = table.nbytes / (1024*1024)

            print(f"  Total time: {total_time:.3f}s")
            print(f"  Filtered rows: {len(filtered_table):,}")
            print(f"  Result rows: {len(sorted_table):,}")
            print(f"  Memory usage: {memory_mb:.1f} MB")

            results.append({
                'rows': num_rows,
                'filter_time': filter_time,
                'sort_time': sort_time,
                'total_time': total_time,
                'memory_mb': memory_mb,
                'filtered_rows': len(filtered_table),
                'result_rows': len(sorted_table)
            })

        # Print scaling analysis
        print("\n" + "=" * 60)
        print("📊 SCALING ANALYSIS")
        print("=" * 60)

        if len(results) >= 2:
            baseline = results[0]
            for result in results[1:]:
                scale_factor = result['rows'] / baseline['rows']
                time_increase = result['total_time'] / baseline['total_time']
                memory_increase = result['memory_mb'] / baseline['memory_mb']

                print(f"Scale {scale_factor:.0f}x:")
                print(f"  Time increase: {time_increase:.1f}x")
                print(f"  Memory increase: {memory_increase:.1f}x")
                print(f"  Expected (linear): {scale_factor:.1f}x")
                print()

        return results

    def test_selective_scanning(self, total_rows: int = 100000):
        """Test selective scanning optimization."""

        print("\n" + "=" * 60)
        print("🎯 SELECTIVE SCANNING TEST")
        print("=" * 60)

        # Generate large dataset
        start_time = datetime.now() - timedelta(hours=1)
        table = self.generator.generate_batch(total_rows, start_time)

        print(f"Generated {total_rows:,} rows ({table.nbytes / (1024*1024):.1f} MB)")

        # Test 1: Full scan + filter (current approach)
        print("\n1. Full scan + filter:")
        start = time.time()
        name_col = table.column('atlan_argo_workflow_name')
        mask = pc.equal(name_col, self.generator.workflow_names[0])
        filtered = pc.filter(table, mask)
        full_scan_time = time.time() - start

        print(f"   Time: {full_scan_time:.3f}s")
        print(f"   Result: {len(filtered)} rows")

        # Test 2: Selective column access
        print("\n2. Selective columns only:")
        start = time.time()
        # Only access the columns we need for filtering and results
        name_col_only = table.column('atlan_argo_workflow_name')
        timestamp_col_only = table.column('timestamp')
        message_col_only = table.column('message')

        # Apply filter on minimal data
        mask_selective = pc.equal(name_col_only, self.generator.workflow_names[0])
        filtered_timestamps = pc.filter(timestamp_col_only, mask_selective)
        filtered_messages = pc.filter(message_col_only, mask_selective)
        selective_time = time.time() - start

        print(f"   Time: {selective_time:.3f}s")
        print(f"   Result: {len(filtered_timestamps)} rows")
        print(f"   Speedup: {full_scan_time / selective_time:.2f}x")

        return {
            'full_scan_time': full_scan_time,
            'selective_time': selective_time,
            'improvement': full_scan_time / selective_time if selective_time > 0 else 0
        }

def run_scale_tests():
    """Run comprehensive scale testing."""

    print("🚀 WORKFLOW LOG SCALE TESTING")
    print("Testing P0 query performance at production scales\n")

    runner = ScaleTestRunner()

    # Test different scales
    test_sizes = [1000, 10000, 50000, 100000]  # Up to 100K rows for testing

    print("Testing P0 query scaling...")
    p0_results = runner.test_p0_scaling(test_sizes)

    # Test selective scanning
    selective_results = runner.test_selective_scanning(50000)

    print("\n" + "=" * 60)
    print("🎯 KEY FINDINGS")
    print("=" * 60)

    if p0_results:
        baseline = p0_results[0]
        largest = p0_results[-1]

        scale_factor = largest['rows'] / baseline['rows']
        time_factor = largest['total_time'] / baseline['total_time']

        print(f"📈 Scaling: {scale_factor:.0f}x more data = {time_factor:.1f}x slower queries")
        print(f"⏱️ At {largest['rows']:,} rows: {largest['total_time']:.1f}s query time")
        print(f"💾 Memory: {largest['memory_mb']:.0f} MB for {largest['rows']:,} rows")

    if selective_results['improvement'] > 1:
        print(f"⚡ Selective scanning: {selective_results['improvement']:.1f}x faster")

    print("\n🔧 RECOMMENDATIONS:")
    print("1. Implement partitioning by time (essential for 1M+ rows)")
    print("2. Use selective column projection")
    print("3. Add query result pagination")
    print("4. Consider pre-computing frequent workflow queries")
    print("5. Test with your actual data distribution")

if __name__ == "__main__":
    run_scale_tests()
