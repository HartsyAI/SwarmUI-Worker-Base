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
ARG SWARMUI_REF=b5d28088f20b0703155d9bb57fa41a39bb62c8fe

# ── Stage 1: build SwarmUI (and the HartsyInference extension) with the .NET SDK ──────────────────
FROM mcr.microsoft.com/dotnet/sdk:8.0-noble AS swarm-build

ARG BACKEND=comfyui
ARG SWARMUI_REPO=https://github.com/mcmonkeyprojects/SwarmUI
ARG SWARMUI_REF
ARG HARTSYINFERENCE_REPO=https://github.com/HartsyAI/SwarmUI-HartsyInference-Backend
# The HartsyInference extension tracks its main branch, which uses the newest published engine. CI passes the resolved
# commit and engine version, so the build cache never serves an older engine.
ARG HARTSYINFERENCE_REF=main
ARG HARTSYINFERENCE_ENGINE=latest

ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
COPY docker/build_swarmui.sh /build/build_swarmui.sh
RUN BACKEND="$BACKEND" SWARMUI_REPO="$SWARMUI_REPO" SWARMUI_REF="$SWARMUI_REF" \
    HARTSYINFERENCE_REPO="$HARTSYINFERENCE_REPO" HARTSYINFERENCE_REF="$HARTSYINFERENCE_REF" \
    HARTSYINFERENCE_ENGINE="$HARTSYINFERENCE_ENGINE" \
    bash /build/build_swarmui.sh

# ── Stage 2: runtime ─────────────────────────────────────────────────────────────────────────────
FROM ${CUDA_IMAGE} AS runtime

ARG BACKEND=comfyui
ARG COMFYUI_REPO=https://github.com/comfyanonymous/ComfyUI
ARG COMFYUI_REF=4ef23c34d950eecc37040a21ee1741a49d2e44b1
# SwarmUI's installer uses the newest CUDA wheels; cu128 runs on the far wider range of host drivers
# that cloud GPUs actually have (driver 570+).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
ARG VERSION=dev
ARG REVISION=unknown
ARG SWARMUI_REF

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
    PYTHONPATH=/opt/worker/lib \
    PATH=/opt/worker/venv/bin:$PATH \
    HOME=/opt/swarmui/home

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      aspnetcore-runtime-8.0 ca-certificates curl git ffmpeg libgl1 libglib2.0-0 \
      python3 python3-venv python3-pip tini \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*' \
 && useradd --no-create-home --home-dir /opt/swarmui/home --uid 10001 --shell /usr/sbin/nologin swarm

COPY --from=swarm-build --chown=swarm:swarm /opt/swarmui /opt/swarmui

# The worker supervisor: its own venv, never shared with ComfyUI's.
COPY --chown=swarm:swarm src/swarmui_worker /opt/worker/lib/swarmui_worker
RUN python3 -m venv /opt/worker/venv \
 && /opt/worker/venv/bin/pip install --no-cache-dir "aiohttp>=3.10,<4" "yarl>=1.10" \
 && chown -R swarm:swarm /opt/worker

COPY --chown=swarm:swarm docker/install_comfyui.sh docker/backends.py docker/prewarm.py /build/
USER swarm
RUN mkdir -p /opt/swarmui/home /opt/swarmui/Models

RUN if [ "$BACKEND" = "comfyui" ]; then \
      COMFYUI_REPO="$COMFYUI_REPO" COMFYUI_REF="$COMFYUI_REF" TORCH_INDEX_URL="$TORCH_INDEX_URL" \
      bash /build/install_comfyui.sh; \
    fi

# First boot happens here, on CPU, so SwarmUI installs what it wants and proves the image works.
RUN python3 /build/backends.py "$BACKEND" --prewarm \
 && /opt/worker/venv/bin/python /build/prewarm.py \
 && python3 /build/backends.py "$BACKEND"

WORKDIR /opt/swarmui
# The gateway is the only public port. SwarmUI itself listens on 127.0.0.1:7810.
EXPOSE 7801

HEALTHCHECK --interval=30s --timeout=10s --start-period=15m --retries=3 \
  CMD curl -fsS -o /dev/null -X POST -H 'Content-Type: application/json' -d '{}' \
      "http://127.0.0.1:${SWARMUI_INTERNAL_PORT:-7810}/API/GetNewSession" || exit 1

# Standalone mode (rented pods/instances; needs SWARMUI_WORKER_TOKEN). Provider images override this.
# tini is PID 1: it forwards signals and reaps orphaned backend processes.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/worker/venv/bin/python", "-m", "swarmui_worker"]
