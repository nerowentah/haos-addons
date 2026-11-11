# API Key Manager

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

See every **long-lived access token** (API key) of every Home Assistant user in one place, and review each one. Home Assistant normally only shows you your own keys, under your profile. This app lists them all, flags the risky ones, and lets you record who uses each key and why.

## Features

- **Every user's API keys:** description, owner, role, created, last used, last IP and expiry.
- **Automatic flags:**
  - **Never used** or **unused** for a number of days you choose.
  - **Vague description** ("test", "token", "API key 2").
  - **Duplicate description** for the same user.
  - **Deactivated owner:** the key is still stored.
  - **Expired:** the key's expiry date has passed.
  - **Admin rights:** the key can do anything.
- **Review each key:** mark it **Approved** or **Suspicious** and add a note (who uses it and why). Approved keys can be hidden.
- **New-key notifications:** you hear about a key as soon as someone creates it.
- **Login sessions** (browsers and Companion apps) can be listed per user.
- **Read-only.** The app never changes Home Assistant's accounts or tokens. It tells you how to revoke a key.

## Install

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

1. Add the repository above.
2. Install **API Key Manager** and start it.
3. Open **API Keys** from the sidebar. Only administrators see it.

Full documentation is in the app's **Documentation** tab, or [DOCS.md](DOCS.md). See the [changelog](CHANGELOG.md) for release history.
