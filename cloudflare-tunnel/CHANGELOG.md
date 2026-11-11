# Changelog

## 0.1.0

- First release. Runs the `cloudflared` connector for a remotely managed Cloudflare Tunnel: paste the tunnel's token from Cloudflare Zero Trust and the app keeps the connector running.
- **Watched connector:** the app restarts `cloudflared` if it exits (1, 2, 5, 10, 30, 60s back-off), shows its state, edge connections, restarts and version in a sidebar panel, and offers a **Restart connector** button.
- **Failure notifications:** phones are told when the tunnel has been down for about 30 seconds, and when it recovers. One switch per phone in the panel.
- **Token safety:** the token is passed through the `TUNNEL_TOKEN` environment variable, never the command line, the log or the panel. `!secret` works.
- **Pinned and verified binary:** `cloudflared` 2026.10.7 (≥ 2026.10.2 per Cloudflare's eMudhra CA advisory), downloaded at build time and checked against GitHub's published SHA-256 digest. Auto-update inside the container is off.
- **Run parameters:** optional `--edge-bind-address`, `--edge-ip-version`, `--grace-period`, `--ha-connections`, `--protocol`, `--region`, `--retries` and `--tag` flags; anything else is rejected.
- Derived from homeassistant-apps/app-cloudflared (MIT).
