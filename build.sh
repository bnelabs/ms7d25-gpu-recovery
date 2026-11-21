#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_kernel="$(uname -r)"
python3 "$task_root/qualify.py" --kernel "$task_kernel" --output "$task_root/build/qualified/$task_kernel"
mkdir -p "$task_root/build"
task_nvcc="${GPU_RECOVERY_NVCC:-/usr/local/cuda-13.4/bin/nvcc}"
"$task_nvcc" -O2 "$task_root/cuda-probe.cu" -o "$task_root/build/cuda-probe"
python3 -m py_compile "$task_root/recover.py" "$task_root/pulse-gpu-perst.py" "$task_root/watch.py" "$task_root/qualify.py"
