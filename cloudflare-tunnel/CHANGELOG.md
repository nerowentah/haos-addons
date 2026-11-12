# Changelog

## 0.2.0

- **Sign in with Cloudflare.** Leave `tunnel_token` empty and the panel walks through the setup: **Sign in with Cloudflare** opens Cloudflare's sign-in, you pick your domain, confirm the suggested hostname (e.g. `home.example.com`), and the app creates the tunnel, its DNS record and the route to Home Assistant, then starts the connector. **Change hostname** and **Sign out** are in the panel. The Zero Trust token setup keeps working as before.
- **Home Assistant proxy settings.** When the tunnel first connects, the app adds `use_x_forwarded_for` and `trusted_proxies` to `configuration.yaml` if they are missing, with a backup and Home Assistant's configuration check (restored if it fails). Home Assistant is only restarted when you click **Restart Home Assistant**. An `http:` block it cannot edit safely is left alone, and the panel shows the snippet.
- **Public URL check.** With `external_hostname` set, the panel links to the public URL and checks every 10 minutes that Home Assistant answers through it (and explains an HTTP 400 or 5xx).
- New option `external_hostname`. The app now maps the Home Assistant configuration folder (read-write) for the proxy settings.

## 0.1.2

- **Fixed: the app did not show in the app store.** The `run_parameters` schema entry was malformed (`$` outside `match(...)`), so the Supervisor rejected the whole config and hid the app. Blank entries and surrounding spaces are now accepted, as the app already ignored them.

## 0.1.1

- **Startup warning removed.** The service is now registered in the `user` bundle under `/etc/s6-overlay/user-bundles.d` instead of the deprecated `/etc/s6-overlay/s6-rc.d/user`, so s6-overlay no longer prints `defining user bundles in /etc/s6-overlay/s6-rc.d is deprecated`. No functional change.
- **Config cleanup.** Removed options that already match Home Assistant defaults, so the app passes the community app config linter. No behaviour change.

## 0.1.0

- First release. Runs the `cloudflared` connector for a remotely managed Cloudflare Tunnel: paste the tunnel's token from Cloudflare Zero Trust and the app keeps the connector running.
- **Watched connector:** the app restarts `cloudflared` if it exits (1, 2, 5, 10, 30, 60s back-off), shows its state, edge connections, restarts and version in a sidebar panel, and offers a **Restart connector** button.
- **Failure notifications:** phones are told when the tunnel has been down for about 30 seconds, and when it recovers. One switch per phone in the panel.
- **Token safety:** the token is passed through the `TUNNEL_TOKEN` environment variable, never the command line, the log or the panel. `!secret` works.
- **Pinned and verified binary:** `cloudflared` 2026.10.7 (≥ 2026.10.2 per Cloudflare's eMudhra CA advisory), downloaded at build time and checked against GitHub's published SHA-256 digest. Auto-update inside the container is off.
- **Run parameters:** optional `--edge-bind-address`, `--edge-ip-version`, `--grace-period`, `--ha-connections`, `--protocol`, `--region`, `--retries` and `--tag` flags; anything else is rejected.
- Derived from homeassistant-apps/app-cloudflared (MIT).
