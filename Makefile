sphinx_docs:
	mkdir -p docs/reference
	sphinx-apidoc -o ./docs/reference ./application_sdk --tocfile index --module-first --separate --force
	cd docs && make html


# Sets up the Dapr and Temporal services in detached mode, required for tests and development
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