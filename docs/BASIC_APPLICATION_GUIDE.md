# Basic Application Builder

The SDK currently only supports FastAPI framework but in the future we will be adding support for other frameworks as well.

## Table of contents
- [Simple Integration](#simple-integration)
- [OTel Integration](#otel-integration)


## Simple Integration
Simplest way to integrate the Application builder to your micro-service is by adding the following lines in your application
```python
import logging
import uvicorn
from application_sdk.app.rest import FastAPIApplicationBuilder
from fastapi import FastAPI
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


app = FastAPI(title="My App")


if __name__ == "__main__":
    atlan_app_builder = FastAPIApplicationBuilder(app)
    atlan_app_builder.add_telemetry_routes() # Adds the OTel consumption API routes

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
    )
```
This will start a FastAPI application with the following features at 0.0.0.0:8000:
1. `/health` endpoint - This endpoint will return the system health of the application
2. `/metrics` endpoint - This endpoint will return the metrics of the application and consume all OTel metrics
3. `/logs` endpoint - This endpoint will return the logs of the application and consume all OTel logs
4. `/traces` endpoint - This endpoint will return the traces of the application and consume all OTel traces

- You can check the swagger docs at `http://0.0.0.0:8000/docs`
- You can skip the telemetry routes by not calling `add_telemetry_routes()` method.

## OTel Integration
The SDK comes with a built-in OTel integration. You can enable it by setting the below environment variables
```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:8000/telemetry";
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf";
export OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED="true";
export OTEL_PYTHON_EXCLUDED_URLS="/telemetry/.*,/system/.*"
```
and running the application with the OTel agent