# Dapr Application Components

This repository contains Dapr components for various services.

## Requirements
- Install [Dapr CLI](https://docs.dapr.io/getting-started/install-dapr-cli/)

## Components
1. State store - Uses SQLite as the state store at `/tmp/dapr/statestore.db`
2. Object store - Uses local file system at `/tmp/dapr/objectstore`
3. Secret store - Uses system environment variables
4. Event store - Uses in-memory event store

## Running examples
1. Create virtual environment using `virtualenv .venv`
2. Activate the virtual environment using `source .venv/bin/activate`
3. Install the dependencies using `poetry install`
4. cd into the example directory `cd examples`
5. Make sure you start the Dapr runtime before running the examples `dapr run --app-id app --app-port 3000 --dapr-http-port 3500 --dapr-grpc-port 50001 --dapr-http-max-request-size 1024 --resources-path ./components`
6. Start the temporal server `temporal server start-dev`
7. You can now run any of the examples using `python <example>.py`
