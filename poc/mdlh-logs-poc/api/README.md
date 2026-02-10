# API

The REST API for querying workflow logs is in a separate repository: **atlan-logs-app**

## Location

```
../atlan-logs-app/
```

Or in the same workspace at:
```
/Users/prasanna.sairam/work/atlan-logs-app/
```

## Quick Reference

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | API info |
| `/server/health` | GET | Liveness probe |
| `/server/ready` | GET | Readiness probe |
| `/logs/v1/container` | POST | **P0: Get container logs** |

### P0 Query Example

```bash
curl -X POST http://localhost:8001/logs/v1/container \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_name": "atlan-salesforce-1671795233-cron-1766791800",
    "workflow_node": null,
    "limit": 100
  }'
```

### Response

```json
{
  "success": true,
  "data": {
    "logs": [...],
    "count": 100,
    "query_time_ms": 250
  }
}
```

## Running

```bash
cd ../atlan-logs-app
./run.sh
# API at http://localhost:8001
# Docs at http://localhost:8001/docs
```

## Architecture

The API uses PyIceberg to query the Polaris catalog directly:

```
FastAPI → PyIceberg → Polaris Catalog → S3 (Iceberg data files)
```

Key optimizations:
- Predicate pushdown (partition pruning)
- Column pruning (only read needed fields)
- PyArrow for fast in-memory sorting
