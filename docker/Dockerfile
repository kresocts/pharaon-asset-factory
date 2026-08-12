# syntax=docker/dockerfile:1
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04@sha256:da6791294b0b04d7e65d87b7451d6f2390b4d36225ab0701ee7dfec5769829f5

ARG DEBIAN_FRONTEND=noninteractive
ARG APP_UID=10001
ARG APP_GID=10001
ARG HUNYUAN_REPOSITORY=https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git
ARG HUNYUAN_COMMIT=82920d643c0dc2f7bfd7255f45f62d386edfe60c
ARG HUNYUAN_REQUIREMENTS_SHA256=ce8954023db68966a6fb2b80253b24c9da81ae48ee9d96f8f4a8d98b4cead289
ARG PYTORCH_VERSION=2.5.1
ARG TORCHVISION_VERSION=0.20.1
ARG TORCHAUDIO_VERSION=2.5.1
ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu124

LABEL org.opencontainers.image.source="https://github.com/kresocts/pharaon-asset-factory" \
      org.opencontainers.image.title="Pharaon Asset Factory Hunyuan dependency layer" \
      org.opencontainers.image.description="Pinned CUDA, PyTorch, and Hunyuan3D 2.1 dependencies without model weights or native extensions" \
      org.opencontainers.image.hunyuan.repository="https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1" \
      org.opencontainers.image.hunyuan.commit="82920d643c0dc2f7bfd7255f45f62d386edfe60c" \
      org.opencontainers.image.pytorch.version="2.5.1" \
      org.opencontainers.image.purpose="hunyuan-dependencies"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential ca-certificates git libgl1 libglib2.0-0 libgomp1 \
        python3.10 python3.10-venv tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app \
    && python3.10 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir \
        pip==25.1.1 setuptools==80.9.0 wheel==0.45.1 \
    && mkdir -p /app /models /data/input /data/output /workspace \
    && chown -R app:app /app /models /data /workspace /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODEL_CACHE_DIR=/models \
    INPUT_DIR=/data/input \
    OUTPUT_DIR=/data/output \
    WORKSPACE_DIR=/workspace \
    HUNYUAN_SOURCE_DIR=/opt/hunyuan3d \
    HUNYUAN_REVISION_FILE=/opt/hunyuan3d.commit \
    HUNYUAN_COMMIT=82920d643c0dc2f7bfd7255f45f62d386edfe60c

# PyPI fallback supplies nvidia-cudnn-cu12 9.1.0.70, still pinned by torch
# 2.5.1 but no longer listed by the cu124 index's NVIDIA package link.
RUN python -m pip install --no-cache-dir \
        --index-url "${PYTORCH_INDEX_URL}" \
        --extra-index-url https://pypi.org/simple \
        "torch==${PYTORCH_VERSION}" \
        "torchvision==${TORCHVISION_VERSION}" \
        "torchaudio==${TORCHAUDIO_VERSION}" \
    && python -c "import torch, torchvision, torchaudio; assert torch.__version__.split('+')[0] == '${PYTORCH_VERSION}'; assert torchvision.__version__.split('+')[0] == '${TORCHVISION_VERSION}'; assert torchaudio.__version__.split('+')[0] == '${TORCHAUDIO_VERSION}'; assert torch.version.cuda == '12.4'"

RUN git init /opt/hunyuan3d \
    && git -C /opt/hunyuan3d remote add origin "${HUNYUAN_REPOSITORY}" \
    && git -C /opt/hunyuan3d fetch --depth 1 origin "${HUNYUAN_COMMIT}" \
    && git -C /opt/hunyuan3d checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/hunyuan3d rev-parse HEAD)" = "${HUNYUAN_COMMIT}" \
    && echo "${HUNYUAN_COMMIT}" > /opt/hunyuan3d.commit \
    && echo "${HUNYUAN_REQUIREMENTS_SHA256}  /opt/hunyuan3d/requirements.txt" | sha256sum --check --strict \
    && cp /opt/hunyuan3d/requirements.txt /opt/hunyuan3d.requirements.upstream.txt \
    && rm -rf /opt/hunyuan3d/.git

# Blender's official bpy wheel needs these small X11 runtime libraries even for
# an import-only, non-rendering dependency check.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libice6 libsm6 libxcursor1 libxi6 libxinerama1 libxkbcommon0 \
        libxrandr2 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY docker/hunyuan-requirements.lock.txt /opt/hunyuan3d.requirements.lock.txt
RUN DS_BUILD_OPS=0 python -m pip install --no-cache-dir \
        --requirement /opt/hunyuan3d.requirements.lock.txt \
    && (python -m pip check > /tmp/pip-check.txt 2>&1 \
        || (test "$(wc -l < /tmp/pip-check.txt)" -eq 2 \
            && grep -Fx "bpy 4.0.0 is not supported on this platform" /tmp/pip-check.txt \
            && grep -Fx "ninja 1.11.1.1 is not supported on this platform" /tmp/pip-check.txt)) \
    && python -c "import bpy, ninja" \
    && rm -f /tmp/pip-check.txt \
    && python -m pip freeze --all > /opt/hunyuan3d.dependencies.freeze.txt \
    && find /opt/hunyuan3d -type d -name __pycache__ -prune -exec rm -rf '{}' + \
    && find /opt/hunyuan3d -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete \
    && test -z "$(find /opt/hunyuan3d/hy3dpaint/custom_rasterizer /opt/hunyuan3d/hy3dpaint/DifferentiableRenderer -type f \( -name '*.so' -o -name '*.pyd' \) -print -quit)" \
    && chown -R root:root /opt/hunyuan3d /opt/hunyuan3d.commit /opt/hunyuan3d.requirements.upstream.txt /opt/hunyuan3d.requirements.lock.txt /opt/hunyuan3d.dependencies.freeze.txt

COPY --chown=app:app docker/health.py /app/health.py
COPY --chown=app:app docker/dependency_smoke.py /app/dependency_smoke.py
COPY --chown=app:app docker/gpu_smoke.py /app/gpu_smoke.py
COPY --chown=root:root docker/entrypoint.sh /usr/local/bin/pharaon-entrypoint
RUN chmod 0755 /usr/local/bin/pharaon-entrypoint \
    && python /app/dependency_smoke.py \
    && python /app/health.py --json

WORKDIR /app
VOLUME ["/models", "/data/input", "/data/output"]
USER app:app

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/pharaon-entrypoint"]
CMD ["health"]
