#!/usr/bin/env bash
set -euo pipefail
STACK=/home/komedi/llama-stack
PREFIX=/home/komedi/.local/opt/llama.cpp
if [[ -f "$STACK/router.env" ]]; then
    set -a
    source "$STACK/router.env"
    set +a
fi
export CUDA_VISIBLE_DEVICES=GPU-8a9c05b3-fea1-4d89-5113-b56acbb1eba9
# Do not let a GPU-less router create CPU-only model workers.
if ! timeout 10 /usr/bin/nvidia-smi -i 0000:01:00.0 --query-gpu=uuid --format=csv,noheader | grep -Fxq "$CUDA_VISIBLE_DEVICES"; then
    echo 'Required GPU unavailable; refusing to start inference' >&2
    exit 1
fi
if ! timeout 30 /usr/local/lib/gpu-recovery/cuda-probe; then
    echo 'CUDA verification failed; refusing to start inference' >&2
    exit 1
fi
export OPENBLAS_NUM_THREADS=1
export LD_LIBRARY_PATH="$PREFIX/lib:/usr/local/cuda-13.4/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mkdir -p "$STACK/workspace"
cd "$STACK/workspace"
args=(
    --device CUDA0
    --host "${HOST:-127.0.0.1}" --port "${PORT:-8080}"
    --models-dir /home/komedi/models --models-preset "$STACK/models.ini"
    --models-max "${MODELS_MAX:-1}" --models-autoload --tools all
    --ui-mcp-proxy
    --cors-origins "${CORS_ORIGINS:-http://192.168.68.57:8080}"
)
if [[ -n ${MCP_CONFIG:-} ]]; then
    args+=(--mcp-servers-config "$MCP_CONFIG")
fi
exec "$PREFIX/bin/llama-server" "${args[@]}" "$@"
