# Changelog

## 0.3.2

- **Two-level hostnames are caught in the panel.** When the public hostname is two levels below your domain (`ha.int.example.com`), the connector card explains that Cloudflare's free certificate doesn't cover it, and **Change hostname** is pre-filled with a one-level name (`ha.example.com`). Home Assistant's own certificate keeps working for the tunnel. The hint goes away once the public URL works, e.g. with an Advanced Certificate.
- **A clear reason for `SSLV3_ALERT_HANDSHAKE_FAILURE`.** This is how Cloudflare's edge answers when it has no certificate for the hostname. The check now says so and names what to check: a two-level name such as `a.b.example.com`, Universal SSL not active yet, or a DNS record that isn't proxied.

## 0.3.1

- **Docs:** Cloudflare Access breaks the Companion app (use a bypass for `/api/*`), and `caPool` adds to the system CAs.
- **The public URL check says why.** Instead of just "No answer from the public hostname", the panel and the log name the reason: the name does not resolve, the TLS certificate does not match (with a hint for two-level names such as `a.b.example.com`), the connection was refused, or nothing answered. A changed reason is logged once.
- **Local DNS overrides no longer break the check.** When the hostname resolves to a private address inside Home Assistant (local HTTPS setups often do this), the check looks it up in Cloudflare's public DNS and requests it through Cloudflare, as the Internet would.

## 0.3.0

- **Home Assistant's certificate is checked.** With the new **SSL Cert Manager** app, the tunnel no longer uses `noTLSVerify`:
  - With its HTTPS proxy on, traffic goes through the proxy and the certificate is checked against its trust bundle for a name the proxy serves.
  - With Home Assistant itself on HTTPS, Home Assistant's certificate is checked the same way.
  - The route follows changes in SSL Cert Manager by itself.
  - Without SSL Cert Manager nothing changes.
  - New option `origin_tls`: `auto` (default), `verify` (never skip the check) or `skip` (the 0.2 behaviour).
- **Tunnel check after every change.** After a new route, an origin change or new options, the app:
  1. waits for the connector to reconnect;
  2. requests `https://<hostname>/api/` (three tries);
  3. reads origin TLS errors from `cloudflared`'s log.

  If the change broke the tunnel, the previous route is **put back automatically** and you are notified with the reason, for example `x509: certificate signed by unknown authority`. A rolled-back route is not retried until SSL Cert Manager changes something.
- The panel shows the tunnel check and where traffic goes. **Check now** (formerly **Check public URL**) runs the check without changing anything.
- HTTP 502, 526 and 530 from the public URL are explained.
- The app maps `/ssl` to read SSL Cert Manager's files and to publish its own status in `/ssl/cloudflare-tunnel/status.json`.
- **Shared panel update:** buttons can upload files. No visible change in this app.

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
