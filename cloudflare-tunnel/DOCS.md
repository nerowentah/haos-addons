# Cloudflare Tunnel

A **Cloudflare Tunnel** connects your Home Assistant to the Internet through Cloudflare's network: the connector (`cloudflared`) makes outbound-only connections to Cloudflare's edge, and your traffic arrives through Cloudflare with TLS, your firewall untouched. No open ports, no port forwarding, no dynamic DNS.

There are two ways to set it up:

- **Sign in with Cloudflare** (easiest): you sign in once from the app's panel and pick your domain; the app creates the tunnel, its DNS record, the route to Home Assistant and Home Assistant's proxy settings.
- **Zero Trust token:** you create a tunnel and its public hostname in Cloudflare Zero Trust yourself and paste the tunnel's token.

The app is derived from [homeassistant-apps/app-cloudflared](https://github.com/homeassistant-apps/app-cloudflared) (MIT), rebuilt for this repository's app design.

## Installation

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **Cloudflare Tunnel** and **Start** it.
3. Open **Cloudflare Tunnel** from the sidebar (only administrators see it) and follow one of the two setups below.

You need a Cloudflare account (the free plan is enough) with your domain added to it.

## Sign in with Cloudflare

1. In the panel, click **Sign in with Cloudflare**, then **Open Cloudflare sign-in**. Cloudflare opens in a new tab.
2. Log in, pick the domain to use for Home Assistant and click **Authorize**. You can close that tab; the panel continues by itself.
3. Click **Choose hostname** and confirm the suggested name (e.g. `home.example.com`) or type another one under your domain.

The app then:

- creates a tunnel named `home-assistant` in your Cloudflare account,
- adds a `CNAME` record for the hostname that points to the tunnel (if the name already has a DNS record, the panel asks before replacing it),
- sends that hostname to Home Assistant (`http://homeassistant:8123`, or `https://` if Home Assistant uses SSL) and everything else to a 404,
- starts the connector, and once it is connected, makes Home Assistant trust it (next section).

To change the hostname later, use **Change hostname** in the panel (or edit `external_hostname`); the app adds the new DNS record and updates the route. **Sign out** stops the connector, deletes the tunnel at Cloudflare and removes the saved sign-in; the DNS record stays, so delete it in the Cloudflare dashboard if you no longer need it.

## Home Assistant proxy settings

Home Assistant rejects requests that come through a proxy (it answers **400 Bad Request**) unless `configuration.yaml` trusts the proxy:

```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 172.30.33.0/24
```

When the tunnel first connects, the app adds this for you, in either setup:

- If there is no `http:` block, it appends the one above. If there is a plain `http:` block, it adds only what is missing, at that block's indentation. If the network is already trusted (also through a wider entry such as `172.30.32.0/23`), nothing changes.
- It saves a backup as `configuration.yaml.cloudflare-tunnel.bak` first, then runs Home Assistant's configuration check. If the check fails, the original file is put back.
- It **never restarts Home Assistant by itself**: the panel shows **Restart Home Assistant**, which applies the change after you confirm.
- An `http:` block it cannot edit safely (`http: !include ...`, flow style, `use_x_forwarded_for: false`) is left alone; the panel shows the snippet to add yourself.

This happens once per install; if you later remove the block, the app does not add it again (use **Check again** in the panel to re-run it).

## Checking Home Assistant's certificate

Without the **SSL Cert Manager** app, the tunnel works as before:

- Home Assistant on `http://` is reached on the internal network.
- Home Assistant on HTTPS is reached with `noTLSVerify`, so its certificate is not checked.

With SSL Cert Manager installed, this app reads `/ssl/ssl-cert-manager/status.json` and points the route at a checked origin (sign-in setup):

| SSL Cert Manager | The tunnel sends traffic to | Certificate check |
| --- | --- | --- |
| HTTPS proxy on | `https://<SSL Cert Manager>:8443` | `caPool` = its trust bundle, `originServerName` = your hostname if a certificate covers it |
| Home Assistant itself on HTTPS | `https://homeassistant:8123` | Same check, for the default certificate's name |
| Neither | `http://homeassistant:8123` | None needed (internal network) |

- The trust bundle holds Cloudflare's Origin CA roots and your imported CAs. Publicly trusted certificates such as Let's Encrypt are accepted too.
- A Cloudflare Origin certificate for your tunnel hostname is the natural choice for the proxy.
- When SSL Cert Manager changes something (a new certificate, the proxy switched on), the route follows within about 30 seconds.

The `origin_tls` option controls this:

- `auto` (default): check where possible, and undo a change that breaks the tunnel.
- `verify`: always check (never `noTLSVerify`), and report failures without undoing them.
- `skip`: the behaviour before 0.3.0.

**Zero Trust token setup:** Cloudflare manages the route, so set it in Zero Trust under **Networks → Tunnels → your tunnel → Public Hostname → (edit) → Additional application settings → TLS**:

- **CA Pool:** `/ssl/ssl-cert-manager/trust/ca-bundle.pem`
- **Origin Server Name:** a name on your certificate
- **No TLS Verify:** off

The panel shows the path.

## Tunnel check after every change

After a new route, an origin change, new options and at start, the app checks the tunnel end to end:

1. The connector must be connected again (at least one edge connection) within a minute.
2. `https://<hostname>/api/` must answer with Home Assistant (HTTP 200 or 401). It is tried three times, 10 seconds apart.
3. If that fails, the reason comes from `cloudflared`'s log when it is a TLS problem (`x509: ...`, `certificate is valid for ...`), or from the HTTP status:
   - **502:** the origin connection failed;
   - **526:** invalid origin certificate;
   - **530:** not routed.

When a change made by the app fails the check in `auto` mode:

- The previous `config.yml` is put back (it is kept as `config.yml.prev` until a change is verified) and the connector restarts.
- The old route is checked again, and you are notified.
- That route is not tried again until SSL Cert Manager's files change.

The **Connector** card shows the result: **Tunnel check passed**, **failed** or **Change rolled back**, with the reason. **Check now** runs the check without changing anything. The result is also written to `/ssl/cloudflare-tunnel/status.json`, where SSL Cert Manager shows it.

## Use a Zero Trust token instead

1. Open [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) with the Cloudflare account that holds your domain.
2. Go to **Networks → Tunnels** and click **Create a tunnel**, choose **Cloudflared** and name it.
3. Cloudflare shows a connector installation command with a long token, for example:
   ```
   cloudflared service install eyJhIjoi...
   ```
   Copy just the token — the `eyJhIjoi...` part — into this app's **tunnel_token** option (Configuration tab) and save. You can skip the rest of Cloudflare's instructions; this app is the connector.
4. In the tunnel's **Public Hostname** tab, add how you reach Home Assistant, for example `home.example.com` → `http://homeassistant:8123`.
5. Optionally set `external_hostname` to that hostname, so the panel links to it and checks that it answers.

The panel shows **Connected** within a few seconds of saving the token.

## Configuration

```yaml
tunnel_token: ""
external_hostname: ""
notify_on_failure: true
run_parameters: []
origin_tls: auto
log_level: info
```

| Option | Default | Description |
| --- | --- | --- |
| `tunnel_token` | `""` | Empty: sign in from the panel. Otherwise the token of a tunnel made in Zero Trust. `!secret` works. |
| `external_hostname` | `""` | The public hostname, e.g. `home.example.com` (no `https://`). Normally set from the panel. |
| `notify_on_failure` | `true` | Notify when the tunnel has been down for about 30 seconds, and when it recovers. |
| `run_parameters` | `[]` | Extra `cloudflared` flags, e.g. `--protocol=quic` or `--region=us`. Only connection flags are allowed (below); anything else is rejected. |
| `origin_tls` | `auto` | How Home Assistant's certificate is checked: `auto`, `verify` or `skip` (see **Checking Home Assistant's certificate**). |
| `log_level` | `info` | How much is logged: `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. Applies without a restart. |

Changes made in the Configuration tab are picked up within about 30 seconds. Changing the token, the log level or the run parameters restarts the connector for you; a new `external_hostname` is routed and applied too. You don't need to restart the app.

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

Anything else is rejected, so a typo can never break the connector. The app already sets `--no-autoupdate`, `--metrics`, `--loglevel` and (when signed in) `--config` itself. Logging-related flags (`--logfile`, `--loglevel`) are not allowed because they would take `cloudflared`'s output out of the app's log, where the app watches it.

## Panel

Until the tunnel runs (sign-in setup), the **Set up** card walks through the three steps — sign in, hostname, tunnel and DNS record — with the button for the next one, and shows any error with **Try again**.

Then the **Connector** card shows:

- **Badge:** the state — **Connected**, **Starting**, **Degraded**, **Restarting**, **Error** or **Not set up**.
- **Edge connections:** how many connections to Cloudflare's edge are live (normally 4).
- **Restarts**, **Started** and **Last change.**
- The **public URL** with the result of the last check: the app requests `https://<hostname>/api/` every 10 minutes through Cloudflare (bypassing a local DNS override); Home Assistant answering (even with "login required") means **Reachable**. Without an answer, the reason is shown.
- **Tunnel check** and **origin**: the result of the last check after a change, and where the tunnel sends traffic (with or without a certificate check).
- **Restart connector**, **Check now**, and when signed in **Change hostname** and **Sign out**.

