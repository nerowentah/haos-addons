# LG webOS Dev Session Auto Renew

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

A Home Assistant add-on that keeps your LG webOS Developer Mode session from expiring.

LG Developer Mode sessions (used by `webostv` and by the LG ThinQ integrations when tinkering via Dev Mode) expire periodically, which silently breaks device access. This add-on automatically calls the LG Developer API on a schedule so your session never lapses and your devices stay connected without manual re-login.

Inspired by:

- <https://github.com/dyarfaradj/homeassistant-addons> and the [community add-on thread](https://community.home-assistant.io/t/lg-dev-session-auto-renewal-addon/939012)
- the simpler REST method recommended by <https://github.com/SR-Lut3t1um/Webos-renew-dev>
- the multi-token scheduling concept from <https://github.com/LucifersCircle/webOS-Token-Refresh>

## Features

- Automatically renews one or more LG webOS Developer Mode sessions
- Configurable renewal interval (1-168 hours) with retry + backoff
- Optional Home Assistant persistent notification when a renewal exhausts its retries
- No external Python dependencies (stdlib only), no web UI, runs fully on-device
- Tokens are never printed to the logs (URLs are shown with the token masked)

## Installation

The add-on is distributed from the public [repository](https://github.com/nerowentah/haos-addons) as a prebuilt multi-architecture image (`ghcr.io/nerowentah/lg-webos-auto-renew`) on GitHub Container Registry.

### Option A — Add-on Store repository URL (recommended)

1. In the frontend go to **Settings** -> **Add-ons** -> **Add-on Store**, open the **...** menu (top right), and select **Repositories**.
2. Paste the repository URL:
   ```
   https://github.com/nerowentah/haos-addons
   ```
3. Click **Add**, then the new repository appears under **Home Assistant Add-ons**. Reload the page if needed, **Install** the `LG webOS Dev Session Auto Renew` add-on, configure your session (below), then **Start**.

### Option B — Local add-on

1. Get this folder (`lg-webos-auto-renew/`, the one containing `config.yaml`) onto the Home Assistant host under `/addons`:
   - **Samba**: share the `addons` share, then copy it to `/addons/lg-webos-auto-renew/`, or
   - **SSH / Advanced SSH & Web Terminal**: `scp -r lg-webos-auto-renew root@<ha-ip>:/addons/`
2. In the frontend go to **Settings** -> **Add-ons** -> **Add-on Store**, open the **...** menu (top right), and select **Repositories**.
3. Click **Add local repository** and pick `/addons/lg-webos-auto-renew`, or simply reload the add-on store — the add-on appears under **Local add-ons**.
4. Click **Install**, configure your session (below), then **Start**.

## Configuration

Example configuration:

```yaml
interval_hours: 48
retries: 3
notify_on_failure: true
sessions:
  - name: "living-room-tv"
    token: "YOUR_DEV_MODE_SESSION_TOKEN"
```

### Options

| Option              | Type | Default | Description                                                                        |
| ------------------- | ---- | ------- | ---------------------------------------------------------------------------------- |
| `interval_hours`    | int  | `48`    | Hours between renewal attempts (1-168).                                            |
| `retries`           | int  | `3`     | Retries per session before marking the renewal failed (0-10).                      |
| `notify_on_failure` | bool | `true`  | Send a Home Assistant persistent notification on final failure.                    |
| `sessions`          | list | -       | One or more TVs. Each session needs a `name` and either a `token` or a full `url`. |

```yaml
sessions:
  - name: "living-room-tv"
    token: "YOUR_DEV_MODE_SESSION_TOKEN"
  - name: "bedroom-tv"
    url: "https://developer.lge.com/secure/ResetDevModeSession.dev?sessionToken=YOUR_OTHER_TOKEN"
```

- A session with `token` uses the endpoint `https://developer.lge.com/secure/ResetDevModeSession.dev?sessionToken=<token>`.
- A session with `url` is called exactly as given (useful if LG changes the endpoint).

### Getting your session token

1. Enable **Developer Mode** on your LG webOS TV (usually in `Settings -> General -> About this TV`, double-tap the OS version, then turn on Developer Mode in the LG Developer app).
2. Register the TV, then read the dev-mode enable key from the host:
   `ssh prisoner@<tv-ip> -p 9922 "cat /var/luna/preferences/devmode_enabled"`
   (the key is also shown in the LG Developer Mode app on the TV).
3. Paste that value into the `token` field for the TV.

## Usage

On start the add-on immediately attempts a renewal for every configured session, then re-runs on the schedule. Renewals and failures are visible in the add-on **Log** tab; each attempt logs the destination URL with the token masked (`sessionToken=***`).

## Troubleshooting

- **Invalid URL error**: session `url` must start with `http://` or `https://`.
- **Repeated network errors**: confirm the HA host can reach `developer.lge.com`.
- **Growth in failures / API "not success"**: the token itself expired; fetch a fresh key from the TV Dev Mode app and update the session.
- **Notifications not arriving**: `notify_on_failure` must be `true` and the add-on needs the internal Home Assistant API (enabled by default here via `homeassistant_api`).

## License / Attribution

Original implementation written for this repository; LG and webOS are trademarks of LG Electronics. The referenced projects ship under their own licenses.

See the [CHANGELOG](CHANGELOG.md) for version history.
