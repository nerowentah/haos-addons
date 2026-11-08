ARG BUILD_VERSION
ARG BUILD_ARCH

# Multi-arch Alpine base (aarch64/amd64). S6-Overlay, bashio and
# ca-certificates are already provided.
FROM ghcr.io/home-assistant/base:3.24

ARG BUILD_VERSION
ARG BUILD_ARCH

# Alpine packages only (no pip): the site client is stdlib; paho-mqtt for MQTT discovery.
RUN apk add --no-cache \
    python3 \
    py3-paho-mqtt

WORKDIR /app

# /app: the app and the shared ingress panel (webui.py, web/).
# /etc/s6-overlay: the app runs as an s6-overlay service (no legacy CMD).
COPY rootfs/ /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/ranhill-saj-bill-checker/run /etc/s6-overlay/s6-rc.d/ranhill-saj-bill-checker/finish \
    && python3 -m compileall -q /app

ENV RANHILL_SAJ_VERSION=${BUILD_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    S6_VERBOSITY=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0

LABEL \
    io.hass.name="Ranhill SAJ Bill Checker" \
    io.hass.description="Amount due on Ranhill SAJ (Johor) water accounts, via the public Quick Payment page" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="CWT <cwt@clgx.org>"
