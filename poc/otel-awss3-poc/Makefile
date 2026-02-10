# OTEL AWSS3 POC Makefile
# ========================
#
# Convenience commands for running the POC

.PHONY: help up down logs test test-s3 test-otlp test-sse test-iceberg \
        s3-ls s3-count clean rebuild status workers-only

# Default target
help:
	@echo "OTEL AWSS3 POC - Workflow Logs Observability"
	@echo "============================================="
	@echo ""
	@echo "Setup Commands:"
	@echo "  make up          - Start all services"
	@echo "  make up-d        - Start all services (detached)"
	@echo "  make down        - Stop all services"
	@echo "  make rebuild     - Rebuild and restart all services"
	@echo "  make clean       - Stop services and remove volumes"
	@echo ""
	@echo "Monitoring Commands:"
	@echo "  make status      - Show status of all services"
	@echo "  make logs        - Follow logs from all services"
	@echo "  make logs-app    - Follow observability-app logs"
	@echo "  make logs-col    - Follow collector logs"
	@echo "  make logs-workers- Follow all worker logs"
	@echo ""
	@echo "S3/MinIO Commands:"
	@echo "  make s3-ls       - List files in workflow-logs bucket"
	@echo "  make s3-count    - Count files in workflow-logs bucket"
	@echo "  make s3-sample   - Show sample file content"
	@echo ""
	@echo "Test Commands:"
	@echo "  make test        - Run all tests"
	@echo "  make test-s3     - Test S3 structure"
	@echo "  make test-otlp   - Test OTLP JSON format"
	@echo "  make test-sse    - Test live streaming (SSE)"
	@echo "  make test-iceberg- Test Iceberg queries"
	@echo ""
	@echo "Ingestion Commands:"
	@echo "  make ingest      - Run ingestion (S3 -> Iceberg)"
	@echo ""
	@echo "Worker Commands:"
	@echo "  make workers-only- Start only workers (assumes collector running)"
	@echo "  make workers-stop- Stop workers"
	@echo ""

# ===========================================
# Setup Commands
# ===========================================

up:
	docker-compose up --build

up-d:
	docker-compose up --build -d

down:
	docker-compose down

rebuild:
	docker-compose down
	docker-compose up --build

clean:
	docker-compose down -v
	docker system prune -f

# ===========================================
# Monitoring Commands
# ===========================================

status:
	docker-compose ps

logs:
	docker-compose logs -f

logs-app:
	docker-compose logs -f observability-app

logs-col:
	docker-compose logs -f collector

logs-workers:
	docker-compose logs -f worker-snowflake worker-mssql worker-postgres

# ===========================================
# S3/MinIO Commands
# ===========================================

s3-ls:
	@docker-compose exec -T minio mc alias set myminio http://localhost:9000 minioadmin minioadmin 2>/dev/null || true
	docker-compose exec minio mc ls myminio/workflow-logs --recursive

s3-count:
	@echo "Counting files in workflow-logs bucket..."
	@docker-compose exec minio mc ls myminio/workflow-logs --recursive | wc -l

s3-sample:
	@echo "Showing sample file content..."
	@docker-compose exec minio sh -c 'mc ls myminio/workflow-logs --recursive | head -1 | awk "{print \$$NF}" | xargs -I {} mc cat myminio/workflow-logs/{} | head -100'

# ===========================================
# Test Commands
# ===========================================

test:
	@echo "Installing test dependencies..."
	pip install -q -r tests/requirements.txt
	@echo ""
	python tests/run_all_tests.py

test-s3:
	@pip install -q -r tests/requirements.txt
	python tests/test_s3_structure.py

test-otlp:
	@pip install -q -r tests/requirements.txt
	python tests/test_otlp_json_format.py

test-sse:
	@pip install -q -r tests/requirements.txt
	python tests/test_live_streaming.py

test-iceberg:
	@pip install -q -r tests/requirements.txt
	python tests/test_iceberg_query.py

# ===========================================
# Ingestion Commands
# ===========================================

ingest:
	docker-compose --profile ingestion up ingestion

# ===========================================
# Worker Commands
# ===========================================

workers-only:
	docker-compose up -d worker-snowflake worker-mssql worker-postgres

workers-stop:
	docker-compose stop worker-snowflake worker-mssql worker-postgres

# ===========================================
# Development Commands
# ===========================================

# Open MinIO console in browser
minio-ui:
	@echo "Opening MinIO console at http://localhost:9001"
	@echo "Login: minioadmin / minioadmin"
	@open http://localhost:9001 2>/dev/null || xdg-open http://localhost:9001 2>/dev/null || echo "Please open http://localhost:9001 in your browser"

# Test OTLP endpoint manually
test-otlp-endpoint:
	@echo "Testing OTLP HTTP endpoint..."
	curl -X POST http://localhost:4318/v1/logs \
		-H "Content-Type: application/json" \
		-d '{"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"test-service"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"'$$(date +%s)000000000'","severityText":"INFO","body":{"stringValue":"Test log message"},"attributes":[{"key":"workflow_id","value":{"stringValue":"test-workflow-001"}}]}]}]}]}'

# Test observability app health
test-app-health:
	@echo "Testing observability app health..."
	curl -s http://localhost:8081/health | python -m json.tool

# Test SSE endpoint (5 second timeout)
test-app-sse:
	@echo "Testing SSE endpoint for 5 seconds..."
	timeout 5 curl -N http://localhost:8081/sse/logs?workflow_id=test || true

# List active workflows
list-workflows:
	@curl -s http://localhost:8081/api/workflows | python -m json.tool
