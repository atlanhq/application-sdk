# Atlan Application SDK
[![On-Push Checks](https://github.com/atlanhq/application-sdk/actions/workflows/push.yaml/badge.svg)](https://github.com/atlanhq/application-sdk/actions/workflows/push.yaml) [![CodeQL Advanced](https://github.com/atlanhq/application-sdk/actions/workflows/codeql.yaml/badge.svg)](https://github.com/atlanhq/application-sdk/actions/workflows/codeql.yaml) [![PyPI version](https://img.shields.io/pypi/v/atlan-application-sdk.svg)](https://pypi.org/project/atlan-application-sdk/)

The Atlan Application SDK is a Python library designed for building applications on the Atlan platform. It offers a full PaaS (Platform-as-a-Service) toolkit — from local development to deployment and partner collaboration — so you can create integrations and tools that seamlessly extend the Atlan experience for our mutual customers.


## Usage

Install `atlan-application-sdk` as a dependency in your project:

- Using pip:
```bash
# pip install the latest version from PyPI
pip install atlan-application-sdk
```

- Using alternative package managers:
```bash
# Using uv to install the latest version from PyPI
uv add atlan-application-sdk

# OR using Poetry to install the latest version from PyPI
poetry add atlan-application-sdk
```

> [!TIP]
> **View sample apps built using Application SDK [here](https://github.com/atlanhq/atlan-sample-apps)**

## Getting Started

- Want to develop locally or run examples from this repository? Check out our [Getting Started Guide](docs/docs/guides/getting-started.md) for a step-by-step walkthrough!
- Detailed documentation for the application-sdk is available at [docs](https://github.com/atlanhq/application-sdk/blob/main/docs/docs/) folder.

## Contributing

- We welcome contributions! Please see our [Contributing Guide](https://github.com/atlanhq/application-sdk/blob/main/CONTRIBUTING.md) for guidelines.


## 🤝 Partner Collaboration Guide – Atlan Integration

Welcome! If you're here, you're probably building something awesome. This guide walks you through how we collaborate on app development and integrations — from GitHub access to go-live, support, and everything in between.

### 👩‍💻 How do we manage code changes?
We believe in a transparent, low-friction workflow that keeps you in full control.

Here’s how it works:

You grant our team access to your private GitHub repository by adding our dedicated collaboration account:
📧 Email: connect@atlan.com
🔑 Permission level: Write access

🛡️ This account is used solely for code contributions and sync — no changes are made to your main branch.

Once access is granted:

- All contributions from Atlan are made to a dedicated branch called atlan-main.
- We never push directly to your main.
- You can review, test, and merge changes on your own timeline.

Questions about a PR? Drop a comment directly on GitHub or reach out to your Atlan integration contact via Slack or email.

We’re here to make collaboration smooth, secure, and efficient.


### 🧪 How do we test?
We make sure everything we contribute works smoothly — both in your world and ours. Here’s how testing responsibilities are typically shared:

✅ What Atlan tests:
- Integration with Atlan services and APIs
- End-to-end workflows and UI/UX behavior
- Secure execution via Argo workflows

✅ What you test:
- Fit within your infrastructure and environment
- Business logic and application-specific behavior
- Final regression before merging into your main branch

Need help setting up a test environment or writing test cases? Just reach out to your Atlan integration contact — we’ve got your back.


### 📞 How do we handle support?
Post-deployment, our partner (you!) takes point on customer-facing support. Here's how we keep it clean:

- You support your application/integration.
- We support the Atlan-side integration and internal tooling.
- If something needs joint triage, we'll jump in immediately via our shared Slack channel or email thread.

💡 We recommend sharing your support SLAs or contact info with us to keep the loop tight.


### 📚 What about documentation?
To ensure customers know how to use your app, please provide:

- A short Overview (What it does, who it's for)
- A Setup Guide (How to install, configure, and connect with Atlan)

Don’t worry — our team will review and edit for clarity and style, and host this on Atlan’s documentation hub.


### 📣 How do we go-to-market?
Once testing is complete and everything looks good:

- We’ll move the application to Internal Testing on Atlan.
- Then we promote to Private Preview, where selected customers can try it.
- After feedback and tweaks, we can roll out more broadly.
- Atlan will amplify launches via:
    - Product announcements
    - Customer success enablement
    - Feature highlights across our marketing channels

Want co-marketing? Let’s plan it together 🎯


### 📬 Ready to get started?
Fill out this intake [form](https://docs.google.com/forms/d/e/1FAIpQLScuAIhCm2GS7YFstrOjawbP8J7PUmOynQo7wI2yGCcCyEcVSw/viewform?usp=sharing&ouid=100133263215396641529) and we’ll take it from there.

Once you’re in:

- Explore this SDK repository and the [sample-applications](https://github.com/atlanhq/atlan-sample-apps) repository
- Meet with the Atlan team to align on scope, process, and timelines
- Create a private GitHub repo for your application.
- Push your application code to the main branch.
- Grant write access to Atlan’s collaboration account:
📧 connect@atlan.com
- Collaborate via the atlan-main branch — we’ll contribute changes there.
- Review and merge changes into your main branch on your schedule.
- Test functionality locally to ensure everything works in your environment.
- Share product documentation, including an overview and setup guide.
- Go live through Atlan’s deployment process.
- Collaborate on support — you’ll handle user-side issues; we’re here for integration help.
- Coordinate a go-to-market plan with the Atlan team to reach customers.

Let’s build something great together.


### 🆘 How do I get help?
We’re here whenever you need us:

- Email: **connect@atlan.com**
- Issues: [GitHub Issues](https://github.com/atlanhq/application-sdk/issues)
- We’ll set up a shared Slack channel for real-time collaboration

Ready to build something great? Let’s go 💪


## Security

Have you discovered a vulnerability or have concerns about the SDK? Please read our [SECURITY.md](https://github.com/atlanhq/application-sdk/blob/main/SECURITY.md) document for guidance on responsible disclosure, or please e-mail security@atlan.com and we will respond promptly.

## License and Attribution

- This project is licensed under the Apache License 2.0 - see the [LICENSE](https://github.com/atlanhq/application-sdk/blob/main/LICENSE) file for details.
- This project includes dependencies with various open-source licenses. See the [NOTICE](https://github.com/atlanhq/application-sdk/blob/main/NOTICE) file for third-party attributions.
