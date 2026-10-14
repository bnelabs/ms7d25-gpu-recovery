#!/usr/bin/python3
"""MS-7D25 / GA102 recovery using the verified GPP_E2 PERST gate.
Never reboots. Kernel/driver-specific teardown assistance fails closed.
"""
import argparse, fcntl, hashlib, json, os, pathlib, re, subprocess, sys, time

LIB = pathlib.Path('/usr/local/lib/gpu-recovery')
GPU = '0000:01:00.0'
BRIDGE = '0000:00:01.0'
PCI = pathlib.Path('/sys/bus/pci/devices')
ENV = dict(os.environ, XDG_RUNTIME_DIR='/run/user/1000',
           DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus')
UNITS = ('deepseek-harness.service', 'llama-server.service')

def log(s):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + s
    print(line, flush=True)
    with open('/var/log/gpu-recovery.log', 'a') as f: f.write(line + '\n')

def run(args, timeout=20, check=True):
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env=ENV, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        raise RuntimeError(f'Timeout in {args[0]}; do not repeat a blocked kernel call')
    if check and p.returncode: raise RuntimeError(f'{args}: {out.strip()}')
    return p.returncode, out.strip()

def userctl(*args):
    return run(['/usr/sbin/runuser','-u','komedi','--','/usr/bin/env',
                'XDG_RUNTIME_DIR=/run/user/1000',
                'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
                '/usr/bin/systemctl','--user',*args], 30, False)

def reg(device, field):
    return run(['/usr/bin/setpci','-s',device,field])[1]

def modules():
    return {l.split()[0] for l in pathlib.Path('/proc/modules').read_text().splitlines()}

def pulse():
    log('Pulsing the GPU-only GPP_E2 PERST gate')
    run(['/usr/bin/python3',str(LIB/'pulse-perst.py')], 8)
    for _ in range(30):
        if reg(GPU,'VENDOR_ID') == '10de': return
        time.sleep(.1)
    raise RuntimeError('GPU did not become reachable after PERST')

def restore_pci(resources, resize):
    for dev, rows in resources.items():
        reg(dev,'COMMAND=0000')
        if dev == GPU and resize:
            # GA102 physical ReBAR capability: BAR1 control at 0xbc0.
            reg(dev,'bc0.l=00000f00:00003f00')
        n = 0
        while n < 6:
            start, end, flags = rows[n]
            if flags:
                reg(dev,f'{0x10+n*4:x}.l={(start & 0xffffffff) | (flags & 15):08x}')
                if flags & 4 and not flags & 1:
                    reg(dev,f'{0x10+(n+1)*4:x}.l={start >> 32:08x}')
                    n += 1
            n += 1
        reg(dev,'COMMAND=0002') # Decode memory; fresh driver enables DMA.

def unload():
    names = [n for n in ('nvidia_uvm','nvidia_drm','nvidia_modeset','nvidia') if n in modules()]
    if not names: return
    log('Unloading old NVIDIA state')
    # A direct Popen makes this PID the actual module-unloading task.
    output = open('/var/log/gpu-recovery-unload.log','a')
    p = subprocess.Popen(['/usr/sbin/modprobe','-r',*names], stdout=output,
                         stderr=subprocess.STDOUT, start_new_session=True)
    probe = False
    deadline = time.monotonic() + 75
    try:
        for _ in range(30):
            if p.poll() is not None: break
            time.sleep(.1)
        while p.poll() is None and time.monotonic() < deadline:
            stack = pathlib.Path(f'/proc/{p.pid}/stack').read_text()
            if not probe and ('nvEvoMakeRoom' in stack or 'nvkms_yield' in stack or 'EvoCheckNotifier' in stack):
                if os.uname().release != '7.0.0-38-generic':
                    raise RuntimeError('Teardown helper unavailable for this kernel')
                manifest = json.loads((LIB/'manifest.json').read_text())
                module_path = run(['/usr/sbin/modinfo','-n','nvidia_modeset'])[1]
                digest = hashlib.sha256(pathlib.Path(module_path).read_bytes()).hexdigest()
                if digest != manifest['nvidia_modeset_sha256']:
                    raise RuntimeError('NVIDIA binary changed; refusing offset-based rescue')
                run(['/usr/sbin/insmod',str(LIB/'evo_bail.ko'),f'target_pid={p.pid}'])
                probe = True
                log(f'Enabled guarded teardown assistance exclusively for PID {p.pid}')
            if probe:
                params = pathlib.Path('/sys/module/evo_bail/parameters')
                # Each arm permits only one validated queue/notifier exit.
                (params/'armed').write_text('1')
                (params/'notifier_armed').write_text('1')
            time.sleep(.1)
        if p.poll() is None:
            os.killpg(p.pid,9)
            raise RuntimeError('Driver teardown still blocked; no reboot issued')
        if p.returncode or any(n in modules() for n in names):
            raise RuntimeError('Module removal failed; PCI state will not be changed again')
    finally:
        if probe:
            params = pathlib.Path('/sys/module/evo_bail/parameters')
            (params/'armed').write_text('0');(params/'notifier_armed').write_text('0')
            run(['/usr/sbin/rmmod','evo_bail'])
        output.close()

def main(check_only=False):
    if os.geteuid(): raise RuntimeError('Run as root')
    lock = open('/run/gpu-recovery.lock','w')
    fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    if pathlib.Path('/sys/class/dmi/id/board_name').read_text().strip() != 'PRO Z690-A WIFI DDR4(MS-7D25)':
        raise RuntimeError('Motherboard mismatch')
    if (PCI/GPU).resolve().parent.name != BRIDGE:
        raise RuntimeError('GPU topology changed')
    children = {p.name for p in (PCI/BRIDGE).iterdir()
                if re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]',p.name)}
    if children != {'0000:01:00.0','0000:01:00.1'}:
        raise RuntimeError(f'Reset scope changed: {children}')
    if (PCI/GPU/'device').read_text().strip() != '0x2204':
        raise RuntimeError('GPU model mismatch')
    if 'evo_bail' in modules(): raise RuntimeError('An existing rescue probe must be removed first')
    manifest = json.loads((LIB/'manifest.json').read_text())
    if os.uname().release != manifest['kernel']:
        raise RuntimeError('Kernel changed; this version-specific recovery requires revalidation')
    module_path = run(['/usr/sbin/modinfo','-n','nvidia_modeset'])[1]
    if hashlib.sha256(pathlib.Path(module_path).read_bytes()).hexdigest() != manifest['nvidia_modeset_sha256']:
        raise RuntimeError('NVIDIA module changed; refusing recovery before any mutation')
    resources = {}
    for dev in (GPU,'0000:01:00.1'):
        resources[dev] = [[int(v,16) for v in l.split()] for l in (PCI/dev/'resource').read_text().splitlines()]
    if resources[GPU][1][1] - resources[GPU][1][0] + 1 != 32 * 1024**3:
        raise RuntimeError('Unexpected GPU BAR1 allocation')
    if check_only:
        log('Read-only compatibility checks passed: board, topology, kernel, NVIDIA binary, BAR allocation')
        return
    units = [u for u in UNITS if userctl('is-active','--quiet',u)[0] == 0]
    persist = run(['/usr/bin/systemctl','is-active','--quiet','nvidia-persistenced.service'],check=False)[0] == 0
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    success = False
    try:
        if units:
            rc,out = userctl('stop',*units)
            if rc: raise RuntimeError(f'Could not stop GPU services: {out}')
        if persist: run(['/usr/bin/systemctl','stop','nvidia-persistenced.service'])
        lost = reg(GPU,'VENDOR_ID') != '10de'
        if lost:
            pulse()
            # Make the reset GPU readable to the old teardown path. Do not
            # change ReBAR while the old driver exists.
            restore_pci(resources,False)
        unload()
        # Start fresh after all old modules are gone, including any teardown
        # firmware state. This is also the normal manual test path.
        pulse()
        restore_pci(resources,True)
        if any(n.startswith('nvidia') for n in modules()):
            raise RuntimeError('Unexpected driver autoload before PCI restoration completed')
        run(['/usr/sbin/modprobe','nvidia','nvidia_uvm'],timeout=30)
        rc,out = run([str(LIB/'cuda-probe')],timeout=30)
        log(out)
        if 'Transfer correctness check: passed' not in out:
            raise RuntimeError('CUDA verification did not pass')
        if pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip() != boot:
            raise RuntimeError('Boot identity unexpectedly changed')
        success = True
        log(f'GPU recovery verified by CUDA; Linux boot unchanged: {boot}')
    finally:
        if persist: run(['/usr/bin/systemctl','start','nvidia-persistenced.service'],check=False)
        if units: userctl('start',*reversed(units))
        if not success: log('Recovery failed; services restored, GPU unavailable. No reboot requested.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='Check compatibility without resetting hardware')
    args = parser.parse_args()
    try: main(args.check)
    except Exception as e:
        log(str(e));sys.exit(1)