The **Home Assistant proxy settings** card appears while there is something to do: a restart to apply the change, or a snippet to add yourself.

The **Settings** card shows the setup type, log level and run parameters, with a **Notifications** switch. The **Phones** card has one switch per Companion app linked to this Home Assistant; phones are found automatically every 5 minutes, and new phones are switched on.

## How it works

- **Sign-in setup:** the app runs Cloudflare's own commands for a locally managed tunnel. `cloudflared tunnel login` gives the sign-in link and, once you authorize a domain, saves a certificate for that domain; `cloudflared tunnel create` makes the tunnel and its credentials; `cloudflared tunnel route dns` adds the CNAME. The app writes `config.yml` with the route to Home Assistant and runs `cloudflared tunnel --config /data/cloudflared/config.yml run <tunnel id>`. All of it lives in `/data/cloudflared` (folder mode 0700).
- **Token setup:** the app runs `cloudflared tunnel --no-autoupdate --metrics=127.0.0.1:36500 --loglevel=<level> <run_parameters> run`, and **the token is passed through the `TUNNEL_TOKEN` environment variable**, never on the command line. Command lines are visible in process listings, so the token never is.
- The connector's Prometheus metrics are served on **localhost only** (`127.0.0.1:36500`), inside the container. No ports are published to your network.
- The app watches the connector: it reads its log, counts edge connections from its metrics, and restarts it if it exits, waiting 1, 2, 5, 10, 30 and then 60 seconds between tries. Rejected credentials are retried every 5 minutes instead of crash-looping.
- `--no-autoupdate` keeps the container's binary the one that was verified at build time (below).

