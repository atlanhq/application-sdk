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
1. `workflows`: Contains the classes and functions required to define and run workflows
2. `fastapi`: Contains routers and functions specific for FastAPI applications
3. 
