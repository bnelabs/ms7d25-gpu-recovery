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
STATE = pathlib.Path('/var/lib/gpu-recovery/services.json')

class CommandTimeout(RuntimeError): pass
class InitializationFailed(RuntimeError): pass
class LinkNotReady(RuntimeError): pass

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
        raise CommandTimeout(f'Timeout in {args[0]}; do not repeat a blocked kernel call')
    if check and p.returncode: raise RuntimeError(f'{args}: {out.strip()}')
    return p.returncode, out.strip()

def userctl(*args):
    return run(['/usr/sbin/runuser','-u','komedi','--','/usr/bin/env',
                'XDG_RUNTIME_DIR=/run/user/1000',
                'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
                '/usr/bin/systemctl','--user',*args], 90 if args and args[0]=='stop' else 30, False)

def reg(device, field):
    return run(['/usr/bin/setpci','-s',device,field])[1]

def modules():
    return {l.split()[0] for l in pathlib.Path('/proc/modules').read_text().splitlines()}

def pulse():
    log('Pulsing the GPU-only GPP_E2 PERST gate')
    run(['/usr/bin/python3',str(LIB/'pulse-perst.py')], 8)
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if reg(GPU,'VENDOR_ID') == '10de': return
        time.sleep(.1)
    raise LinkNotReady('GPU did not become reachable within 15 seconds after completed PERST')

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

def hold_services():
    """Remember desired services across failed recovery and keep them stopped."""
    STATE.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    saved=json.loads(STATE.read_text()) if STATE.exists() else {'units':[], 'persist':False}
    units=set(saved['units']) | {u for u in UNITS if userctl('is-active','--quiet',u)[0]==0}
    if not units<=set(UNITS):raise RuntimeError('Unexpected saved service')
    persist=saved['persist'] or run(['/usr/bin/systemctl','is-active','--quiet','nvidia-persistenced.service'],check=False)[0]==0
    temporary=STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps({'units':sorted(units),'persist':persist}))
    os.chmod(temporary,0o600);temporary.replace(STATE)
    # Stop both units even when one is currently activating or restarting.
    rc,out=userctl('stop',*UNITS)
    if rc:raise RuntimeError('Could not stop GPU services: '+out)
    run(['/usr/bin/systemctl','stop','nvidia-persistenced.service'])
    return saved


def resume_services():
    if not STATE.exists():return
    saved=json.loads(STATE.read_text())
    if not set(saved['units'])<=set(UNITS):raise RuntimeError('Unexpected saved service')
    if saved['persist']:run(['/usr/bin/systemctl','start','nvidia-persistenced.service'])
    if saved['units']:
        ordered=[u for u in reversed(UNITS) if u in saved['units']]
        rc,out=userctl('start',*ordered)
        if rc:raise RuntimeError('GPU recovered but service startup failed: '+out)
    STATE.unlink()


def initialize_once(resources):
    lost=reg(GPU,'VENDOR_ID')!='10de'
    if lost:
        pulse();restore_pci(resources,False)
    unload()
    pulse();restore_pci(resources,True)
    if any(n.startswith('nvidia') for n in modules()):
        raise RuntimeError('Unexpected driver autoload before PCI restoration completed')
    rc,out=run(['/usr/sbin/modprobe','--all','nvidia','nvidia_uvm'],timeout=30,check=False)
    if rc:raise InitializationFailed('Fresh module initialization failed: '+out)
    rc,out=run([str(LIB/'cuda-probe')],timeout=30,check=False)
    log(out)
    if rc or 'Transfer correctness check: passed' not in out:
        raise InitializationFailed('Fresh CUDA verification failed: '+out)


def initialize_with_retry(resources):
    for attempt in range(2):
        try:
            initialize_once(resources);return
        except InitializationFailed:
            if attempt:raise
            log('Fresh initialization failed; one clean unload/PERST/reload retry in 5 seconds')
            time.sleep(5)


def main(check_only=False,hold_only=False):
    if os.geteuid(): raise RuntimeError('Run as root')
    lock = open('/run/gpu-recovery.lock','w')
    fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    if hold_only:
        hold_services();return
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
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    success = False
    try:
        hold_services()
        initialize_with_retry(resources)
        if pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip() != boot:
            raise RuntimeError('Boot identity unexpectedly changed')
        success = True
        log(f'GPU recovery verified by CUDA; Linux boot unchanged: {boot}')
        resume_services()
    finally:
        if not success: log('Recovery failed; inference services remain stopped. No reboot requested.')

def error_exit_status(error):
    return 75 if isinstance(error,LinkNotReady) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='Check compatibility without resetting hardware')
    parser.add_argument('--hold-services',action='store_true',help='Stop GPU services and preserve desired state; no hardware reset')
    args = parser.parse_args()
    try: main(args.check,args.hold_services)
    except Exception as e:
        log(str(e));sys.exit(error_exit_status(e))
