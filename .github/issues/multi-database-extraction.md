# 🗄️ Multi-Database Extraction in SQLExtraction

## 📝 Summary
Enhance SQLExtraction functionality to support extracting metadata from multiple databases in a single workflow execution.

## 💡 Basic Example
```python
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow

# Current: Single database extraction
workflow_args = {
    "connection_string": "postgresql://user:pass@host:5432/db1"
}

# Proposed: Multi-database extraction
workflow_args = {
    "databases": [
        {
            "name": "primary_db",
            "connection_string": "postgresql://user:pass@host:5432/db1"
        },
        {
            "name": "analytics_db", 
            "connection_string": "postgresql://user:pass@host:5432/db2"
        },
        {
            "name": "reporting_db",
            "connection_string": "mysql://user:pass@host:3306/reports"
        }
    ]
}
```

## 🎯 Motivation
- **Efficiency**: Extract metadata from multiple databases in a single workflow
- **Resource Optimization**: Reduce overhead of running separate workflows
- **Consistency**: Ensure consistent extraction timing across related databases
- **Enterprise Use Case**: Many organizations have multiple related databases that should be processed together

## 💼 Acceptance Criteria
- [ ] Design multi-database workflow architecture
- [ ] Update BaseSQLMetadataExtractionWorkflow to support multiple databases
- [ ] Implement parallel extraction with proper error handling
- [ ] Add configuration support for multiple database connections
- [ ] Handle different database types in single workflow
- [ ] Add progress tracking for multi-database extraction
- [ ] Update documentation and examples
- [ ] Add comprehensive tests for multi-database scenarios

## 🔧 Technical Requirements
- Support for different database types in single workflow
- Parallel extraction with configurable concurrency limits
- Individual database error handling (don't fail entire workflow)
- Progress reporting for each database
- Resource management for multiple connections
- Transaction isolation between databases

## ⚠️ Potential Challenges
- Managing connections to multiple databases simultaneously
- Handling different database schemas and authentication methods
- Error recovery strategies for partial failures
- Resource consumption with many concurrent connections

## 🏷️ Labels
- `enhancement`
- `sql-extraction`
- `workflow`
- `v0.2.0`

## 🔗 Reference
Related to Application SDK v0.2.0 release planning discussion