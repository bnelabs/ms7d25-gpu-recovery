#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_kernel="$(uname -r)"
if [[ "$task_kernel" != 7.0.0-38-generic ]]; then
    echo 'This helper is validated only for kernel 7.0.0-38-generic.' >&2
    exit 1
fi
make -C "/lib/modules/$task_kernel/build" M="$task_root/evo-bail" modules
mkdir -p "$task_root/build"
task_nvcc="${GPU_RECOVERY_NVCC:-/usr/local/cuda-13.4/bin/nvcc}"
"$task_nvcc" -O2 "$task_root/cuda-probe.cu" -o "$task_root/build/cuda-probe"
python3 -m py_compile "$task_root/recover.py" "$task_root/pulse-gpu-perst.py" "$task_root/watch.py"