## Security

- **Pinned, verified binary.** The Dockerfile downloads a fixed `cloudflared` release and fails the build unless its SHA-256 matches the digest GitHub publishes for that release asset. Updating means changing the pin here, in the open.
- **2026.10.2 or later.** Following Cloudflare's advisory about the compromised eMudhra intermediate CA, connectors must run `cloudflared` 2026.10.2 or later; this app pins a newer release and never auto-updates to an unvetted one.
- **Post-quantum encryption** between the connector and Cloudflare's edge is on by default (it has been since cloudflared 2024.1); there is no option to weaken it.
- **Least privilege.** The app needs no port mappings, no device access and no USB. It talks to the Supervisor and Home Assistant APIs for its panel, notifications and the configuration check, and to Cloudflare. It writes to your Home Assistant configuration folder only to add the proxy settings described above, with a backup and Home Assistant's own check. From `/ssl` it reads SSL Cert Manager's status and trust bundle, and writes only its own `/ssl/cloudflare-tunnel/status.json`.

## Notifications

- The tunnel being down for about 30 seconds is reported once, and its recovery is reported. The connector's own restarts (crash back-off) don't trigger a notification unless the tunnel stays down.
- Without any linked phones, the app creates a persistent notification instead.

## Privacy

- **The panel and the log never show the tunnel token, the sign-in certificate or the tunnel credentials.** The log shows connector output, states, counts and setup steps.
- **`/data/cloudflared`** holds the sign-in certificate (it contains an API token for the domain you picked), the tunnel credentials and `config.yml`, readable by the app only. **Sign out** deletes them.
- **`/data/state.json`** holds the phone notification switches, the routed hostname and the outcome of the proxy-settings step.
- `cloudflared`'s metrics endpoint answers on localhost inside the container only.

