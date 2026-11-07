ARG BUILD_VERSION
ARG BUILD_ARCH

# Multi-arch Alpine base (aarch64/amd64) since docker-base 2026.03.1.
# S6-Overlay, bashio and ca-certificates are already provided.
FROM ghcr.io/home-assistant/base:3.24

ARG BUILD_VERSION
ARG BUILD_ARCH

# The base image ships Bash but not always Python; python3 only needs the
# stdlib for this add-on, so no pip packages are required.
RUN apk add --no-cache \
    python3

WORKDIR /app

# /app: the app and the shared ingress panel (webui.py, web/).
# /etc/s6-overlay: the app runs as an s6-overlay service (no legacy CMD).
COPY rootfs/ /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/lg-webos-devmode-mgmt/run /etc/s6-overlay/s6-rc.d/lg-webos-devmode-mgmt/finish \
    && python3 -m compileall -q /app

ENV LG_RENEW_VERSION=${BUILD_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    S6_VERBOSITY=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0

LABEL \
    io.hass.name="LG webOS DevMode Mgmt" \
    io.hass.description="Periodically renews LG webOS Developer Mode sessions via the LG Developer API" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="CWT <cwt@clgx.org>"
