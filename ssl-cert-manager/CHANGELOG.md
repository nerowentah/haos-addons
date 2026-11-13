# Changelog

## 0.1.2

- **A clear reason for `SSLV3_ALERT_HANDSHAKE_FAILURE`.** This is how Cloudflare's edge answers when it has no certificate for the hostname. The check now says so and names what to check: a two-level name such as `a.b.example.com`, Universal SSL not active yet, or a DNS record that isn't proxied.

## 0.1.1

- **Proxy options**, like the official NGINX SSL proxy:
  - `proxy_hsts` adds a `Strict-Transport-Security` header (off by default).
  - `proxy_real_ip` passes the visitor's address on to Home Assistant. Through Cloudflare Tunnel it works by default (`apps`). Set `cloudflare` to trust Cloudflare's IP ranges when port 8443 is forwarded through Cloudflare's proxy.
  - The proxy card shows both settings.
- **Docs:** local DNS pitfalls (HTTPS/SVCB records, IPv6, Pi-hole forwarding, the Companion app's external URL).
- **The Cloudflare Tunnel check says why.** Instead of just "No answer from the public hostname", the panel and the log name the reason: the name does not resolve, the TLS certificate does not match (with a hint for two-level names such as `a.b.example.com`), the connection was refused, or nothing answered. A changed reason is logged once.
- **Local DNS overrides no longer break the check.** When the hostname resolves to a private address inside Home Assistant (local HTTPS setups often do this), the check looks it up in Cloudflare's public DNS and requests it through Cloudflare, as the Internet would.

## 0.1.0

- First release. One place for your TLS certificates:
  - **Let's Encrypt** through Cloudflare DNS-01, wildcards included, with no open port. Renewed automatically.
  - **Cloudflare Origin CA** certificates (15 years, trusted by Cloudflare). Revoked when deleted.
  - **Imports:** a certificate with its key, a CA to trust, or a CA with its key. A CA with its key issues certificates for local names and IP addresses, and the app renews them.
- **Published to `/ssl/ssl-cert-manager/`.** Each certificate gets its own folder, plus `default/`, a trust bundle (Cloudflare's Origin CA roots and your CAs) and `status.json` for other apps. Optionally the default certificate also goes to `/ssl/fullchain.pem` and `/ssl/privkey.pem`, but only where no other app or person put a file.
- **Secure local access:**
  - An **HTTPS proxy** on port 8443 serves every certificate by name, forwards WebSockets and keeps Home Assistant on `http://`. Home Assistant's `trusted_proxies` are added for it.
  - Optionally, **Home Assistant itself on HTTPS**: `ssl_certificate` is added to `configuration.yaml` with a backup and Home Assistant's configuration check, and Home Assistant restarts only when you click.
- **Cloudflare Tunnel check.** After every certificate or proxy change, the app checks that Home Assistant still answers through the Cloudflare Tunnel app, and notifies you when it does not.
- **Notifications** for failed renewals, certificates close to expiry and a failing tunnel check. One switch per phone in the panel.
- **The panel can upload files** (shared panel update), used to import certificates.
