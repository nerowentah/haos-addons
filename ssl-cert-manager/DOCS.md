# SSL Cert Manager

SSL Cert Manager keeps your Home Assistant's TLS certificates in one place:

- **gets** them from Let's Encrypt or Cloudflare, or **imports** yours;
- **renews** them before they expire;
- **publishes** them to the shared `/ssl` folder for Home Assistant and other apps.

On top of that it can:

- give your home network **secure local access** with an HTTPS proxy;
- let the **Cloudflare Tunnel** app check Home Assistant's certificate instead of skipping the check.

## Installation

1. Go to **Settings → Apps → App store**, open **⋮ → Repositories** and add:
   ```
   https://github.com/nerowentah/haos-addons
   ```
2. Install **SSL Cert Manager** and **Start** it.
3. Open **SSL Certificates** from the sidebar (only administrators see it).

## Cloudflare API token

You need this token for Let's Encrypt and Cloudflare Origin certificates. Imports don't need it.

1. In the Cloudflare dashboard, open **My Profile → API Tokens → Create Token → Create Custom Token**.
2. Add these permissions, for your domain only (**Zone Resources → Include → Specific zone**):
   - **Zone → Zone → Read**
   - **Zone → DNS → Edit** (Let's Encrypt places a TXT record while it checks)
   - **Zone → SSL and Certificates → Edit** (Cloudflare Origin certificates)
3. Paste the token into `cloudflare_api_token` in the Configuration tab, or use a `!secret`.

The token reaches certbot only through a private (0600) file, and only while a request runs. It never appears on the command line, in the log or in the panel.

## Getting certificates

All of these are in the panel's **Add a certificate** card.

### Let's Encrypt

**Let's Encrypt** asks for one or more names under your Cloudflare domain, such as `home.example.com`. Wildcards like `*.example.com` work too.

To prove you own the domain, certbot places a TXT record through the Cloudflare API. That is a DNS-01 challenge: no port is opened and nothing on your network has to be reachable. The certificate is trusted by every browser and phone.

- **Renewal:** automatic, `renew_days_before` days before expiry (30 by default). A failed renewal is retried every 12 hours and sends a notification.
- **Testing:** switch on **Let's Encrypt staging** first. Staging certificates are not trusted, but the rate limits are much higher. Switch staging off and click **Renew now** to get a real certificate.

### Cloudflare Origin

**Cloudflare Origin** asks for names in the same way. The private key is made in the app; only a certificate signing request is sent to Cloudflare. The certificate is valid for 15 years.

It is **trusted by Cloudflare only, not by browsers**. That makes it the right certificate for the connection between the Cloudflare Tunnel and Home Assistant. **Delete** also revokes it at Cloudflare.

### Import certificate

**Import certificate** opens a file picker. Select the certificate, its chain and its private key together; one combined `.pem` works too.

- The app checks that the key belongs to the certificate, puts the chain in order and rejects expired or CA certificates.
- Password-protected keys are refused. Remove the password first, for example with `openssl pkey -in key.pem -out key-plain.pem`.
- Imported certificates can't be renewed by the app. You get a notification `renew_days_before` days before expiry, and again once it has expired.

### Your own CA

Use **Import CA** in the **Certificate authorities** section.

- **CA certificate only:** the CA is trusted. It joins the **trust bundle** (`/ssl/ssl-cert-manager/trust/ca-bundle.pem`), next to Cloudflare's Origin CA roots, for apps that check certificates.
- **CA certificate and its private key:** the CA can also **issue certificates**.
  - **Issue certificate** asks for names and IP addresses, for example `homeassistant.local, 192.168.1.10`.
  - The certificate gets a new P-256 key, `serverAuth` and a lifetime of 397 days at most (never past the CA's own expiry).
  - It renews automatically.
  - Install the CA on your phones and computers, and `https://homeassistant.local:8443` is trusted at home.

**Remove** takes a CA out of the trust bundle. Certificates it issued stay, but are no longer renewed.

## Where the files are

| Path | What |
| --- | --- |
| `/ssl/ssl-cert-manager/<id>/fullchain.pem`, `privkey.pem`, `chain.pem` | Each certificate (the panel shows its path) |
| `/ssl/ssl-cert-manager/default/…` | The certificate marked **default** (★). Home Assistant's own HTTPS uses this one |
| `/ssl/ssl-cert-manager/trust/ca-bundle.pem` | Cloudflare's Origin CA roots plus every imported CA |
| `/ssl/ssl-cert-manager/status.json` | What other apps (Cloudflare Tunnel) need to know: certificates, names, proxy |
| `/ssl/fullchain.pem`, `/ssl/privkey.pem` | The default certificate under the names most apps expect, while **Also write /ssl/fullchain.pem** is on |

- Private keys are written with mode 0600.
- The app writes `/ssl/fullchain.pem` and `/ssl/privkey.pem` only when they don't exist yet, or still hold exactly what the app wrote last time. If anything else put a file there, it is left alone and the panel says so.
- The first certificate you add becomes the default. **Make default** changes it.

## Secure local access

### HTTPS proxy (recommended)

Switch on **HTTPS proxy** in the panel, or set `local_proxy: true`. nginx then listens on **port 8443** and forwards everything to Home Assistant, WebSockets included:

- **Certificates:** every certificate is served, picked by the name the browser asks for (SNI). The default certificate answers any other name.
- **Proxy settings:** the first time, the app adds `use_x_forwarded_for` and `trusted_proxies` for its own network to `configuration.yaml`, the same way the Cloudflare Tunnel app does. That way Home Assistant sees the real client address. Then click **Restart Home Assistant** in the panel.
- **What stays the same:** Home Assistant itself is untouched. `http://homeassistant.local:8123`, the Companion apps, add-on ingress and automations keep working.

To use a public name such as `home.example.com` at home, point it at your Home Assistant in your local DNS (router, Pi-hole, AdGuard). Then open `https://home.example.com:8443`. The Cloudflare Tunnel check is not fooled by this: it notices the private address and asks Cloudflare's public DNS instead.

The port can be changed or cleared in the app's **Network** settings. With the port cleared, the proxy is only reachable from other apps, such as Cloudflare Tunnel.

### Local DNS (split DNS) pitfalls

Pointing a public name at Home Assistant's LAN address in your router or Pi-hole is the usual way to use the proxy at home. Watch for these:

- **HTTPS (SVCB) DNS records.** Current browsers also ask for the name's `HTTPS` record (type 65), and Cloudflare answers it with its own addresses. Overriding only the `A` record then sends the browser to Cloudflare anyway.
  - **Fix:** override or block the `HTTPS` record for that name too.
  - **Or:** leave the name public and use a separate local-only name (for example `ha.home.example.com`) for port 8443.
- **IPv6.** Override (or block) the `AAAA` record as well as `A`. Otherwise devices with IPv6 still get Cloudflare's address.
- **Pi-hole forwarding.** If the local record seems ignored, turn on **Never forward reverse lookups for private IP ranges** and check that the local record (not a CNAME to the public name) is what answers.
- **Phones away from home.** The Companion app's external URL must be the public `https://home.example.com` (through the tunnel), not `https://home.example.com:8443`. Port 8443 only exists on your network.

The Cloudflare Tunnel check is not affected: it notices the private address and asks Cloudflare's public DNS instead.

### Proxy options

Modelled on the official NGINX SSL proxy add-on:

- **`proxy_hsts`** (default empty, so no header): a `Strict-Transport-Security` value such as `max-age=31536000`.
  - Browsers then refuse plain `http://` for that name, so set it only once HTTPS works.
  - Add `; includeSubDomains` only if every subdomain has HTTPS.
- **`proxy_real_ip`**: which client address Home Assistant sees.
  - `apps` (default) trusts `X-Forwarded-For` from other apps, such as Cloudflare Tunnel. Login attempts and IP bans then show the visitor, not `172.30.33.x`.
  - `cloudflare` also trusts Cloudflare's published IP ranges and their `CF-Connecting-IP` header. Use it when you forward port 8443 through Cloudflare's proxy (the orange cloud) without the tunnel.
  - `off` passes on only the direct peer.

### Home Assistant itself on HTTPS (optional)

**Turn on** under **Home Assistant itself on HTTPS** adds this to `configuration.yaml`:

```yaml
http:
  ssl_certificate: /ssl/ssl-cert-manager/default/fullchain.pem
  ssl_key: /ssl/ssl-cert-manager/default/privkey.pem
```

- The file is backed up as `configuration.yaml.ssl-cert-manager.bak` and checked with Home Assistant's configuration check. If the check fails, the original goes back.
- Home Assistant is only restarted when you click **Restart Home Assistant**.
- A renewed default certificate also needs a restart. The panel asks for it, and you get a notification.

> **Warning:** after the restart Home Assistant answers **only** on `https://`. Every `http://` address stops working, including Companion apps and integrations set up with `http://`. The default certificate must cover the name you use to reach Home Assistant. Most setups only need the proxy.

**Turn off** removes exactly those two lines again. If `http:` already has another `ssl_certificate`, the app leaves it alone and shows the snippet instead.

## Cloudflare Tunnel

With Cloudflare Tunnel 0.3.0 or later, the tunnel reads `/ssl/ssl-cert-manager/status.json`:

- **HTTPS proxy on:** the tunnel sends traffic to `https://<this app>:8443` and checks the certificate against the trust bundle, for a name the proxy has a certificate for. An Origin certificate for your tunnel hostname is the natural fit.
- **Home Assistant itself on HTTPS:** the tunnel checks Home Assistant's certificate the same way, instead of `noTLSVerify`.

After every change that affects the tunnel, both apps check it end to end:

- **Cloudflare Tunnel:**
  1. waits for the connector to reconnect;
  2. requests `https://<hostname>/api/`;
  3. when that fails, **puts the previous route back** and notifies you. The reason is shown, for example `x509: certificate signed by unknown authority`.
- **SSL Cert Manager:** checks that the tunnel app runs, waits for it to settle, then requests the public hostname itself. Its **Cloudflare Tunnel** card shows the result and the tunnel's own check. **Check now** runs it again without changing anything.

## Configuration

```yaml
cloudflare_api_token: ""
acme_email: ""
acme_staging: false
local_proxy: false
write_ssl_root: true
renew_days_before: 30
proxy_hsts: ""
proxy_real_ip: apps
notify_on_failure: true
log_level: info
```

| Option | Default | Description |
| --- | --- | --- |
| `cloudflare_api_token` | `""` | For Let's Encrypt and Cloudflare Origin certificates (see above). `!secret` works. |
| `acme_email` | `""` | Let's Encrypt writes to this address about your certificates. Optional. |
| `acme_staging` | `false` | Let's Encrypt test certificates (not trusted, higher limits). |
| `local_proxy` | `false` | The HTTPS proxy on port 8443. |
| `write_ssl_root` | `true` | Also write the default certificate to `/ssl/fullchain.pem` and `/ssl/privkey.pem` (only where allowed, see above). |
| `proxy_hsts` | `""` | `Strict-Transport-Security` value for the HTTPS proxy, e.g. `max-age=31536000`; empty sends none. |
| `proxy_real_ip` | `apps` | Client address passed to Home Assistant: `apps`, `cloudflare` or `off` (see **Proxy options**). |
| `renew_days_before` | `30` | Renew (or warn) this many days before expiry, from 7 to 60. |
| `notify_on_failure` | `true` | Notifications about renewals, expiry and the tunnel check. |
| `log_level` | `info` | `trace`, `debug`, `info`, `notice`, `warning`, `error` or `fatal`. Applies without a restart. |

The switches in the panel save these options. Changes in the Configuration tab are picked up within about 30 seconds, with no restart.

## Panel

- **Certificates:** one card per certificate (names, issuer, expiry, renewal, file path) with **Make default**, **Renew now** and **Delete**, plus the **Add a certificate** card. An **Activity** card shows what is running and the last result.
- **Local secure access:** the **HTTPS proxy** card, with its switch and links, and the **Home Assistant itself on HTTPS** card.
- **Cloudflare Tunnel:** the latest tunnel check.
- **Certificate authorities:** the imported CAs with **Issue certificate** and **Remove**, and the trust bundle with **Import CA**.
- **Settings and Phones:** notifications, `/ssl` root files, Let's Encrypt staging, and one notification switch per Companion app.

## Privacy and security

- Private keys never leave the app's storage and `/ssl`. They are never shown in the panel, sent anywhere or written to the log.
- Only a certificate signing request goes to Cloudflare for Origin certificates. Only certbot talks to Let's Encrypt and Cloudflare's DNS API.
- Uploads travel through Home Assistant's authenticated ingress connection, are checked, and stored with mode 0600.
- The `configuration.yaml` edits are minimal, backed up, checked by Home Assistant and undone if the check fails. The app never restarts Home Assistant by itself.

## Troubleshooting

- **Let's Encrypt: "DNS problem" or "unauthorized":**
  - The name must be in the Cloudflare zone that the token can edit.
  - Check the token permissions (above).
  - Set `log_level: debug` to see certbot's full output.
- **"Cloudflare refused the API token":** the token lacks **SSL and Certificates → Edit** for that zone.
- **The proxy shows "Error":** nginx rejected the configuration. The message names the problem, and the previous configuration keeps running.
- **HTTP 400 through the proxy:** Home Assistant does not trust the proxy yet. Restart Home Assistant if the panel asks for it, or add the snippet shown in the proxy card.
- **The browser warns about the certificate on port 8443:**
  - The name in the address bar must be on one of the certificates.
  - For certificates from your own CA, install the CA on that device.
- **"No answer from the public hostname: ..."** now ends with the reason:
  - **"does not resolve from Home Assistant or in public DNS"**: the DNS record is missing or still new. Check the CNAME in the Cloudflare dashboard, and wait a few minutes after creating it.
  - **"TLS: ... refused the handshake for ..., so Cloudflare has no edge certificate for it"** (`SSLV3_ALERT_HANDSHAKE_FAILURE`): Cloudflare's edge has no certificate for the name, so it ends the connection instead of showing one. Check, in the Cloudflare dashboard:
    - **The name is one level deep.** The free Universal certificate covers `example.com` and `*.example.com` only. `ha.home.example.com` needs an Advanced Certificate (paid), or use `home.example.com`.
    - **SSL/TLS → Edge Certificates:** Universal SSL is enabled and the certificate is **Active**. On a new domain, or after re-enabling it, this can take up to 24 hours.
    - **DNS:** the hostname's record is **Proxied** (orange cloud).
    - Port 8443 working at home doesn't rule this out: that is SSL Cert Manager's own certificate, not Cloudflare's.
  - **"TLS: Hostname mismatch ..."**: a server other than Cloudflare's edge answered with a certificate for another name (for example a local DNS override that points at another device).
  - **"connection refused by ..."** or **"no answer within 15 s from ..."**: Cloudflare's edge could not be reached from Home Assistant (a firewall or outbound filtering). With **"(local DNS override; the name has no public address)"**, the name only exists in your local DNS.
- **A local DNS override is fine.** When your router or Pi-hole points the hostname at Home Assistant's LAN address, the check notices it, looks the name up in Cloudflare's public DNS (1.1.1.1) instead and still goes through the tunnel. The panel then adds "(local DNS points to 192.168.x.x; checked through public DNS)".
- **Cloudflare Tunnel check: HTTP 502 or "x509":** the tunnel cannot verify the proxy's certificate. Use a Let's Encrypt or Cloudflare Origin certificate for the tunnel hostname, or a CA in the trust bundle.
