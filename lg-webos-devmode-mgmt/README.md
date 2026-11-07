# LG webOS DevMode Mgmt

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

Keeps your LG webOS **Developer Mode** sessions from expiring. The app renews them through the LG Developer API on a schedule, so `webostv` and your Dev Mode tinkering stay connected without logging in again by hand.

## Features

- **One or more TVs**, each renewed every 1-168 hours, with retries and backoff.
- **Sidebar panel.** A card per TV shows the last and next renewal, with an **Auto renew** switch and a **Renew now** button. The panel follows your HA theme.
- **Switches apply immediately.** Configuration-tab edits are picked up within about 30 seconds, with no restart.
- **Failure notifications** go to the phones linked to your Home Assistant. Phones are found automatically and each has its own switch. With no phones, a persistent notification is used.
- **Private.** Tokens and renewal URLs are never logged or shown.
- **Small.** It uses only the Python standard library.

## Install

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

1. Add the repository above.
2. Install **LG webOS DevMode Mgmt**.
3. Add your TV's session token in the Configuration tab and start the app.

The app's **Documentation** tab, or [DOCS.md](DOCS.md), explains how to read the token from your TV and covers every option. See the [changelog](CHANGELOG.md) for release history.
