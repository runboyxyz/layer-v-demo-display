FROM python:3.12-slim-bookworm

ARG BUILD_ARCH
ARG BUILD_VERSION=0.1.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    APP_VERSION=${BUILD_VERSION}

LABEL \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.source="https://github.com/runboyxyz/layer-v-demo-display" \
    org.opencontainers.image.version="${BUILD_VERSION}"

WORKDIR /app

COPY app ./app
COPY run.sh /run.sh

RUN useradd --uid 2200 --user-group --create-home --shell /usr/sbin/nologin demo-display \
    && chmod 0755 /run.sh \
    && chown -R demo-display:demo-display /app

USER demo-display
EXPOSE 8099
STOPSIGNAL SIGTERM
CMD ["/run.sh"]
