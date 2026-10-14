#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$EUID" != 0 ]]; then
    echo 'Run sudo ./install.sh after ./build.sh.' >&2
    exit 1
fi
python3 - "$task_root/manifest.json" <<'PY'
import hashlib,json,pathlib,subprocess,sys,os
m=json.loads(pathlib.Path(sys.argv[1]).read_text())
if os.uname().release != m['kernel']:raise SystemExit('Kernel mismatch')
if pathlib.Path('/sys/class/dmi/id/board_name').read_text().strip() != 'PRO Z690-A WIFI DDR4(MS-7D25)':raise SystemExit('Board mismatch')
p=subprocess.check_output(['/usr/sbin/modinfo','-n','nvidia_modeset'],text=True).strip()
if hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() != m['nvidia_modeset_sha256']:raise SystemExit('NVIDIA module mismatch; revalidation required')
PY
test -f "$task_root/evo-bail/evo_bail.ko"
test -x "$task_root/build/cuda-probe"
install -d -o root -g root -m 0755 /usr/local/lib/gpu-recovery
install -o root -g root -m 0755 "$task_root/recover.py" /usr/local/sbin/gpu-recover-ms7d25
install -o root -g root -m 0644 "$task_root/pulse-gpu-perst.py" /usr/local/lib/gpu-recovery/pulse-perst.py
install -o root -g root -m 0644 "$task_root/evo-bail/evo_bail.ko" /usr/local/lib/gpu-recovery/evo_bail.ko
install -o root -g root -m 0644 "$task_root/manifest.json" /usr/local/lib/gpu-recovery/manifest.json
install -o root -g root -m 0644 "$task_root/watch.py" /usr/local/lib/gpu-recovery/watch.py
install -o root -g root -m 0755 "$task_root/build/cuda-probe" /usr/local/lib/gpu-recovery/cuda-probe
install -o root -g root -m 0644 "$task_root/watch.service" /etc/systemd/system/gpu-xid79-recovery.service
systemctl daemon-reload
echo 'Installed. Run sudo gpu-recover-ms7d25 --check, then enable the watcher explicitly.'
