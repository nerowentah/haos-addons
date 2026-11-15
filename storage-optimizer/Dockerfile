ARG BUILD_VERSION
ARG BUILD_ARCH

# Multi-arch Alpine base (aarch64/amd64). S6-Overlay, bashio and
# ca-certificates are already provided.
FROM ghcr.io/home-assistant/base:3.24@sha256:58422480a299b16b6ea376c2167463206d430619ed02b40cd687c8bd6875519e

ARG BUILD_VERSION
ARG BUILD_ARCH

# Python standard library only: no pip, no extra packages.
# apk upgrade: the security fixes Alpine published after the base image was built.
# tempio (a Go template tool in the base image) is not used by this app.
RUN apk upgrade --no-cache \
    && apk add --no-cache python3 \
    && rm -f /usr/bin/tempio

WORKDIR /app

# /app: the app and the shared ingress panel (webui.py, web/).
# /etc/s6-overlay: the app runs as an s6-overlay service (no legacy CMD).
COPY rootfs/ /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/storage-optimizer/run /etc/s6-overlay/s6-rc.d/storage-optimizer/finish \
    && python3 -m compileall -q /app

ENV STORAGE_OPTIMIZER_VERSION=${BUILD_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    S6_VERBOSITY=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0

LABEL \
    io.hass.name="HA Storage Optimizer" \
    io.hass.description="See where the disk space went and free it safely: recorder, backups, logs and caches" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="nerowentah" \
    org.opencontainers.image.title="HA Storage Optimizer" \
    org.opencontainers.image.description="Disk usage overview and safe clean-up for Home Assistant OS" \
    org.opencontainers.image.vendor="nerowentah" \
    org.opencontainers.image.authors="nerowentah" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.url="https://github.com/nerowentah/haos-addons" \
    org.opencontainers.image.source="https://github.com/nerowentah/haos-addons" \
    org.opencontainers.image.documentation="https://github.com/nerowentah/haos-addons/tree/main/storage-optimizer"
