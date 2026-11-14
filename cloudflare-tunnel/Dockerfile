ARG BUILD_VERSION
ARG BUILD_ARCH

# Multi-arch Alpine base (aarch64/amd64). S6-Overlay, bashio and
# ca-certificates are already provided.
FROM ghcr.io/home-assistant/base:3.24@sha256:58422480a299b16b6ea376c2167463206d430619ed02b40cd687c8bd6875519e

ARG BUILD_VERSION
ARG BUILD_ARCH

# cloudflared is downloaded from its GitHub release and verified against the
# SHA-256 digests GitHub publishes for the release assets (see DOCS.md).
# Pinned to >= 2026.10.2: Cloudflare's advisory on the compromised eMudhra
# intermediate CA asks every connector to run 2026.10.2 or later.
ARG CLOUDFLARED_VERSION=2026.11.0
ARG CLOUDFLARED_AMD64_SHA256=51f7c68b57ed7269164075d7e789efce5b59355982f06392deeb037d982a51fd
ARG CLOUDFLARED_ARM64_SHA256=abe54a006acf9faf2d19ee9d953cb0d0804e92f59c5382a162e57b84d086f56f

RUN apk add --no-cache python3

WORKDIR /app

# Install cloudflared with SHA-256 verification (fails the build on any mismatch).
RUN case "${BUILD_ARCH}" in \
      aarch64) cloudflared_arch="arm64";  sha="${CLOUDFLARED_ARM64_SHA256}";; \
      *)       cloudflared_arch="amd64";  sha="${CLOUDFLARED_AMD64_SHA256}";; \
    esac && \
    wget -q -O /usr/bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${cloudflared_arch}" && \
    echo "${sha}  /usr/bin/cloudflared" | sha256sum -c - && \
    chmod a+x /usr/bin/cloudflared && /usr/bin/cloudflared --version

# /app: the app and the shared ingress panel (webui.py, web/).
# /etc/s6-overlay: the app runs as an s6-overlay service (no legacy CMD).
COPY rootfs/ /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/cloudflare-tunnel/run /etc/s6-overlay/s6-rc.d/cloudflare-tunnel/finish \
    && python3 -m compileall -q /app

ENV CF_TUNNEL_VERSION=${BUILD_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    S6_VERBOSITY=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0

LABEL \
    io.hass.name="Cloudflare Tunnel" \
    io.hass.description="Connect Home Assistant to the Internet through a Cloudflare Tunnel without opening any port" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="CWT <cwt@clgx.org>" \
    org.opencontainers.image.title="Cloudflare Tunnel" \
    org.opencontainers.image.description="Cloudflare Tunnel connector for Home Assistant, derived from homeassistant-apps/app-cloudflared (MIT)" \
    org.opencontainers.image.vendor="CWT <cwt@clgx.org>" \
    org.opencontainers.image.authors="CWT <cwt@clgx.org>" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.url="https://github.com/nerowentah/haos-addons" \
    org.opencontainers.image.source="https://github.com/nerowentah/haos-addons" \
    org.opencontainers.image.documentation="https://github.com/nerowentah/haos-addons/tree/main/cloudflare-tunnel"
