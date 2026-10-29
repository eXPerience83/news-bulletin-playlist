# syntax=docker/dockerfile:1
FROM python:3.15.2-slim-trixie@sha256:c4158543ecb725b99e57d129dfd43d761d4c43e4577b5e23326aabb223cf7f4e AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Wheel every runtime dependency in the builder; runtime stays offline.
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.15.2-slim-trixie@sha256:c4158543ecb725b99e57d129dfd43d761d4c43e4577b5e23326aabb223cf7f4e AS runtime

ARG SOURCE_REVISION=dev

LABEL org.opencontainers.image.source="https://github.com/eXPerience83/news-bulletin-playlist" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="Roberto Ribes"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    NEWS_PLAYLIST_DATA_DIR=/data \
    NEWS_PLAYLIST_BUILD_REVISION=${SOURCE_REVISION}

COPY --from=builder /wheels /wheels
COPY LICENSE /usr/share/licenses/news-bulletin-playlist/LICENSE
COPY assets/covers/spotify /opt/news-bulletin-playlist/covers
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels /wheels/news_bulletin_playlist*.whl \
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
