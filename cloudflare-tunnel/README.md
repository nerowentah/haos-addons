# Cloudflare Tunnel

![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

Reach your Home Assistant from anywhere through a **Cloudflare Tunnel** — no open ports, no port forwarding, no dynamic DNS. Outbound-only connections to Cloudflare's edge, with the connector (`cloudflared`) kept running and watched for you.

## Features

- **Remote-managed tunnels.** Create the tunnel in Cloudflare Zero Trust, paste its token, done: public hostnames and routing stay managed from the Cloudflare dashboard.
- **Watched connector.** The app restarts `cloudflared` automatically if it exits, with increasing back-off, and shows its state, edge connections, restarts and version in a sidebar panel.
- **Failure notifications.** Your phones are told when the tunnel has been down for about 30 seconds, and when it recovers.
- **Token safety.** The tunnel token is passed to `cloudflared` through its environment — it never appears in the command line, the log or the panel. `!secret` works.
- **Pinned and verified binary.** `cloudflared` is pinned to a current release and its download is checked against the SHA-256 digest published by the release, so the image can't be tampered with in transit. Auto-update inside the container is disabled.
- **Modern crypto by default.** Post-quantum tunnel encryption has been on by default since cloudflared 2024.1, and this app keeps a version that has it.

## Install

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnerowentah%2Fhaos-addons)

1. Add the repository above.
2. Install **Cloudflare Tunnel** and start it.
3. Create a tunnel in Cloudflare Zero Trust and paste its token in the app's Configuration tab (see the app's **Documentation** tab).
4. Open **Cloudflare Tunnel** from the sidebar. Only administrators see it.

Full documentation is in the app's **Documentation** tab, or [DOCS.md](DOCS.md). See the [changelog](CHANGELOG.md) for release history.
