# Application SDK

Application SDK is a Python library for developing applications on the Atlan Platform. It provides a comprehensive PaaS (Platform as a Service) system with tools and services to build, test, and manage applications.

The SDK empowers developers to build applications that are:

- Scalable
- Secure
- Reliable
- Easy to maintain

## Getting Started

To begin developing with the Application SDK:

1. Clone the repository
2. Follow the setup instructions for your platform:
   - [Windows](./docs/docs/setup/WINDOWS.md)
   - [Mac](./docs/docs/setup/MAC.md)
   - [Linux](./docs/docs/setup/LINUX.md)
3. Run the example application:
   - [Hello World](./examples/application_hello_world.py)
   - [SQL](./examples/application_sql.py)
4. (Optionally) clone an existing application -- [sql](https://github.com/atlanhq/atlan-postgres-app) example, [hello-world](https://github.com/atlanhq/atlan-hello-world-app) example.

## Documentation

- Detailed documentation for the application-sdk is available at [k.atlan.dev/application-sdk/main](https://k.atlan.dev/application-sdk/main).
- If you are not able to access the URL, you can check the docs in the [docs](./docs) folder.

## Usage

### Example Applications

- View a production-grade SQL application built using application-sdk [here](https://github.com/atlanhq/atlan-postgres-app)
- View a hello-world application built using application-sdk [here](https://github.com/atlanhq/atlan-hello-world-app)

### Installation

Install `application-sdk` as a dependency in your project:

```bash
poetry add git+ssh://git@github.com/atlanhq/application-sdk.git#commit-hash
```

## Contributing

We welcome contributions! Please see our [Contributing Guide](./.github/CONTRIBUTING.md) for guidelines.

## Need Help?

Get support through any of these channels:

- Email: **apps@atlan.com**
- Slack: **#pod-app-framework**
- Issues: [GitHub Issues](https://github.com/atlanhq/application-sdk/issues)

