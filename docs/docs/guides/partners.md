# 🤝 Partner Collaboration Guide – Atlan Integration

## 📚 Table of Contents

- [How do we manage code changes?](#-how-do-we-manage-code-changes)
- [How do we test?](#-how-do-we-test)
- [How do we handle support?](#-how-do-we-handle-support)
- [What about documentation?](#-what-about-documentation)
- [How do we go-to-market?](#-how-do-we-go-to-market)
- [Contact](#-contact)


Welcome! If you're here, you're probably building something awesome. This guide walks you through how we collaborate on app development and integrations — from GitHub access to go-live, support, and everything in between.

## 👩‍💻 How do we manage code changes?
We believe in a transparent, low-friction workflow that keeps you in full control.

Here's how it works:

- Grant our team access to your private GitHub repository by adding our dedicated collaboration account:
   - 📧 Email: connect@atlan.com
   - 🔑 Permission level: Write access
- Once access is granted:
   - All contributions from Atlan are made to a dedicated branch called atlan-main.
   - We never push directly to your main.
   - You can review, test, and merge changes on your own timeline.

> [!NOTE]
> The collaboration account is used solely for code contributions and sync — no changes are made to your main branch.

> Questions about a PR? Drop a comment directly on GitHub or reach out to your Atlan integration contact email.

## 🧪 How do we test?
We make sure everything we contribute works smoothly — both in your world and ours. Here's how testing responsibilities are typically shared:

What you test:
- Fit within your infrastructure and environment
- Business logic and application-specific behavior
- Final regression before merging into your main branch

What Atlan tests:
- Integration with Atlan services and APIs
- End-to-end workflows and UI/UX behavior
- Secure execution


Need help setting up a test environment or writing test cases? Just reach out to your Atlan integration contact — we've got your back.


## 📞 How do we handle support?
Post-deployment, our partner (you!) leads customer-facing support. Here's how we keep it clean:

- You support your application/integration.
- We support the Atlan-side integration and internal tooling.
- If something needs joint triage, we'll jump in immediately via our shared Slack channel or email thread.

> [!TIP]
> We recommend sharing your support SLAs or contact info with us to keep the loop tight.

## 📚 What about documentation?
To ensure customers know how to use your app, please provide:

- A short Overview (What it does, who it's for)
- A Setup Guide (How to install, configure, and connect with Atlan)

Our team will review and edit your provided documentation for clarity and style, and then host it on Atlan's documentation hub.

## 📣 How do we go-to-market?
Once testing is complete and everything looks good:

- We'll move the application to Internal Testing on Atlan.
- Then we promote to Private Preview, where selected customers can try it.
- After incorporating feedback and making necessary adjustments, we can roll out more broadly.
- Atlan will amplify launches via:
    - Product announcements
    - Customer success enablement
    - Feature highlights across our marketing channels

Want co-marketing? Let's plan it together 🎯

## 📬 Contact

- Email: connect@atlan.com
- Issues: [GitHub Issues](https://github.com/atlanhq/application-sdk/issues)
- We’ll set up a shared Slack channel for real-time collaboration



### Ready to get started?
Fill out this intake [form](https://docs.google.com/forms/d/e/1FAIpQLScuAIhCm2GS7YFstrOjawbP8J7PUmOynQo7wI2yGCcCyEcVSw/viewform?usp=sharing&ouid=100133263215396641529) and our team will guide you through the next steps.

Once you're in:

- Explore this repository (SDK) and our [sample applications](https://github.com/atlanhq/atlan-sample-apps) repository for examples.
- Meet with the Atlan team to align on scope, process, and timelines
- Create a private GitHub repo for your application.
- Push your application code to the main branch.
- Grant write access to Atlan's collaboration account: 📧 connect@atlan.com
- Collaborate via the `atlan-main` branch — we'll contribute changes there.
- Review and merge changes into your `main` branch on your schedule.
- Test functionality locally to ensure everything works in your environment.
- Share product documentation, including an overview and setup guide.
- Go live through Atlan's deployment process.
- Collaborate on support — you'll handle user-side issues; we're here for integration help.
- Coordinate a go-to-market plan with the Atlan team to reach customers.

Let's build something great together! 🤝