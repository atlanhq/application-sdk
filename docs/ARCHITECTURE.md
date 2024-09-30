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


## SDK Structure
The SDK is divided into the following modules:
1. **Application SDK**: This module is used to interact with the Atlan Platform APIs and services. It provides a set of tools and services to build, test and manage applications.
   1. `app` - This module contains the core functionality of the SDK. It provides a consistent way to develop applications on the Atlan Platform.
   2. `common` - This module contains common utilities and functions that are used across the SDK. 
   3. `paas` - This module contains the PaaS system that provides a set of tools and services to interact with the Atlan Platform APIs and services.
   4. `workflows` - This module contains the interfaces that are used to schedule and execute the workflow.
2. **Examples**: This module contains examples of how to use the SDK to build applications on the Atlan Platform.
