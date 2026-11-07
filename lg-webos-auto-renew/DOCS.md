# LG webOS Dev Session Auto Renew

LG webOS Developer Mode sessions expire after about 50 hours. That silently breaks access for `webostv` and for the LG integrations you use through Dev Mode. This app calls the LG Developer API on a schedule, so the session never lapses and you never have to log in again by hand.

## Installation

### From the repository (recommended)

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **LG webOS Dev Session Auto Renew**.
3. Add your TV in the Configuration tab (below), then **Start** the app.

The app ships as a prebuilt multi-arch image, `ghcr.io/nerowentah/lg-webos-auto-renew`.

### As a local app

1. Copy this folder (the one containing `config.yaml`) to `/addons/lg-webos-auto-renew/` on the Home Assistant host. Use the **Samba** share or `scp -r lg-webos-auto-renew root@<ha-ip>:/addons/`.
2. In **Settings → Apps → App store**, open **⋮ → Check for updates**. The app appears under **Local apps**.
3. Install it, configure it, then start it.

## Configuration

```yaml
interval_hours: 48
retries: 3
notify_on_failure: true
sessions:
  - name: "living-room-tv"
    token: "YOUR_DEV_MODE_SESSION_TOKEN"
  - name: "bedroom-tv"
    url: "https://developer.lge.com/secure/ResetDevModeSession.dev?sessionToken=YOUR_OTHER_TOKEN"
    enabled: false
```

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `interval_hours` | int | `48` | Hours between renewals (1-168). Keep this well below the ~50-hour session lifetime. |
| `retries` | int | `3` | Retries per TV, with backoff, before a renewal counts as failed (0-10). |
| `notify_on_failure` | bool | `true` | Notify when a renewal fails after all retries. See [Notifications](#notifications). |
| `sessions` | list | - | One entry per TV (see below). |

Session fields:

| Field | Description |
| --- | --- |
| `name` | Friendly name of the TV. |
| `token` | Dev Mode session token. It is masked in the UI and calls `https://developer.lge.com/secure/ResetDevModeSession.dev?sessionToken=<token>`. |
| `url` | Optional full renewal URL, called exactly as given. Use it instead of `token` if LG changes the endpoint. |
| `enabled` | Optional, default `true`. Set it to `false` to pause scheduled renewals and keep the token. This is the panel's **Auto renew** switch. |

Changes made in the Configuration tab are picked up within about 30 seconds. You don't need to restart the app.

## Getting your session token

1. **Turn on Developer Mode on the TV.**
   1. Install the **Developer Mode** app on the TV.
   2. Open **Settings → General → About this TV** and double-tap the OS version.
   3. Launch the Developer Mode app, sign in with an LG account and switch Developer Mode on.
2. **Turn on the Key Server** in the Developer Mode app.
3. **Save the TV's SSH private key** on your computer.
   - **Option A (no LG tooling):** download the key from the TV's key server on port 9991.
     - Linux/macOS: `curl -o ~/.ssh/tv15_webos http://<tv-ip>:9991/webos_rsa && chmod 600 ~/.ssh/tv15_webos`
     - Windows PowerShell: `Invoke-WebRequest -Uri "http://<tv-ip>:9991/webos_rsa" -OutFile "$env:USERPROFILE\.ssh\tv15_webos"`
   - **Option B (webOS SDK):** `ares-novacom --device <name> --getkey`
   - The key's passphrase is the 6-character, case-sensitive code shown at the bottom-left of the Developer Mode app screen. Run `ssh-keygen -p -f <key>` to remove it.
4. **Read the token** over SSH (port `9922`, user `prisoner`). webOS only offers the legacy `ssh-rsa` host key, which OpenSSH 8.8+ refuses by default, so enable it explicitly:

   ```text
   ssh -p 9922 -i <path-to-ssh-key> `
     -oHostKeyAlgorithms=+ssh-rsa `
     -oPubkeyAcceptedAlgorithms=+ssh-rsa `
     -oStrictHostKeyChecking=accept-new `
     prisoner@<tv-ip> cat /var/luna/preferences/devmode_enabled
   ```

   - If you see `PTY allocation request failed`, add `-T`.
   - In PowerShell, the backticks continue the line. Alternatively, paste the whole command as one line.
5. **Paste the entire printed value** into the session's `token` field. A cut-off or stale value makes LG return `ERR_005, Check user session`.

## Panel

Open **LG Dev Renew** from the sidebar, or click **Open web UI** on the app page. Only administrators see it. The panel follows your Home Assistant theme, including dark mode.

- **TV cards:**
  - Status: *Renewed*, *Failed*, *Renewing…*, *Paused* or *Not run yet*.
  - The last attempt, last success and next renewal.
  - An **Auto renew** switch and a **Renew now** button. **Renew now** also works on a paused TV.
- **Schedule card:** the interval and retries, the **Failure notifications** switch and **Renew all now**.
- **Phones card:** one switch per Companion app linked to this Home Assistant.

Every switch applies immediately. TV and notification switches are saved into the app options, so they also show in the Configuration tab. Phone switches are kept by the app itself. The panel never shows tokens or renewal URLs.

## Notifications

When a renewal fails after all retries, the app notifies every phone that is switched on in the **Phones** card.

- Phones are found automatically from the `notify.mobile_app_*` services, at start and every 5 minutes.
- New phones are on by default. Phones that are no longer linked disappear from the list. You never type phone names.
- With no linked phones, the app creates a persistent notification instead.

## Usage

- On start, the app immediately renews every enabled TV, then repeats on the schedule.
- The **Log** tab shows each renewal by TV name only. It never shows renewal URLs or tokens.

## Troubleshooting

- **Invalid URL:** a session's `url` must start with `http://` or `https://`.
- **Network errors:** make sure the Home Assistant host can reach `developer.lge.com`.
- **LG answers "not success" / `ERR_005`:** the token is stale. Read a fresh one from the TV and update the session.
- **No notifications:**
  - Check that `notify_on_failure` is on and that the phone's switch is on in the **Phones** card.
  - The phone must have the Companion app linked to this Home Assistant.
  - The log line `Notifications go to N phone(s)` lists the phones being used.

## Credits

Inspired by:
- [dyarfaradj/homeassistant-addons](https://github.com/dyarfaradj/homeassistant-addons) and the [community thread](https://community.home-assistant.io/t/lg-dev-session-auto-renewal-addon/939012)
- the REST method from [SR-Lut3t1um/Webos-renew-dev](https://github.com/SR-Lut3t1um/Webos-renew-dev)
- multi-token scheduling from [LucifersCircle/webOS-Token-Refresh](https://github.com/LucifersCircle/webOS-Token-Refresh)

LG and webOS are trademarks of LG Electronics. This project is not affiliated with LG.
