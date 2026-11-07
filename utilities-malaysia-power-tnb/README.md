# Power TNB

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

Track Malaysian **TNB** electricity for one or more accounts. Power TNB imports hourly myTNB smart meter usage and cost into the **Energy dashboard**. It also estimates your running bill live from a local energy sensor, using the 2025 TNB domestic tariff.

## Features

- **Multiple accounts.** Each account uses a myTNB login, a local energy sensor, or both.
- **Energy dashboard statistics.** Hourly kWh and RM from myTNB. Each sync re-imports 30 days, so late TNB data is filled in.
- **Live bill estimate** covering General and Time-of-Use, AFA, EEI, KWTBB, SST and NEM. It includes a full breakdown and a prediction for the end of the cycle.
- **Sidebar panel.** Account cards, switches that apply immediately, and a **Sync myTNB now** button. It follows your HA theme.
- **MQTT optional.** With the Mosquitto broker app you get one device per account. Without it, you get plain entities with the same ids. A broker installed later is detected automatically.
- **Phone notifications** when a sync fails or recovers. Phones are found automatically, and each has its own switch.
- **Private.** Passwords, tokens and smart meter URLs are never logged.

## Install

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

1. Add the repository above.
2. Install **Power TNB**.
3. Add your accounts in the Configuration tab and start the app.

Full documentation is in the app's **Documentation** tab, or [DOCS.md](DOCS.md). See the [changelog](CHANGELOG.md) for release history.

> myTNB has no public API. The portal flow is unofficial and may break if TNB changes its website. Bill estimates are indicative only.
