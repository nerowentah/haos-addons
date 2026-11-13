# Changelog

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
