# Changelog

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
