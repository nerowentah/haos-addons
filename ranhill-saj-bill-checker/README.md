# Ranhill SAJ Bill Checker

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

Track the amount due on your **Ranhill SAJ** (Johor) water bill in Home Assistant, for one or more accounts. The app looks each account up on the public Quick Payment page, the same one you'd use to pay without logging in. No password is needed.

## Features

- **Amount due as a sensor.** One RM sensor per account, plus when it last changed, so an automation can tell you when a new bill arrives.
- **Payment due sensor and reminders.** A **Payment due** binary sensor is on while an amount is owed. The app notifies you when a bill arrives, at each weekly check while it stays unpaid, and when it is paid.
- **Weekly checks.** Each account is checked once a week, and **Check now** works any time.
- **Several accounts.** List each account number from your bill. Each gets its own card and device.
- **Sidebar panel.** Account cards, switches that apply immediately, and a **Check now** button. It follows your HA theme.
- **MQTT optional.** With the Mosquitto broker app you get one device per account. Without it, you get plain entities with the same ids. A broker installed later is detected automatically.
- **Phone notifications** when checks keep failing and when they recover. Phones are found automatically, and each has its own switch.
- **Private.** Account numbers are masked in the panel and logs, and the holder's name is reduced to initials.
- **Read-only.** The app never starts a payment.

## Install

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

1. Add the repository above.
2. Install **Ranhill SAJ Bill Checker**.
3. In the Configuration tab, enter each account's 16-character number from your bill, then start the app. If anything is missing, the panel tells you what to fix.

Full documentation is in the app's **Documentation** tab, or [DOCS.md](DOCS.md). See the [changelog](CHANGELOG.md) for release history.

> Ranhill SAJ has no public API. The app reads the Quick Payment page, which may change without notice. Your bill is authoritative.
