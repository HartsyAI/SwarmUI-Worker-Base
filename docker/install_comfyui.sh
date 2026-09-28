#!/usr/bin/env bash
# Installs ComfyUI the way SwarmUI's own launchtools/comfy-install-linux.sh does (dlbackend/ComfyUI,
# a venv beside it, torch, then ComfyUI's requirements), with two deliberate differences for a cloud
# image: ComfyUI is pinned to a commit, and the torch wheel index is a build argument. SwarmUI's
# script uses the newest CUDA wheels, which need a newer host driver than many cloud GPUs run.
set -euo pipefail

: "${COMFYUI_REPO:?}" "${COMFYUI_REF:?}" "${TORCH_INDEX_URL:?}"

mkdir -p /opt/swarmui/dlbackend
cd /opt/swarmui/dlbackend
git init -q ComfyUI
git -C ComfyUI remote add origin "$COMFYUI_REPO"
git -C ComfyUI fetch -q --depth 1 origin "$COMFYUI_REF"
git -C ComfyUI checkout -q FETCH_HEAD

cd ComfyUI
python3 -s -m venv venv
./venv/bin/python -m pip install --no-cache-dir --upgrade pip
./venv/bin/python -s -m pip install --no-cache-dir torch torchvision --index-url "$TORCH_INDEX_URL"
./venv/bin/python -s -m pip install --no-cache-dir -r requirements.txt
