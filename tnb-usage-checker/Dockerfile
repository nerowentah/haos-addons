ARG BUILD_VERSION
ARG BUILD_ARCH

# Multi-arch Alpine base (aarch64/amd64). S6-Overlay, bashio and
# ca-certificates are already provided.
FROM ghcr.io/home-assistant/base:3.24

ARG BUILD_VERSION
ARG BUILD_ARCH

# Alpine packages only (no pip): MQTT discovery and the HA websocket API.
RUN apk add --no-cache \
    python3 \
    py3-paho-mqtt \
    py3-websocket-client

WORKDIR /app

# /app: the app and the shared ingress panel (webui.py, web/).
# /etc/s6-overlay: the app runs as an s6-overlay service (no legacy CMD).
COPY rootfs/ /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/tnb-usage-checker/run /etc/s6-overlay/s6-rc.d/tnb-usage-checker/finish

ENV POWER_TNB_VERSION=${BUILD_VERSION} \
    PYTHONUNBUFFERED=1 \
    S6_VERBOSITY=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0

LABEL \
    io.hass.name="TNB Usage Checker" \
    io.hass.description="Multi-account TNB (Malaysia) myTNB usage import and live bill estimates" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="CWT <cwt@clgx.org>"
