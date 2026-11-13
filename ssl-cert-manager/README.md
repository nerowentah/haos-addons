# SSL Cert Manager

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

One place for your Home Assistant's TLS certificates. Request them, import your own, renew them on time and share them with Home Assistant and the other apps. Optionally, use them for secure local access.

## Features

- **Let's Encrypt via Cloudflare DNS.** Get certificates every browser trusts for names under your Cloudflare domain, wildcards included, without opening a port. They renew automatically.
- **Cloudflare Origin certificates.** 15-year certificates that Cloudflare trusts. They secure the Cloudflare Tunnel's connection to Home Assistant.
- **Import your own.** Upload a certificate with its key, a CA certificate to trust, or a CA with its key. With the key, the app issues certificates for local names and IP addresses such as `homeassistant.local` or `192.168.1.10`, and renews them.
- **Published to `/ssl`.** Each certificate gets its own folder, and there is a `default` folder and a trust bundle. Optionally the app also writes `/ssl/fullchain.pem` and `/ssl/privkey.pem`, but it never replaces files it did not write.
- **Secure local access.** An HTTPS proxy on port 8443 serves every certificate by name, while Home Assistant itself stays on `http://`. You can also switch Home Assistant itself to HTTPS: the app edits `configuration.yaml` after a backup and Home Assistant's configuration check.
- **Works with Cloudflare Tunnel.** The tunnel checks Home Assistant's certificate instead of skipping the check. After every certificate change it is checked end to end, and a broken change is rolled back.
- **Notifications** about failed renewals, certificates close to expiry and a failing tunnel check.
- **Private keys stay private.** They are kept in the app's storage, with 0600 copies in `/ssl` for Home Assistant. They are never shown in the panel or written to the log.

## Install

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

1. Add the repository above.
2. Install **SSL Cert Manager** and start it.
3. Open **SSL Certificates** from the sidebar. Imports work straight away. For Let's Encrypt and Cloudflare Origin certificates, first set a Cloudflare API token in the Configuration tab (see the **Documentation** tab).

Full documentation is in the app's **Documentation** tab, or [DOCS.md](DOCS.md). See the [changelog](CHANGELOG.md) for release history.
