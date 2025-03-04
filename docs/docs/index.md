# Python Application SDK

Python Application SDK is a Python library for developing applications on the Atlan Platform and is a PaaS system that provides a set of tools and services to build, test and manage applications.

The SDK also focuses on empowering developers to build applications that are scalable, secure, reliable and easy to maintain.


## Features

The SDK once integrated will automatically generate API routes for the application and provide a set of features that are common across all applications on Atlan Platform.


- Observability and Supportability. The SDK integrated with [OTel](https://opentelemetry.io/) and provides the below features out of the box:
- Application health checks
- PaaS System integration. The SDK integrates with the Atlan PaaS system and provides the below features:
- Workflows. Interfaces to create boilerplate code for workflows that can be scheduled and executed using the Temporal engine.
- SQL Applications. SDK has abstracted out so that building SQL applications is as simple as writing a SQL query. It uses SQLAlchemy for this purpose.

Head over to the [Architecture doc](https://k.atlan.dev/application-sdk/explanations/ARCHITECTURE.html) for detailed information on the features and how they work.

## Usage

A production grade SQL application built using Phoenix Application SDK can be found [here](https://github.com/atlanhq/atlan-postgres-app)

For using the SDK in your project from scratch:

Install `application-sdk` as a dependency in your project using the following command:

```bash
poetry add git+ssh://git@github.com/atlanhq/application-sdk.git#commit-hash
```

- Refer to the [Detailed Usage Guide](https://k.atlan.dev/application-sdk/guides/USAGE.html) to see how to use the SDK and all its features
- Refer to the [Examples](https://github.com/atlanhq/application-sdk/blob/main/examples/README.md) section to see how to use the SDK for various use cases

## Contributing

Want to contribute to the SDK? Here's how you can get started:

- Communication - Use **#collab-phoenix** to ask questions to the team.
- Development - Refer to the [Development and Quickstart Guide](https://k.atlan.dev/application-sdk/guides/DEVELOPMENT.html) on how to add features to the SDK



