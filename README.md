# Python Application SDK

Python Application SDK is a Python library for developing applications on Atlan Platform and is a PaaS system that provides a set of tools and services to build, test and manage applications.
The vision of this SDK is that it will be a single line initializer that can plug into any Python microservice application and provide all the necessary features required to run on the Atlan Platform.  


## Table of Contents
1. [Features](#features)
2. [Usage](#usage)
3. [Contributing](#contributing)
4. [Architecture](./docs/ARCHITECTURE.md)


## Features
1. Providing a consistent way to develop applications on Atlan Platform
2. Reducing the boilerplate code required to develop applications on Atlan Platform
3. Providing a consistent way to interact with Atlan Platform APIs and services
4. Providing observability and monitoring capabilities out of the box
5. Providing a security layer to ensure that applications are secure by default


## Usage
- Install `application-sdk` as a dependency in your project using the following command:
```bash
poetry add git+ssh://git@github.com/atlanhq/application-sdk.git#commit-hash
```
- A sample SQL application built using Phoenix ApplicationSDK can be found [here](https://github.com/atlanhq/phoenix-postgres-app)

## Contributing
- Slack - #collab-phoenix
- [Development and Quickstart Guide](./docs/DEVELOPMENT.md)
