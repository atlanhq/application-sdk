
# Define variables
APP_NAME := atlan-application-sdk

# Phony targets
.PHONY: run start-dapr start-temporal-dev start-all

# Run Temporal locally
start-temporal-dev:
	temporal server start-dev --db-filename /tmp/temporal.db

# Run Dapr locally
start-dapr:
	dapr run --enable-api-logging --log-level debug --app-id app --app-port 3000 --dapr-http-port 3500 --dapr-grpc-port 50001 --dapr-http-max-request-size 1024 --resources-path components

install:
	# Install the dependencies with extras
	uv sync --all-extras

	# Activate the virtual environment and install pre-commit hooks
	uv run pre-commit install

# Start all services in detached mode
start-all:
	@echo "Starting all services in detached mode..."
	make start-dapr &
	make start-temporal-dev &
	@echo "Services started. Proceeding..."

# Stop all services
stop-all:
	@echo "Stopping all detached processes..."
	@pkill -f "temporal server start-dev" || true
	@pkill -f "dapr run --app-id app" || true
	@echo "All detached processes stopped."


# Generate documentation
apidocs:
	cd docs && mkdocs build
	cd docs && pydoctor --html-output=site/api ../application_sdk --docformat=google