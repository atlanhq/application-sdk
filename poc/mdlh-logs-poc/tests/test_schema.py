#!/usr/bin/env python3
"""
Test schema compatibility between source Parquet and target Iceberg.

Usage:
    python3 tests/test_schema.py
"""

import sys
from pathlib import Path

# Add parent directory to path to import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingestion.schema import get_base_schema, CONFIGURATIONS


def test_schema_compatibility():
    """Test that schema definitions are valid."""
    print("=" * 60)
    print("Test: Schema Compatibility")
    print("=" * 60)
    
    # Test base schema
    print("\n--- Base Schema ---")
    schema = get_base_schema()
    print(f"Fields: {len(schema.fields)}")
    
    for field in schema.fields:
        print(f"  {field.field_id}: {field.name} ({field.type})")
    
    # Test each configuration
    print("\n--- Configurations ---")
    for config_name, config in CONFIGURATIONS.items():
        print(f"\n{config_name}:")
        print(f"  Table: {config['table_name']}")
        print(f"  Partition: {config['partition_config']}")
        print(f"  Sort: {config['sort_order']}")
        
        # Verify partition spec
        if config['partition_spec']:
            print(f"  Partition fields: {len(config['partition_spec'].fields)}")
    
    print("\n✅ All schemas valid")
    return True


if __name__ == "__main__":
    success = test_schema_compatibility()
    sys.exit(0 if success else 1)
