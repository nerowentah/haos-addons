ARG BUILD_VERSION
ARG BUILD_ARCH

# Multi-arch Alpine base (aarch64/amd64). S6-Overlay, bashio and
# ca-certificates are already provided.
FROM ghcr.io/home-assistant/base:3.24

ARG BUILD_VERSION
ARG BUILD_ARCH

# python3 + cryptography: certificate parsing, keys and signing.
# certbot + its Cloudflare DNS plugin: Let's Encrypt over DNS-01 (no open port).
# nginx: the optional HTTPS proxy in front of Home Assistant.
RUN apk add --no-cache python3 py3-cryptography certbot certbot-dns-cloudflare nginx \
    && certbot --version && nginx -v

WORKDIR /app

# /app: the app and the shared ingress panel (webui.py, web/).
# /etc/s6-overlay: the app runs as an s6-overlay service (no legacy CMD).
COPY rootfs/ /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/ssl-cert-manager/run /etc/s6-overlay/s6-rc.d/ssl-cert-manager/finish \
    && python3 -m compileall -q /app

ENV SSL_CERT_MANAGER_VERSION=${BUILD_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    S6_VERBOSITY=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0

LABEL \
    io.hass.name="SSL Cert Manager" \
    io.hass.description="Get, import and renew TLS certificates for Home Assistant, with an optional HTTPS proxy" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="CWT <cwt@clgx.org>" \
    org.opencontainers.image.title="SSL Cert Manager" \
    org.opencontainers.image.description="TLS certificates (Let's Encrypt, Cloudflare Origin CA, imports) for Home Assistant" \
    org.opencontainers.image.vendor="CWT <cwt@clgx.org>" \
    org.opencontainers.image.authors="CWT <cwt@clgx.org>" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.url="https://github.com/nerowentah/haos-addons" \
    org.opencontainers.image.source="https://github.com/nerowentah/haos-addons" \
    org.opencontainers.image.documentation="https://github.com/nerowentah/haos-addons/tree/main/ssl-cert-manager"
