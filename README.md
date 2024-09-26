# Phoenix Application SDK

Phoenix Application SDK is a Python library for developing applications on Atlan Platform.
The vision of this SDK is that it will be a single line initializer that can plug into any Python microservice application and provide all the necessary features required to run on Atlan Platform.  


## Table of Contents
1. [Features](#features)
2. [Usage](#usage)
3. [Contributing](#contributing)


## Features
1. Providing a consistent way to develop applications on Atlan Platform
2. Reducing the boilerplate code required to develop applications on Atlan Platform
3. Providing a consistent way to interact with Atlan Platform APIs and services
4. Providing observability and monitoring capabilities out of the box
5. Providing a security layer to ensure that applications are secure by default

## SDK Structure
The SDK is divided into the following modules:
1. `workflows`: Contains the classes and functions required to define and run workflows
2. `fastapi`: Contains routers and functions specific for FastAPI applications
3. 

## Usage
- Install `application-sdk` as a dependency in your project using the following command:
```bash
poetry add git+ssh://git@github.com/atlanhq/application-sdk.git#commit-hash
```
- A sample SQL application built using Phoenix ApplicationSDK can be found [here](https://github.com/atlanhq/phoenix-postgres-app)

## Contributing
- Slack - #collab-phoenix
- [Development and Quickstart Guide](./docs/DEVELOPMENT.md)
