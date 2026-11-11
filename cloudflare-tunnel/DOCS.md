# Cloudflare Tunnel

A **Cloudflare Tunnel** connects your Home Assistant to the Internet through Cloudflare's network: the connector (`cloudflared`) makes outbound-only connections to Cloudflare's edge, and your traffic arrives through Cloudflare with TLS, your firewall untouched. No open ports, no port forwarding, no dynamic DNS.

This app runs the connector for a **remotely managed** tunnel: you create the tunnel and its public hostnames in Cloudflare Zero Trust, and the app only needs the tunnel's token. It is derived from [homeassistant-apps/app-cloudflared](https://github.com/homeassistant-apps/app-cloudflared) (MIT), rebuilt for this repository's app design.

## Installation

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **Cloudflare Tunnel** and **Start** it.
3. Create the tunnel and paste its token (next section).
4. Open **Cloudflare Tunnel** from the sidebar. Only administrators see it.

## Create the tunnel

1. Open [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) (a free plan is enough) with the Cloudflare account that holds your domain.
2. Go to **Networks → Tunnels** and click **Create a tunnel**.
3. Choose **Cloudflared** as the tunnel type and give the tunnel a name.
4. Cloudflare shows a connector installation command with a long token, for example:
   ```
   cloudflared service install eyJhIjoi...
   ```
   Copy just the token — the `eyJhIjoi...` part — into this app's **tunnel_token** option (Configuration tab) and save. You can skip the rest of Cloudflare's instructions; this app is the connector.
5. Still in Zero Trust, open the tunnel's **Public Hostname** tab and add how you reach Home Assistant, for example `home.example.com` → `http://homeassistant.local:8123`.

If Home Assistant runs on the same machine as this app, the service URL is `http://homeassistant.local:8123` (or your Home Assistant's host or IP).

The app's panel shows **Connected** within a few seconds of saving the token.

## Configuration

```yaml
tunnel_token: ""
notify_on_failure: true
run_parameters: []
log_level: info
```

| Option | Default | Description |
| --- | --- | --- |
| `tunnel_token` | `""` | The token of a remotely managed Cloudflare Tunnel. Required. `!secret` works. |
| `notify_on_failure` | `true` | Notify when the tunnel has been down for about 30 seconds, and when it recovers. |
| `run_parameters` | `[]` | Extra `cloudflared` flags, e.g. `--protocol=quic` or `--region=us`. Only connection flags are allowed (below); anything else is rejected. |
| `log_level` | `info` | How much is logged: `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. Applies without a restart. |

Changes made in the Configuration tab are picked up within about 30 seconds. Changing the token, the log level or the run parameters restarts the connector for you; you don't need to restart the app.

### Passwords and secrets

`tunnel_token` can use a `!secret`:

```yaml
tunnel_token: !secret cloudflare_tunnel_token
```

in your Home Assistant `secrets.yaml`:

```yaml
cloudflare_tunnel_token: eyJhIjoi...
```

The Supervisor resolves secrets into the app only when it starts, so a new secret needs an app restart. The panel and the Configuration tab always show the reference (`!secret cloudflare_tunnel_token`), never the value, and the app writes the reference back — never the secret.

### Run parameters

Only these `cloudflared` flags are accepted, each as `--flag=value`:

`--edge-bind-address`, `--edge-ip-version`, `--grace-period`, `--ha-connections`, `--protocol`, `--region`, `--retries`, `--tag`

Anything else is rejected, so a typo can never break the connector. The app already sets `--no-autoupdate`, `--metrics` and `--loglevel` itself. Logging-related flags (`--logfile`, `--loglevel`) are not allowed because they would take `cloudflared`'s output out of the app's log, where the app watches it.

## Panel

The panel shows one card for the connector:

- **Badge:** the state — **Connected**, **Starting**, **Degraded**, **Restarting** or **Error**.
- **Edge connections:** how many connections to Cloudflare's edge are live (normally 4).
- **Restarts**, **Started** and **Last change.**
- **Restart connector** restarts `cloudflared` immediately.

The **Settings** card shows the active log level and run parameters, with a **Notifications** switch. The **Phones** card has one switch per Companion app linked to this Home Assistant; phones are found automatically every 5 minutes, and new phones are switched on.

## How it works

- The app starts `cloudflared` as a child process: `cloudflared tunnel --no-autoupdate --metrics=127.0.0.1:36500 --loglevel=<level> <run_parameters> run`.
- **The token is passed through the `TUNNEL_TOKEN` environment variable**, never on the command line. Command lines are visible in process listings, so the token never is. The app's log shows `cloudflared`'s output; its lines never contain the token.
- The connector's Prometheus metrics are served on **localhost only** (`127.0.0.1:36500`), inside the container. No ports are published to your network.
- The app watches the connector: it reads its log, counts edge connections from its metrics, and restarts it if it exits, waiting 1, 2, 5, 10, 30 and then 60 seconds between tries. A rejected token is retried every 5 minutes instead of crash-looping.
- `--no-autoupdate` keeps the container's binary the one that was verified at build time (below).

## Security

- **Pinned, verified binary.** The Dockerfile downloads a fixed `cloudflared` release and fails the build unless its SHA-256 matches the digest GitHub publishes for that release asset. Updating means changing the pin here, in the open.
- **2026.10.2 or later.** Following Cloudflare's advisory about the compromised eMudhra intermediate CA, connectors must run `cloudflared` 2026.10.2 or later; this app pins a newer release and never auto-updates to an unvetted one.
- **Post-quantum encryption** between the connector and Cloudflare's edge is on by default (it has been since cloudflared 2024.1); there is no option to weaken it.
- **Least privilege.** The app needs no port mappings, no device access and no USB; it talks to the Supervisor and Home Assistant APIs only for its panel and notifications, and to Cloudflare's edge.

## Notifications

- The tunnel being down for about 30 seconds is reported once, and its recovery is reported. The connector's own restarts (crash back-off) don't trigger a notification unless the tunnel stays down.
- Without any linked phones, the app creates a persistent notification instead.

## Privacy

- **The panel and the log never show the tunnel token.** The log shows connector output, states and counts.
- **`/data/state.json`** holds only the phone notification switches.
- `cloudflared`'s metrics endpoint answers on localhost inside the container only.

## Troubleshooting

- **"Cloudflare rejected the tunnel token":** the token is wrong or was rotated. Copy the token from Zero Trust (**Networks → Tunnels → your tunnel → …**) again and save it in the Configuration tab.
- **Panel stuck on Starting:** check the app's log; set `log_level: debug` for `cloudflared`'s debug output. A firewall blocking outbound UDP 7844 (QUIC) is the usual cause — `--protocol=http` in `run_parameters` works as a fallback.
- **Edge connections 0 but the process runs:** the connector lost its edge connections; the panel shows **Degraded** and `cloudflared` keeps retrying on its own.
- **Wrong version warning from Cloudflare:** Zero Trust flags connectors older than 2026.10.2. Update this app to get the pinned release.
- The app's **Restart connector** button is a full stop and start of `cloudflared`.

## Attribution

Derived from [homeassistant-apps/app-cloudflared](https://github.com/homeassistant-apps/app-cloudflared) by the homeassistant-apps authors (MIT). The `cloudflared` connector is [Cloudflare's](https://github.com/cloudflare/cloudflared), also MIT.
