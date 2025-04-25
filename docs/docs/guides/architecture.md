# Architecture

The SDK uses two tools to power all its features and provide a PaaS interface on the Atlan Platform.

1. [Dapr](https://dapr.io/): Dapr is a portable, event-driven runtime that makes it easy for developers to build resilient, microservice stateless and stateful applications that run on the cloud and edge and embraces the diversity of languages and developer frameworks.
2. [Temporal](https://docs.temporal.io/): Temporal is a microservices orchestration platform that makes it easy to build scalable and reliable applications. Temporal is a cloud-native workflow engine that orchestrates distributed services and jobs in a scalable and fault-tolerant way.

While Dapr is used to abstract the underlying infrastructure and provide a set of building blocks for the application, Temporal is used to schedule and execute the workflow.


## Hello world on these tools

1. [Dapr](https://github.com/dapr/quickstarts/blob/master/tutorials/hello-world/README.md)
2. [Temporal](https://learn.temporal.io/getting_started/python/hello_world_in_python/)


## Features

### Module Structure

The application_sdk package is organized into the following modules and submodules:

1. **Core Application Components**:
    - `application/`: Core application functionality
    - `worker.py`: Manages Temporal workflow workers and their execution

2. **Infrastructure and Clients**:
    - `clients/`: Contains client implementations for various services
    - `common/`: Shared utilities and common functionality

3. **Workflow and Activity Management**:
    - `workflows/`: Workflow definitions and implementations
    - `activities/`: Activity definitions and implementations
    - `handlers/`: Event and request handlers

4. **Data Processing and Transformation**:
    - `transformers/`: Data transformation utilities
    - `inputs/`: Input processing and validation
    - `outputs/`: Output formatting and handling

5. **Documentation and Testing**:
    - `docgen/`: Documentation generation tools
    - `test_utils/`: Testing utilities and helpers

### Key Features

The package leverages two main technologies:

1. **Dapr**: Used for infrastructure abstraction and building blocks
2. **Temporal**: Used for workflow orchestration and execution

Key capabilities include:

- Workflow management through Temporal
- Activity execution and coordination
- Client implementations for various services
- Data transformation and processing capabilities
- Documentation generation
- Testing utilities

### Architecture Design

The architecture follows a modular design with clear separation of concerns:

- Core application logic is separated from infrastructure concerns
- Workflow and activity management are isolated
- Data processing is handled through dedicated transformers
- Common utilities are centralized in the common module

This structure enables:

- Easy extension of functionality
- Clear separation of concerns
- Modular testing and maintenance
- Flexible workflow and activity management
- Efficient data processing and transformation

The package is designed for building scalable, reliable applications on the Atlan Platform, leveraging both Dapr for infrastructure and Temporal for workflow orchestration.


