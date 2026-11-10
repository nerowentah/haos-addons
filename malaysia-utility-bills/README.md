# Malaysia Utility Bills

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

Your Malaysian utility bills in Home Assistant, in one app: **TNB** electricity and **Ranhill SAJ** (Johor) water, with TM Unifi to follow.

## Features

- **TNB:** imports hourly myTNB smart meter usage and cost into the **Energy dashboard**, reads the amount due, and estimates the running bill live from a local energy sensor (2025 tariff, ToU, AFA, EEI, SST, KWTBB, NEM).
- **Ranhill SAJ:** the amount due on each water account, from the public Quick Payment page. No password is needed.
- **Several accounts per provider**, each with its own card and device.
- **One sidebar panel** for everything: account cards, switches that apply immediately, **Sync now** and **Check now** buttons. It follows your HA theme.
- **MQTT optional.** With the Mosquitto broker app you get one device per account. Without it, you get plain entities with the same ids.
- **Phone notifications** for failures, unpaid bills and a daily TNB usage report. Phones are found automatically.
- **Replaces TNB Usage Checker and Ranhill SAJ Bill Checker** and keeps their entity ids, history and Energy dashboard statistics. The panel imports their accounts for you.

## Install

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

1. Add the repository above.
2. Install **Malaysia Utility Bills**.
3. Add your accounts under `tnb_accounts` and/or `saj_accounts` in the Configuration tab, or start the app and press **Import** in its panel. Then stop the old apps.

Full documentation is in the app's **Documentation** tab, or [DOCS.md](DOCS.md). See the [changelog](CHANGELOG.md) for release history.

> Neither myTNB nor Ranhill SAJ has a public API. Their websites may change without notice. Bill estimates are indicative only; your bills are authoritative.
