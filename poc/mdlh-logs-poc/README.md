# MDLH Workflow Logs POC

> **Status:** POC Complete | Paused for handover  
> **Last Updated:** January 2026  
> **Handover Doc:** See [CONFLUENCE.md](CONFLUENCE.md) for full context

A proof-of-concept for storing and querying Argo workflow logs using Apache Iceberg on MDLH (Managed Data Lakehouse), with a FastAPI REST API for log retrieval.

## TL;DR

- **Problem:** Need visibility into workflow container logs
- **Solution:** Store logs in Iceberg tables, query via REST API
- **Current State:** P0 query (container logs) working end-to-end
- **Performance:** ~1.3s for 436K rows with 99% partition pruning

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           WORKFLOW LOG PIPELINE                             │
└─────────────────────────────────────────────────────────────────────────────┘

┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   Temporal   │    │     S3       │    │   Polaris    │    │   REST API   │
│   Workflow   │───▶│   Parquet    │───▶│   Iceberg    │───▶│   FastAPI    │
│              │    │    Files     │    │    Table     │    │              │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
       │                   │                   │                   │
       ▼                   ▼                   ▼                   ▼
 application-sdk     Hive partitioned    workflow_name +     P0: Container
 AtlanLoggerAdapter  year/month/day      month partition     logs query
```

### Components

| Component | Location | Purpose |
|-----------|----------|---------|
| **Log Producer** | `application-sdk` | SDK's `AtlanLoggerAdapter` writes logs to Parquet via Dapr |
| **Ingestion POC** | `mdlh_test/ingestion/` | Scripts to load S3 Parquet into Iceberg tables |
| **REST API** | `atlan-logs-app/` | FastAPI service for querying logs |
| **Benchmarks** | `mdlh_test/benchmarks/` | Performance testing for partition strategies |

---

## Key Findings

### Partition Strategy (Benchmark Results)

We tested 3 partition strategies:

| Configuration | Total Time | Files Scanned | Rows Matched |
|--------------|------------|---------------|--------------|
| `wf_bucket_month` | 1.278s | 14 | 436,985 |
| `wf_bucket` | 1.298s | 14 | 436,985 |
| **`wf_month`** | 1.399s | 9 | 436,985 |

**Winner:** `wf_month` (workflow_name + month identity partition)
- Fewer files scanned (9 vs 14)
- Best partition pruning for P0 queries
- Simple, predictable partitioning

### Schema Design Decision

Extracted key fields to top-level for predicate pushdown:
- `atlan_argo_workflow_name` - enables partition pruning
- `atlan_argo_workflow_node` - enables node-level filtering
- `trace_id` - correlation ID

---

## Project Structure

```
mdlh_test/
├── README.md                 # This file
├── CONFLUENCE.md             # Handover document for Confluence
├── config.py                 # Shared Polaris/AWS configuration
├── .env.example              # Environment template
│
├── ingestion/                # Data pipeline scripts
│   ├── s3_to_iceberg.py     # Main ingestion (parallel S3 → Iceberg)
│   ├── schema.py            # Iceberg schema definitions
│   └── ...
│
├── benchmarks/               # Performance testing
│   ├── p0_queries.py        # P0 query benchmarks
│   ├── results/             # Benchmark output (JSON, HTML)
│   └── ...
│
├── tools/                    # Debugging utilities
│   ├── duckdb_cli.py        # Interactive SQL CLI
│   ├── duckdb_ui.py         # Streamlit web UI
│   └── ...
│
├── analysis/                 # Performance analysis
│   ├── partition_analysis.py
│   └── ...
│
└── tests/                    # Test scripts
    └── ...
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- Access to Polaris catalog (mdlh-aws for testing)
- AWS credentials for S3 access

### Setup

```bash
# Clone and setup
cd mdlh_test
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt  # (or install pyiceberg, pyarrow, etc.)

# Configure credentials
cp .env.example .env
# Edit .env with your Polaris/AWS credentials
```

### Run the API

```bash
cd ../atlan-logs-app
./run.sh
# API available at http://localhost:8001/docs
```

### Query Logs

```bash
curl -X POST http://localhost:8001/logs/v1/container \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_name": "atlan-salesforce-1671795233-cron-1766791800",
    "limit": 100
  }'
```

---

## What's Working (P0)

- [x] End-to-end data flow (SDK → S3 → Iceberg → API)
- [x] P0 query: Get container logs by workflow name
- [x] Predicate pushdown with partition pruning
- [x] Benchmarking framework
- [x] Developer tools (DuckDB CLI/UI)

## What's Pending

- [ ] MDLH native ingestion (replace POC scripts)
- [ ] P1: Live streaming logs
- [ ] P2: Log download
- [ ] K8s config injection (currently hardcoded)
- [ ] Multi-tenant catalog switching
- [ ] Production deployment

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Log Storage | Apache Iceberg on S3 |
| Catalog | Polaris REST Catalog |
| Ingestion | PyIceberg + PyArrow |
| API | FastAPI + Uvicorn |
| Query Engine | PyIceberg scan + PyArrow compute |

---

## Related Repositories

- **application-sdk**: Log producer (AtlanLoggerAdapter)
- **atlan-logs-app**: REST API for log queries

---

## Contact

For questions about this POC, see the [CONFLUENCE.md](CONFLUENCE.md) handover document.
