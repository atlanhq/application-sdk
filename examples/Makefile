# Define variables
APP_NAME := application-sdk

# Phony targets
.PHONY: run start-dapr start-temporal-dev start-all

# Run Temporal locally
start-temporal-dev:
	temporal server start-dev --db-filename /tmp/temporal.db

start-dapr:
	dapr run --app-id app --app-port 3000 --dapr-http-port 3500 --dapr-grpc-port 50001 --dapr-http-max-request-size 1024 --resources-path ../components

start-all:
	@echo "Starting all services in detached mode..."
	make start-dapr &
	make start-temporal-dev &
	@echo "Services started. Proceeding..."

stop-all:
	@echo "Stopping all detached processes..."
	@pkill -f "temporal server start-dev" || true
	@pkill -f "dapr run --app-id app" || true
	@echo "All detached processes stopped."
