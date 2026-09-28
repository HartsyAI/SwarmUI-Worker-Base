# syntax=docker/dockerfile:1.7
#
# Provider-neutral SwarmUI worker image by Hartsy.
#
# Build one image per backend:
#   docker build --build-arg BACKEND=comfyui         -t kalebbroo/swarmui-worker-base:dev-comfyui .
#   docker build --build-arg BACKEND=hartsyinference -t kalebbroo/swarmui-worker-base:dev-hartsyinference .
#
# Provider images (RunPod, Vast.ai) build FROM this one and add only their provider adapter.

ARG CUDA_IMAGE=nvidia/cuda:12.8.1-runtime-ubuntu24.04

# ── Stage 1: build SwarmUI (and the HartsyInference extension) with the .NET SDK ──────────────────
FROM mcr.microsoft.com/dotnet/sdk:8.0-noble AS swarm-build

ARG BACKEND=comfyui
ARG SWARMUI_REPO=https://github.com/mcmonkeyprojects/SwarmUI
ARG SWARMUI_REF=e2c35f354dd9411b64037e18abf319927260609d
ARG HARTSYINFERENCE_REPO=https://github.com/HartsyAI/SwarmUI-HartsyInference-Backend
ARG HARTSYINFERENCE_REF=2bba9053e8789a0099d91098c1d2bcc90df6db6f

ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
COPY docker/build_swarmui.sh /build/build_swarmui.sh
RUN BACKEND="$BACKEND" SWARMUI_REPO="$SWARMUI_REPO" SWARMUI_REF="$SWARMUI_REF" \
    HARTSYINFERENCE_REPO="$HARTSYINFERENCE_REPO" HARTSYINFERENCE_REF="$HARTSYINFERENCE_REF" \
    bash /build/build_swarmui.sh

# ── Stage 2: runtime ─────────────────────────────────────────────────────────────────────────────
FROM ${CUDA_IMAGE} AS runtime

ARG BACKEND=comfyui
ARG COMFYUI_REPO=https://github.com/comfyanonymous/ComfyUI
ARG COMFYUI_REF=4ef23c34d950eecc37040a21ee1741a49d2e44b1
# SwarmUI's installer uses the newest CUDA wheels; cu128 runs on the far wider range of host drivers
# that cloud GPUs actually have (driver 570+).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
# Optional: bake one model into the image (for providers with no persistent volume, e.g. Vast.ai
# Serverless). A SHA-256 is required whenever a URL is given.
ARG BAKE_MODEL_URL=""
ARG BAKE_MODEL_SHA256=""
ARG BAKE_MODEL_SUBDIR=Stable-Diffusion
ARG VERSION=dev
ARG REVISION=unknown
ARG SWARMUI_REF=e2c35f354dd9411b64037e18abf319927260609d

LABEL org.opencontainers.image.title="swarmui-worker-base" \
      org.opencontainers.image.description="Provider-neutral SwarmUI worker by Hartsy (backend: ${BACKEND})" \
      org.opencontainers.image.vendor="Hartsy" \
      org.opencontainers.image.url="https://hartsy.ai" \
      org.opencontainers.image.source="https://github.com/HartsyAI/SwarmUI-Worker-Base" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      ai.hartsy.swarmui.ref="${SWARMUI_REF}" \
      ai.hartsy.swarmui.backend="${BACKEND}"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    SWARMUI_DIR=/opt/swarmui \
    SWARMUI_BACKEND=${BACKEND} \
    PYTHONPATH=/opt/worker/lib \
    PATH=/opt/worker/venv/bin:$PATH \
    HOME=/opt/swarmui/home

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      aspnetcore-runtime-8.0 ca-certificates curl git ffmpeg libgl1 libglib2.0-0 \
      python3 python3-venv python3-pip \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*' \
 && useradd --create-home --home-dir /home/swarm --uid 10001 --shell /usr/sbin/nologin swarm

COPY --from=swarm-build --chown=swarm:swarm /opt/swarmui /opt/swarmui

# The worker supervisor: its own venv, never shared with ComfyUI's.
COPY --chown=swarm:swarm pyproject.toml /opt/worker/pyproject.toml
COPY --chown=swarm:swarm src/swarmui_worker /opt/worker/lib/swarmui_worker
RUN python3 -m venv /opt/worker/venv \
 && /opt/worker/venv/bin/pip install --no-cache-dir "aiohttp>=3.10,<4" \
 && chown -R swarm:swarm /opt/worker

COPY --chown=swarm:swarm docker/install_comfyui.sh docker/backends.py docker/prewarm.py /build/
USER swarm
RUN mkdir -p /opt/swarmui/home /opt/swarmui/Models/${BAKE_MODEL_SUBDIR}

RUN if [ "$BACKEND" = "comfyui" ]; then \
      COMFYUI_REPO="$COMFYUI_REPO" COMFYUI_REF="$COMFYUI_REF" TORCH_INDEX_URL="$TORCH_INDEX_URL" \
      bash /build/install_comfyui.sh; \
    fi

# First boot happens here, on CPU, so SwarmUI installs what it wants and proves the image works.
RUN python3 /build/backends.py "$BACKEND" --prewarm \
 && /opt/worker/venv/bin/python /build/prewarm.py \
 && python3 /build/backends.py "$BACKEND"

RUN if [ -n "$BAKE_MODEL_URL" ]; then \
      if [ -z "$BAKE_MODEL_SHA256" ]; then echo "BAKE_MODEL_SHA256 is required with BAKE_MODEL_URL" >&2; exit 1; fi; \
      name="$(basename "${BAKE_MODEL_URL%%\?*}")"; \
      dest="/opt/swarmui/Models/${BAKE_MODEL_SUBDIR}/${name}"; \
      curl -fL --retry 5 --retry-delay 5 -o "$dest" "$BAKE_MODEL_URL" \
      && echo "${BAKE_MODEL_SHA256}  ${dest}" | sha256sum -c -; \
    fi

WORKDIR /opt/swarmui
# The gateway is the only public port. SwarmUI itself listens on 127.0.0.1:7810.
EXPOSE 7801

HEALTHCHECK --interval=30s --timeout=10s --start-period=15m --retries=3 \
  CMD curl -fsS -o /dev/null -X POST -H 'Content-Type: application/json' -d '{}' \
      "http://127.0.0.1:${SWARMUI_INTERNAL_PORT:-7810}/API/GetNewSession" || exit 1

# Standalone mode (rented pods/instances; needs SWARMUI_WORKER_TOKEN). Provider images override this.
ENTRYPOINT ["/opt/worker/venv/bin/python", "-m", "swarmui_worker"]