## Troubleshooting

- **"Cloudflare rejected the tunnel token":** the token is wrong or was rotated. Copy the token from Zero Trust (**Networks → Tunnels → your tunnel → …**) again and save it in the Configuration tab.
- **"Cloudflare rejected the tunnel credentials"** (sign-in setup): the tunnel was deleted in the dashboard. **Sign out** and sign in again.
- **The sign-in link expired:** it is valid for about 10 minutes; click **Sign in with Cloudflare** again.
- **"already has a DNS record":** the hostname is used by another record. **Replace DNS record** points it at this tunnel, or choose another name.
- **Public URL shows HTTP 400:** Home Assistant does not trust the tunnel yet. Restart Home Assistant if the panel asks for it, or add the snippet from **Home Assistant proxy settings**.
- **"No answer from the public hostname: ..."** now ends with the reason:
  - **"does not resolve from Home Assistant or in public DNS"**: the DNS record is missing or still new. Check the CNAME in the Cloudflare dashboard, and wait a few minutes after creating it.
  - **"TLS: Hostname mismatch ..."**: the certificate at Cloudflare's edge does not cover the name. Cloudflare's free certificate covers `example.com` and `*.example.com` only, so use a name one level deep such as `home.example.com`, or add an Advanced Certificate.
  - **"connection refused by ..."** or **"no answer within 15 s from ..."**: Cloudflare's edge could not be reached from Home Assistant (a firewall or outbound filtering). With **"(local DNS override; the name has no public address)"**, the name only exists in your local DNS.
- **A local DNS override is fine.** When your router or Pi-hole points the hostname at Home Assistant's LAN address, the check notices it, looks the name up in Cloudflare's public DNS (1.1.1.1) instead and still goes through the tunnel. The panel then adds "(local DNS points to 192.168.x.x; checked through public DNS)".
- **Public URL shows HTTP 502 or 530:** Cloudflare reached the tunnel but not Home Assistant; check that Home Assistant runs and look at the app's log.
- **"Change rolled back" with `x509: ...`:** the certificate the tunnel saw does not match. Give the origin a certificate that covers your tunnel hostname (a Cloudflare Origin or Let's Encrypt certificate in SSL Cert Manager), or set `origin_tls: skip` to go back to not checking.
- **The Companion app cannot connect through the tunnel, but the browser can:** a Cloudflare Access (Zero Trust login) policy on the hostname blocks the app and webhooks, which can't show a login page. Leave Access off for Home Assistant, or add a Bypass policy for `/api/*` and `/auth/*`.
- **"caPool replaces the normal CAs?"** No. `cloudflared` starts from the system certificate store and adds the trust bundle, so a Let's Encrypt certificate on the origin still verifies.
- **Panel stuck on Starting:** check the app's log; set `log_level: debug` for `cloudflared`'s debug output. A firewall blocking outbound UDP 7844 (QUIC) is the usual cause — `--protocol=http` in `run_parameters` works as a fallback.
- **Edge connections 0 but the process runs:** the connector lost its edge connections; the panel shows **Degraded** and `cloudflared` keeps retrying on its own.
- **Wrong version warning from Cloudflare:** Zero Trust flags connectors older than 2026.10.2. Update this app to get the pinned release.
- The app's **Restart connector** button is a full stop and start of `cloudflared`.

## Attribution

Derived from [homeassistant-apps/app-cloudflared](https://github.com/homeassistant-apps/app-cloudflared) by the homeassistant-apps authors (MIT). The `cloudflared` connector is [Cloudflare's](https://github.com/cloudflare/cloudflared), also MIT.
