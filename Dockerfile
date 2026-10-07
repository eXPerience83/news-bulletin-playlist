# syntax=docker/dockerfile:1
FROM python:3.14.8-slim-trixie@sha256:f85c5697265c178cc6887276c55fe16cf3d14ca35c3df6a5eab3b360534a55d2 AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.14.8-slim-trixie@sha256:f85c5697265c178cc6887276c55fe16cf3d14ca35c3df6a5eab3b360534a55d2 AS runtime

ARG SOURCE_REVISION=dev

LABEL org.opencontainers.image.source="https://github.com/eXPerience83/news-bulletin-playlist"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    NEWS_PLAYLIST_DATA_DIR=/data \
    NEWS_PLAYLIST_BUILD_REVISION=${SOURCE_REVISION}

COPY --from=builder /wheels /wheels
COPY assets/covers/spotify /opt/news-bulletin-playlist/covers
RUN python -m pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels \
    && mkdir -p /data \
    && chown 10001:10001 /data

USER 10001:10001
WORKDIR /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --start-interval=1s --retries=3 \
    CMD ["news-playlist", "healthcheck"]

ENTRYPOINT ["news-playlist"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
