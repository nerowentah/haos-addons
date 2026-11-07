# Mobile Device Manager

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

Keep the phones and tablets registered through the Home Assistant **Companion app** tidy. The app lists every device and shows which automations, scripts and notify calls still use it. It flags devices that are stale or duplicated, and only removes a device once nothing depends on it.

## Features

- **Inventory** of every Companion app device: last seen, battery, OS, notify service and registration state.
- **Linked items:** every automation, script and scene that uses the device or its sensors, plus every automation and script that sends notifications to it. Each one has an **Edit** button that opens it in Home Assistant.
- **Flags:**
  - **Stale:** not seen for a number of days you choose.
  - **Duplicate:** an older registration of the same phone.
  - **Not loaded:** the registration is broken.
- **Safe removal.** **Remove** only works when nothing uses the device. The app re-checks right before removing and asks you to confirm. It never force-removes.
- **Attention notifications** when devices turn stale or scanning keeps failing. Each phone has its own switch.

## Install

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

1. Add the repository above.
2. Install **Mobile Device Manager** and start it.
3. Open **Mobile Devices** from the sidebar.

Full documentation is in the app's **Documentation** tab, or [DOCS.md](DOCS.md). See the [changelog](CHANGELOG.md) for release history.
