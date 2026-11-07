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

COPY rootfs/app/ /app/

ENV POWER_TNB_VERSION=${BUILD_VERSION} \
    PYTHONUNBUFFERED=1

LABEL \
    io.hass.name="TNB Usage Checker" \
    io.hass.description="Multi-account TNB (Malaysia) myTNB usage import and live bill estimates" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="CWT <cwt@clgx.org>"

CMD ["python3", "/app/main.py"]
