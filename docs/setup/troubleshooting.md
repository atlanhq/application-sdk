# Troubleshooting Guide

This guide helps resolve common issues encountered during setup or while using the Application SDK.

## Common Setup Issues

<details>
<summary><b>Permission issues during uv installation</b></summary>

If you encounter permission errors while installing uv (our Python environment and dependency manager), you can typically resolve this by running the installation script with sudo:

```bash
curl -LsSf https://astral.sh/uv/0.7.3/install.sh | sudo sh
```

This grants the necessary privileges for the installation to complete successfully. Remember to only use sudo when necessary and when you understand the implications.

</details>

## Application Usage Issues

<details>
<summary><b>UI elements not loading or displaying incorrectly</b></summary>

Sometimes, browser cache can cause the user interface (UI) to behave unexpectedly, such as elements not loading, displaying old information, or breaking layouts. This is often due to outdated cached versions of assets (like JavaScript or CSS files).

**Solution: Hard Refresh**

A hard refresh forces your browser to re-download all assets for the page, bypassing the cache. Here's how to do it on most common browsers:

- **Windows/Linux:**
    - Chrome, Firefox, Edge: `Ctrl + Shift + R` or `Ctrl + F5`
- **macOS:**
    - Chrome, Firefox, Safari: `Cmd + Shift + R`

If a hard refresh doesn't solve the issue, try clearing your browser's cache and cookies for the specific site, or try accessing the application in an incognito/private browsing window to rule out browser extension interference.

</details>

---

If your issue isn't listed here, please reach out to our support channels or check the project's issue tracker on GitHub.
