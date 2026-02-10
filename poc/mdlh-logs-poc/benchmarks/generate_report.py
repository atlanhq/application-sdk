#!/usr/bin/env python3
"""
Generate performance comparison report from benchmark results.

Usage:
    python3 benchmarks/generate_report.py --results benchmarks/results/benchmark_results.json
"""

import json
import argparse
from pathlib import Path

def generate_report(results_file, output_file=None):
    """Generate HTML report from benchmark results."""
    with open(results_file, 'r') as f:
        results = json.load(f)
    
    if output_file is None:
        output_file = results_file.replace('.json', '.html')
    
    # Sort by total time
    sorted_configs = sorted(results.items(), key=lambda x: x[1]['total_time'])
    
    # Generate HTML
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>P0 Query Performance Comparison</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: #333; }}
        table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
        th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
        th {{ background-color: #4CAF50; color: white; }}
        tr:nth-child(even) {{ background-color: #f2f2f2; }}
        .best {{ background-color: #d4edda; font-weight: bold; }}
        .metric {{ font-size: 0.9em; color: #666; }}
    </style>
</head>
<body>
    <h1>P0 Query Performance Comparison</h1>
    
    <h2>Summary</h2>
    <table>
        <tr>
            <th>Configuration</th>
            <th>Total Time (s)</th>
            <th>Plan Time (s)</th>
            <th>Scan Time (s)</th>
            <th>Sort Time (s)</th>
            <th>Files Scanned</th>
            <th>Rows After Filter</th>
            <th>Rows Returned</th>
        </tr>
"""
    
    best_time = min(r['total_time'] for r in results.values())
    
    for i, (config_name, result) in enumerate(sorted_configs):
        is_best = result['total_time'] == best_time
        row_class = 'best' if is_best else ''
        
        html += f"""        <tr class="{row_class}">
            <td>{config_name}</td>
            <td>{result['total_time']:.3f}</td>
            <td>{result['plan_time']:.3f}</td>
            <td>{result['scan_time']:.3f}</td>
            <td>{result['sort_time']:.3f}</td>
            <td>{result['files_scanned']}</td>
            <td>{result['rows_after_filter']:,}</td>
            <td>{result['rows_returned']:,}</td>
        </tr>
"""
    
    html += """    </table>
    
    <h2>Performance Analysis</h2>
    <ul>
"""
    
    # Add analysis
    for config_name, result in sorted_configs:
        speedup = best_time / result['total_time'] if result['total_time'] > 0 else 0
        html += f"""        <li><strong>{config_name}</strong>: {result['total_time']:.3f}s 
            ({speedup:.2f}x {'faster' if speedup > 1 else 'slower'} than best)</li>
"""
    
    html += """    </ul>
    
    <h2>Recommendations</h2>
    <p>Based on the benchmark results, the configuration with the lowest total time is recommended for production use.</p>
    
</body>
</html>
"""
    
    with open(output_file, 'w') as f:
        f.write(html)
    
    print(f"✅ Report generated: {output_file}")
    
    # Print summary to console
    print(f"\n{'='*60}")
    print("PERFORMANCE SUMMARY")
    print(f"{'='*60}")
    
    print(f"\n{'Config':<30} {'Total Time':<12} {'Speedup':<10}")
    print("-" * 55)
    
    for config_name, result in sorted_configs:
        speedup = best_time / result['total_time'] if result['total_time'] > 0 else 0
        marker = "🏆" if speedup == 1.0 else ""
        print(f"{config_name:<30} {result['total_time']:<12.3f} {speedup:<10.2f}x {marker}")

def main():
    parser = argparse.ArgumentParser(description='Generate performance report from benchmark results')
    parser.add_argument('--results', required=True, help='Input JSON results file')
    parser.add_argument('--output', help='Output HTML file (default: results_file.html)')
    
    args = parser.parse_args()
    
    generate_report(args.results, args.output)

if __name__ == "__main__":
    main()
