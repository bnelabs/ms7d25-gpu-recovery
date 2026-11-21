#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$EUID" != 0 ]]; then
    echo 'Run sudo ./install.sh after ./build.sh.' >&2
    exit 1
fi
python3 - <<'PY'
import pathlib
if pathlib.Path('/sys/class/dmi/id/board_name').read_text().strip() != 'PRO Z690-A WIFI DDR4(MS-7D25)':raise SystemExit('Board mismatch')
PY
test -x "$task_root/build/cuda-probe"
install -d -o root -g root -m 0755 /usr/local/lib/gpu-recovery
install -d -o root -g root -m 0755 /usr/local/lib/gpu-recovery/evo-bail
install -o root -g root -m 0755 "$task_root/recover.py" /usr/local/sbin/gpu-recover-ms7d25
install -o root -g root -m 0644 "$task_root/pulse-gpu-perst.py" /usr/local/lib/gpu-recovery/pulse-perst.py
install -o root -g root -m 0644 "$task_root/evo-bail/evo_bail.c" /usr/local/lib/gpu-recovery/evo-bail/evo_bail.c
install -o root -g root -m 0644 "$task_root/evo-bail/Makefile" /usr/local/lib/gpu-recovery/evo-bail/Makefile
install -o root -g root -m 0644 "$task_root/qualify.py" "$task_root/qualified-driver.json" /usr/local/lib/gpu-recovery/
python3 /usr/local/lib/gpu-recovery/qualify.py
install -o root -g root -m 0644 "/var/lib/gpu-recovery/qualified/$(uname -r)/manifest.json" /usr/local/lib/gpu-recovery/manifest.json
install -o root -g root -m 0644 "$task_root/watch.py" /usr/local/lib/gpu-recovery/watch.py
install -o root -g root -m 0755 "$task_root/build/cuda-probe" /usr/local/lib/gpu-recovery/cuda-probe
install -o root -g root -m 0644 "$task_root/watch.service" /etc/systemd/system/gpu-xid79-recovery.service
for task_service in nvidia-persistenced.service nvidia-cdi-refresh.service; do
    install -d -o root -g root -m 0755 "/etc/systemd/system/$task_service.d"
    printf '[Unit]\nConditionPathExists=!/run/gpu-recovery-active\n' > "/etc/systemd/system/$task_service.d/50-gpu-recovery.conf"
done
systemctl daemon-reload
echo 'Installed with automatic kernel qualification. Run sudo gpu-recover-ms7d25 --check, then enable the watcher explicitly.'
