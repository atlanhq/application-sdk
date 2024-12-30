# Architecture

The SDK uses 2 tools to power all its features and provide a PaaS interface on the Atlan Platform.
1. [Dapr](https://dapr.io/): Dapr is a portable, event-driven runtime that makes it easy for developers to build resilient, microservice stateless and stateful applications that run on the cloud and edge and embraces the diversity of languages and developer frameworks.
2. [Temporal](https://docs.temporal.io/): Temporal is a microservices orchestration platform that makes it easy to build scalable and reliable applications. Temporal is a cloud-native workflow engine that orchestrates distributed services and jobs in a scalable and fault-tolerant way.

While Dapr is used to abstract the underlying infrastructure and provide a set of building blocks for the application, Temporal is used to schedule and execute the workflow.


## Table of Contents
1. [Hello world on these tools](#hello-world-on-these-tools)
2. [SDK Structure](#sdk-structure)


## Hello world on these tools
1. [Dapr](https://github.com/dapr/quickstarts/blob/master/tutorials/hello-world/README.md)
2. [Temporal](https://learn.temporal.io/getting_started/python/hello_world_in_python/)


## Features
![SDK Features](../docs/images/Phoenix%20-%20SDK%20Featureset.png)

- Observability and Supportability. The SDK integrated with [OTel](https://opentelemetry.io/) and provides the below features out of the box:
  - [Metrics](../application_sdk/app/rest/common/interfaces/metrics.py) at `/telemetry/v1/metrics`
  - [Tracing](../application_sdk/app/rest/common/interfaces/traces.py) at `/telemetry/v1/traces`
  - [Logging](../application_sdk/app/rest/common/interfaces/logs.py) at `/telemetry/v1/logs`
  - UI to view the metrics, traces and logs at `/telemetry/v1/ui`
- Application health checks at `/system/health` and `/system/ready`
- PaaS System integration. The SDK integrates with the PaaS system and provides the below features:
  - Secrets Manager
  - Object Store
  - State Store
  - Workflow Manager
  - Event Store
- Workflows. Interfaces to create boilerplate code for workflows that can be scheduled and executed using the Temporal engine.
  - [SQL Workflow Interface](../application_sdk/workflows/sql/workflows/workflow.py) - A single line initializer to create SQL based workflows
  - _BI Workflow Interface(In development)- A single line initializer to create BI based workflows_
  - _Data Quality Workflow Interface(In development) - A single line initializer to create Data Quality based workflows_
  - _Profiling Workflow Interface(In development) - A single line initializer to create Profiling based workflows_
  - _Open Lineage Workflow Interface(In development) - A single line initializer to create Open Lineage based workflows_
  - It also exposes endpoints for workflow setup
    - `/workflow/v1/preflight` - To check if the workflow can be executed
    - `/workflow/v1/auth` - To authenticate the credentials for the workflow
    - `/workflow/v1/ui` - UI to setup the workflows
- SQL Applications. SDK has abstracted out so that building SQL applications is as simple as writing a SQL query. It uses SQLAlchemy for this purpose.
  - Query Interface to run adhoc SQL queries on `POST /sql/v1/query`

**Note**: The SDK is in active development and currently only supports FastAPI applications. Support for other frameworks will be added in the future.


## SDK Structure
The SDK is divided into the following modules:
1. **Application SDK**: This module is used to interact with the Atlan Platform APIs and services. It provides a set of tools and services to build, test and manage applications.
   1. [`app`](../application_sdk/app/README.md) - This module contains the core functionality of the SDK. It provides a consistent way to develop applications on the Atlan Platform.
   2. [`common`](../application_sdk/common/README.md) - This module contains common utilities and functions that are used across the SDK.
   3. [`paas`](../application_sdk/paas/README.md) - This module contains the PaaS system that provides a set of tools and services to interact with the Atlan Platform APIs and services.
   4. [`workflows`](../application_sdk/workflows/README.md) - This module contains the interfaces that are used to schedule and execute the workflow.
      - [`sql`](../application_sdk/workflows/sql/README.md) - This module contains the SQL workflow interface that is used to schedule and execute SQL based workflows.
      - [`transformers`](../application_sdk/workflows/transformers/README.md) - This module contains the transformers that are used to transform the data in the workflow.
2. **Examples**: This module contains examples of how to use the SDK to build applications on the Atlan Platform.
3. **Components**: This module contains the components that are used by the SDK to interact with the Atlan Platform APIs and services.

