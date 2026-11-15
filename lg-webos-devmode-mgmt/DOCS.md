# LG webOS DevMode Mgmt

LG webOS Developer Mode sessions expire: after up to 1000 hours on current TVs, about 50 hours on older firmware. That silently breaks access for `webostv` and for the LG integrations you use through Dev Mode. This app calls the LG Developer API on a schedule, so the session never lapses. It also checks with LG that the session is still alive, and tells you when the TV needs a new sign-in.

## Installation

### From the repository (recommended)

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **LG webOS DevMode Mgmt**.
3. Add your TV in the Configuration tab (below), then **Start** the app.

The app ships as a prebuilt multi-arch image, `ghcr.io/nerowentah/lg-webos-auto-renew`.

### As a local app

1. Copy this folder (the one containing `config.yaml`) to `/addons/lg-webos-devmode-mgmt/` on the Home Assistant host. Use the **Samba** share or `scp -r lg-webos-devmode-mgmt root@<ha-ip>:/addons/`.
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
| `interval_hours` | int | `48` | Hours between renewals: 48 or more, with no upper limit. A lower value is treated as 48. The default suits every TV: sessions last up to 1000 hours on current TVs and about 50 on older firmware. |
| `maintenance_windows` | list | `daily 00:00-06:00` | When LG's developer site may be down for maintenance (Home Assistant's timezone), one per line: `daily 00:00-06:00`, `sun 01:00-05:00`, `mon-fri 23:00-02:00` (crosses midnight), `weekends 02:00-04:00` or `mon,wed 00:00-24:00`. No renewal is attempted inside a window; a renewal due then waits until it ends. An empty list allows renewing at any time. |
| `retries` | int | `3` | Retries per TV, with backoff, before a renewal counts as failed (0-10). |
| `notify_on_failure` | bool | `true` | Notify when a renewal fails after all retries. See [Notifications](#notifications). |
| `sessions` | list | - | One entry per TV (see below). |
| `log_level` | string | `info` | How much is logged: `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. Use `debug` while troubleshooting. Applies without a restart. |

Session fields:

| Field | Description |
| --- | --- |
| `name` | Friendly name of the TV. |
| `token` | Dev Mode session token. It is masked in the UI and calls `https://developer.lge.com/secure/ResetDevModeSession.dev?sessionToken=<token>`. |
| `url` | Optional full renewal URL, called exactly as given. Use it instead of `token` if LG changes the endpoint. |
| `enabled` | Optional, default `true`. Set it to `false` to pause scheduled renewals and keep the token. This is the panel's **Auto renew** switch. |
| `host` | Optional. The TV's IP address. Set it to let the app read the token from the TV itself. See [Reading the token from the TV automatically](#reading-the-token-from-the-tv-automatically). Masked. |
| `port` | Optional. The TV's SSH port, default `9922`. |
| `passphrase` | **Required when `host` is set.** The 6-character, case-sensitive code shown in the TV's Developer Mode app. Masked. |
| `private_key` | Optional. The TV's SSH private key as text, instead of uploading it in the panel or using Key Server. Masked. |
| `media_player` | Optional. The TV's entity from the LG webOS TV integration, for example `media_player.living_room_tv`. The token is read each time it turns on. |

Changes made in the Configuration tab are picked up within about 30 seconds. You don't need to restart the app.

## Getting your session token

1. **Turn on Developer Mode on the TV.**
   1. Install the **Developer Mode** app on the TV.
   2. Open **Settings → General → About this TV** and double-tap the OS version.
   3. Launch the Developer Mode app, sign in with the email-based ID and password of your LG Developer account, and switch Developer Mode on.
2. **Turn on the Key Server** in the Developer Mode app.
3. **Save the TV's SSH private key** on your computer.
   - **Option A (no LG tooling):** download the key from the TV's key server on port 9991.
     - Linux/macOS: `curl -o ~/.ssh/lg_tv_webos http://<tv-ip>:9991/webos_rsa && chmod 600 ~/.ssh/lg_tv_webos`
     - Windows PowerShell: `Invoke-WebRequest -Uri "http://<tv-ip>:9991/webos_rsa" -OutFile "$env:USERPROFILE\.ssh\lg_tv_webos"`
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

## After a Developer Mode app update or a new sign-in

The token belongs to one sign-in. When the Developer Mode app on the TV asks for your LG account email and password again (for example after the app updates itself, or after Developer Mode was switched off), the TV gets a **new token** and the saved one stops matching.

- **What the app shows:** when LG no longer accepts the saved token, the TV's card shows **Sign in on TV** and you get one notification saying so. The app keeps trying on the schedule, without repeating the alert.
- **What to do:**
  1. On the TV, open the Developer Mode app, sign in and switch Dev Mode on.
  2. Read the token again (step 4 above).
  3. Save it in the session's `token` field. The next renewal clears the warning; **Renew now** does it at once.
- **Check after every sign-in on the TV, even without a warning.** LG can keep accepting the old token for a while, so the card may still say *Renewed* while the TV counts down on a different token. Compare the value from step 4 with the one you saved.

## Reading the token from the TV automatically

Instead of reading the token on a PC, the app can read it from the TV itself. It does what `ares-novacom` does: an SSH login to the TV (user `prisoner`) that prints `/var/luna/preferences/devmode_enabled`.

```yaml
sessions:
  - name: "living-room-tv"
    token: "YOUR_DEV_MODE_SESSION_TOKEN"
    host: "192.0.2.10"
    port: 9922
    passphrase: "ABC123"
    media_player: media_player.living_room_tv
```

**Set-up**

1. Give the TV a fixed IP address and enter it as `host`.
2. Enter the `passphrase`: the code at the bottom-left of the Developer Mode app.
3. Give the app the TV's SSH key, in one of three ways:
   - **Upload it (recommended).** Open the **LG DevMode** panel and press **Upload SSH key** on the TV's card. Choose the private key file you saved in [Getting your session token](#getting-your-session-token), step 3. If you use the ares CLI, it is the file `<device>_webos` in the `.ssh` folder of your user profile.
   - **Paste it** into the `private_key` option, from `-----BEGIN` to the `-----END` line. A paste that ends up on one line is fine. To keep it out of the app options, put it in `secrets.yaml` as a block (`tv_key: |` followed by the indented key lines) and enter `!secret tv_key`. This option wins over an uploaded key.
   - **Let the app fetch it.** Switch **Key Server** on in the TV's Developer Mode app. The app downloads the key once and keeps it; you can switch Key Server off again afterwards.
4. Optional: set `media_player` to the TV's entity from the **LG webOS TV** integration.

It has to be the **private** key. A public key (one line starting with `ssh-rsa`) only identifies the key and cannot log in. Leave the key as it is: it stays encrypted with the passphrase, which is why `passphrase` is always required. If you removed the passphrase from your copy, the option is still required but its value is not used.

The TV issues a new key and passphrase when Developer Mode is switched on again. Upload or paste the new key then, or switch Key Server on once.

**When the token is read**

- Before each scheduled renewal, if the TV is reachable.
- About 20 seconds after `media_player` turns on (and at app start, if the TV is on). If the TV does not answer yet, the app tries twice more.
- When you press **Check TV now** on the TV's card.

**What happens next**

| The TV holds | The app |
| --- | --- |
| the saved token | checks the session with LG and refreshes **Time left**. **TV token** shows *Same as saved*. |
| a different token that LG accepts | notifies you, saves it as the `token` option and renews with it. **TV token** shows *Updated from TV*. |
| a different token, and yours is a `!secret` reference or a custom `url` | notifies you and renews with the new token until the app restarts. It never overwrites the reference: update `secrets.yaml` or the URL yourself. **TV token** shows *Changed on TV (not saved)*. |
| a token LG rejects | shows **Sign in on TV**. |
| no token | shows **Dev Mode off on TV**: switch Developer Mode on again in the TV's app. |

Inside an LG maintenance window the token is still read and compared, but LG is not asked.

**Security**

- The SSH key lets its holder open a restricted shell on the TV. The app keeps it in its private data folder exactly as the TV sends it, encrypted with the passphrase. It is never stored decrypted, and the passphrase is never put on a command line.
- An uploaded key lives only in that data folder. A key in the `private_key` option is also kept in the Supervisor's stored options, which the Supervisor does not encrypt, so prefer the upload or `!secret`.
- A key fetched from the TV comes over plain HTTP on your network; the TV offers nothing else. The key is useless without the passphrase.
- The TV's SSH host key is remembered on first contact. If it changes later, the app refuses to connect and tells you.
- The token read from the TV is not written to the app's data folder.
- The app only ever runs one fixed command on the TV, which prints the token file. It changes nothing on the TV.

## Panel

Open **LG DevMode** from the sidebar, or click **Open web UI** on the app page. Only administrators see it. The panel follows your Home Assistant theme, including dark mode.

- **TV cards:**
  - Status: *Renewed*, *Failed*, *Sign in on TV*, *LG maintenance*, *Renewing…*, *Paused* or *Not run yet*.
  - The last attempt, last success and next renewal.
  - **Time left:** the session time LG reported after the last renewal (`-` when LG gave none, or for a custom `url` outside `developer.lge.com`).
  - An **Auto renew** switch and a **Renew now** button. **Renew now** also works on a paused TV.
  - With `host` set: **TV token**, **Last TV check**, **SSH key** (where the key came from), and the **Check TV now** and **Upload SSH key** buttons.
- **Schedule card:** the interval and retries, the **Failure notifications** switch and **Renew all now**.
- **Phones card:** one switch per Companion app linked to this Home Assistant.

Every switch applies immediately. TV and notification switches are saved into the app options, so they also show in the Configuration tab. Phone switches are kept by the app itself. The panel never shows tokens or renewal URLs.

## Notifications

When a renewal fails after all retries, or LG no longer accepts a TV's token, the app notifies every phone that is switched on in the **Phones** card.

- Phones are found automatically from the `notify.mobile_app_*` services, at start and every 5 minutes.
- New phones are on by default. Phones that are no longer linked disappear from the list. You never type phone names.
- With no linked phones, the app creates a persistent notification instead.

## Usage

- On start, the app immediately renews every enabled TV, then repeats on the schedule.
- The **Log** tab shows each renewal by TV name only. It never shows renewal URLs or tokens.

## Passwords and secrets

- **Where they live:** Home Assistant's Supervisor stores the app options, including each TV's `token`, `url`, `host` and `passphrase`. The Supervisor does not encrypt that storage, and an app cannot change this; Home Assistant can encrypt its backups. The app itself stores only the TV's SSH key, encrypted with the passphrase, and never a password, passphrase or token.
- **Masked:** password fields show as dots in the Configuration tab.
- **Never logged:** passwords, tokens and cookie values never appear in the log or the panel, and numbers are masked.
- **`!secret`:** `token`, `url`, `host`, `passphrase` and `private_key` accept it. To keep a value out of the app options, put it in `/config/secrets.yaml` and reference it, for example `token: "!secret living_room_tv_token"`.
  - The panel switches and live edits keep the reference as it is. The app never writes the value back.
  - A new or changed secret takes effect after you **restart** the app, because the Supervisor reads `secrets.yaml` only at start. Until then the panel keeps the old value and the log says a restart is needed.
- Anyone who can open the app's Configuration tab, or log in to the host, can still see the values. Give the Home Assistant admin role only to people you trust.

## Troubleshooting

- **TV shows "LG maintenance":** LG's site answered with a maintenance page or HTTP 503. That doesn't count as a failure and sends no alert. The app retries after 30 min, then 1 h, 2 h, 4 h, up to 6 h, and returns to the normal interval after the next success.

- **Configuration needs attention:** if the options are invalid (for example no TV yet), the app keeps running and the panel shows exactly what to fix. It starts within 15 seconds of saving valid options.
- **What do the `s6-rc` lines in the log mean?** The app runs as an s6-overlay service, the process supervisor built into Home Assistant app images. Only s6 warnings and errors are logged. Older versions printed `legacy-cont-init`, `legacy-services` and `s6-rc.d is empty` on every start. Those were harmless, and came from running without a proper service definition. The service is registered in the `user` bundle under `/etc/s6-overlay/user-bundles.d`, so the old `defining user bundles ... is deprecated` line no longer appears.
- **Invalid URL:** a session's `url` must be an `https://` address, because it carries the session token. It is masked in the Configuration tab.
- **Network errors:** make sure the Home Assistant host can reach `developer.lge.com`.
- **TV shows "Sign in on TV" (`ERR_005`, *Check user session*, or a sign-in page):** LG no longer accepts the saved token. Follow [After a Developer Mode app update or a new sign-in](#after-a-developer-mode-app-update-or-a-new-sign-in).
- **TV shows "Failed" with `not LG's JSON` in the log:** LG answered with something the app can't read (an empty page, an error page). Earlier versions counted this as a success. It is retried, then reported.
- **No notifications:**
  - Check that `notify_on_failure` is on and that the phone's switch is on in the **Phones** card.
  - The phone must have the Companion app linked to this Home Assistant.
  - The log line `Notifications go to N phone(s)` lists the phones being used.

- **TV shows "Turn on Key Server":** the app has no key the TV accepts (the TV issues a new one when Developer Mode is switched on again) and could not download the current one. Press **Upload SSH key** and choose the TV's current private key, or switch **Key Server** on in the TV's Developer Mode app and press **Check TV now**.
- **TV shows "Replace the SSH key":** the TV refused the key from the `private_key` option. Save the TV's current private key there, or check the `passphrase`.
- **"this is a public key":** you gave the app the public half of the key. Use the private key file instead; its first line reads `-----BEGIN ... PRIVATE KEY-----`.
- **TV shows "Check the passphrase":** the TV refused the key the app had just downloaded. The `passphrase` option does not match the code shown in the Developer Mode app; it changes when Developer Mode is switched on again.
- **TV shows "Dev Mode off on TV":** the TV has no session token. Open the Developer Mode app, sign in and switch Dev Mode on.
- **TV shows "TV host key changed":** the TV's SSH identity is not the one the app first saw, so it did not connect. If you reset or replaced the TV, press **Trust this TV again** on its card: the app forgets the old identity and remembers the new one at the next check. If you did not change the TV, first find out what is answering on that IP address.
- **TV token shows "TV not reachable":** the TV is off, asleep or on another address, or Developer Mode is off. This is not an error and sends no alert.

## Credits

Inspired by:
- [dyarfaradj/homeassistant-addons](https://github.com/dyarfaradj/homeassistant-addons) and the [community thread](https://community.home-assistant.io/t/lg-dev-session-auto-renewal-addon/939012)
- the REST method from [SR-Lut3t1um/Webos-renew-dev](https://github.com/SR-Lut3t1um/Webos-renew-dev)
- multi-token scheduling from [LucifersCircle/webOS-Token-Refresh](https://github.com/LucifersCircle/webOS-Token-Refresh)

LG and webOS are trademarks of LG Electronics. This project is not affiliated with LG.
