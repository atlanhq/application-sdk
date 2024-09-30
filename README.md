# Python Application SDK

Python Application SDK is a Python library for developing applications on the Atlan Platform and is a PaaS system that provides a set of tools and services to build, test and manage applications.
The vision of this SDK is that it will be a single line initializer that can plug into any Python microservice application and provide all the necessary features required to run it on the Atlan Platform.  


## Table of Contents
1. [Features](#features)
2. [Usage](#usage)
3. [Contributing](#contributing)
4. [Architecture](./docs/ARCHITECTURE.md)
5. [Best Practices](./docs/BEST_PRACTICES.md)


## Features
The SDK once integrated will automatically generate API routes for the application and provide a set of features that are common across all applications on Atlan Platform.
- Observability and Supportability. The SDK integrated with [OTel](https://opentelemetry.io/) and provides the below features out of the box:
  - [Metrics](application_sdk/app/rest/interfaces/metrics.py) at `/telemetry/v1/metrics`
  - [Tracing](application_sdk/app/rest/interfaces/traces.py) at `/telemetry/v1/traces`
  - [Logging](application_sdk/app/rest/interfaces/logs.py) at `/telemetry/v1/logs`
  - UI to view the metrics, traces and logs at `/telemetry/v1/ui`
- Application health checks at `/system/health` and `/system/ready`
- PaaS System integration. The SDK integrates with the PaaS system and provides the below features:
  - Secrets Manager
  - Object Store
  - State Store
  - Workflow Manager
  - Event Store
- Workflows. Interfaces to create boilerplate code for workflows that can be scheduled and executed using the Temporal engine.
  - [SQL Workflow Interface](./application_sdk/workflows/sql/workflow.py) - A single line initializer to create SQL based workflows
  - _BI Workflow Interface(In development)- A single line initializer to create BI based workflows_
  - _Data Quality Workflow Interface(In development) - A single line initializer to create Data Quality based workflows_
  - _Profiling Workflow Interface(In development) - A single line initializer to create Profiling based workflows_
  - _Open Lineage Workflow Interface(In development) - A single line initializer to create Open Lineage based workflows_

**Note**: The SDK is in active development and currently only supports FastAPI applications. Support for other frameworks will be added in the future.

## Usage
- Install `application-sdk` as a dependency in your project using the following command:
```bash
poetry add git+ssh://git@github.com/atlanhq/application-sdk.git#commit-hash
```
- Refer to the [Examples](./examples/README.md) to see how to use the SDK to build applications on the Atlan Platform
- A production grade SQL application built using Phoenix ApplicationSDK can be found [here](https://github.com/atlanhq/phoenix-postgres-app)

## Contributing
- Slack - _#collab-phoenix_
- Refer to the [Development and Quickstart Guide](./docs/DEVELOPMENT.md) on how to add features to the SDK
