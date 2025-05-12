# Atlan Application SDK
[![On-Push Checks](https://github.com/atlanhq/application-sdk/actions/workflows/push.yaml/badge.svg)](https://github.com/atlanhq/application-sdk/actions/workflows/push.yaml) [![CodeQL Advanced](https://github.com/atlanhq/application-sdk/actions/workflows/codeql.yaml/badge.svg)](https://github.com/atlanhq/application-sdk/actions/workflows/codeql.yaml) [![PyPI version](https://img.shields.io/pypi/v/atlan-application-sdk.svg)](https://pypi.org/project/atlan-application-sdk/)

Application SDK is a Python library for developing applications on the Atlan Platform. It provides a comprehensive PaaS (Platform as a Service) system with tools and services to build, test, and manage applications.

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

## Documentation

- Detailed documentation for the application-sdk is available at [k.atlan.dev/application-sdk/main](https://k.atlan.dev/application-sdk/main).
- If you are not able to access the URL, you can check the docs in the [docs](./docs) folder.

## Usage

### Example Applications

- View a production-grade SQL application built using application-sdk [here](https://github.com/atlanhq/atlan-postgres-app)
- View sample apps built using application-sdk [here](https://github.com/atlanhq/atlan-sample-apps)

### Installation

Install `atlan-application-sdk` as a dependency in your project:

- Using pip:
```bash
# pip install the latest version from PyPI
pip install atlan-application-sdk
```

- Using alternative package managers:
```bash
# Using uv to install the latest version from PyPI
uv pip install atlan-application-sdk

# using Poetry to install the latest version from PyPI
poetry add atlan-application-sdk
```

## Contributing

We welcome contributions! Please see our [Contributing Guide](./CONTRIBUTING.md) for guidelines.

## Need Help?

Get support through any of these channels:

- Email: **connect@atlan.com**
- Slack: **#pod-app-framework**
- Issues: [GitHub Issues](https://github.com/atlanhq/application-sdk/issues)

## Security

Have you discovered a vulnerability or have concerns about the SDK? Please read our [SECURITY.md](./SECURITY.md) document for guidance on responsible disclosure, or Please e-mail security@atlan.com and we will respond promptly.


## License and Attribution

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

This project includes dependencies with various open-source licenses. See the [NOTICE](NOTICE) file for third-party attributions.
