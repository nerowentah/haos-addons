ARG BUILD_VERSION
ARG BUILD_ARCH

# Multi-arch Alpine base (aarch64/amd64) since docker-base 2026.03.1.
# S6-Overlay, bashio and ca-certificates are already provided.
FROM ghcr.io/home-assistant/base:3.24

# The base image ships Bash but not always Python; python3 only needs the
# stdlib for this add-on, so no pip packages are required.
RUN apk add --no-cache \
    python3

WORKDIR /app

COPY rootfs/run.py /app/run.py

RUN chmod a+x /app/run.py

LABEL \
    io.hass.name="LG webOS Dev Session Auto Renew" \
    io.hass.description="Periodically renews LG webOS Developer Mode sessions via the LG Developer API" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="CWT <cwt@clgx.org>"

CMD ["python3", "/app/run.py"]