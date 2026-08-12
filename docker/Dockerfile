# syntax=docker/dockerfile:1
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04@sha256:da6791294b0b04d7e65d87b7451d6f2390b4d36225ab0701ee7dfec5769829f5

LABEL org.opencontainers.image.source="https://github.com/kresocts/pharaon-asset-factory" \
      org.opencontainers.image.title="Pharaon Asset Factory GPU base" \
      org.opencontainers.image.description="Provider-neutral CUDA and Python base for the future asset worker"

ARG DEBIAN_FRONTEND=noninteractive
ARG APP_UID=10001
ARG APP_GID=10001

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        python3.10 \
        python3.10-venv \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app \
    && python3.10 -m venv /opt/venv \
    && mkdir -p /app /models /data/input /data/output /workspace \
    && chown -R app:app /app /models /data /workspace /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_CACHE_DIR=/models \
    INPUT_DIR=/data/input \
    OUTPUT_DIR=/data/output \
    WORKSPACE_DIR=/workspace

COPY --chown=app:app docker/health.py /app/health.py
COPY --chown=root:root docker/entrypoint.sh /usr/local/bin/pharaon-entrypoint
RUN chmod 0755 /usr/local/bin/pharaon-entrypoint

WORKDIR /app
VOLUME ["/models", "/data/input", "/data/output"]
USER app:app

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/pharaon-entrypoint"]
CMD ["health"]
