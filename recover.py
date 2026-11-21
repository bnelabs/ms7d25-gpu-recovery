#!/usr/bin/python3
"""MS-7D25 / GA102 recovery using the verified GPP_E2 PERST gate.
Never reboots. Kernel/driver-specific teardown assistance fails closed.
"""
import argparse, fcntl, hashlib, json, os, pathlib, re, subprocess, sys, time

LIB = pathlib.Path('/usr/local/lib/gpu-recovery')
QUALIFIED = pathlib.Path('/var/lib/gpu-recovery/qualified') / os.uname().release
GPU = '0000:01:00.0'
BRIDGE = '0000:00:01.0'
PCI = pathlib.Path('/sys/bus/pci/devices')
ENV = dict(os.environ, XDG_RUNTIME_DIR='/run/user/1000',
           DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus')
INFERENCE_CONFIG = pathlib.Path('/etc/gpu-recovery/inference-services.json')
DEFAULT_INFERENCE_SERVICES = {
    'gateway': 'inference-gateway.service',
    'gateway_backends': ('llama-server.service', 'strata-server.service', 'strata-coder.service',
                         'maya-server.service', 'hyperqwen-server.service'),
    'user_services': ('deepseek-harness.service',),
    'system_services': (),
}
SERVICE_NAME = re.compile(r'(?:[A-Za-z0-9:_.@-]|\\x[0-9a-fA-F]{2})+\.service')
STATE = pathlib.Path('/var/lib/gpu-recovery/services.json')
POWER_TIMER = 'gpu-power-limit.timer'
ACTIVE = pathlib.Path('/run/gpu-recovery-active')
CDI_SERVICE = 'nvidia-cdi-refresh.service'

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

def systemctl(*args):
    return run(['/usr/bin/systemctl', *args], timeout=90 if args[0] == 'stop' else 30, check=False)


def valid_service_name(unit):
    return (isinstance(unit, str) and SERVICE_NAME.fullmatch(unit)
            and not unit.startswith('-') and not unit.endswith('@.service'))


def inference_services():
    profile = DEFAULT_INFERENCE_SERVICES.copy()
    try:
        with INFERENCE_CONFIG.open() as config:
            info = os.fstat(config.fileno())
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise RuntimeError(f'{INFERENCE_CONFIG} must be owned by the recovery operator and writable only by its owner')
            overrides = json.load(config)
    except FileNotFoundError:
        return profile
    if not isinstance(overrides, dict) or set(overrides) - profile.keys():
        raise RuntimeError(f'Unknown inference service setting in {INFERENCE_CONFIG}')
    profile.update(overrides)
    gateway = profile['gateway']
    if gateway is not None and not valid_service_name(gateway):
        raise RuntimeError(f'Invalid gateway service in {INFERENCE_CONFIG}')
    for group in ('gateway_backends', 'user_services', 'system_services'):
        units = profile[group]
        if not isinstance(units, (list, tuple)) or not all(valid_service_name(unit) for unit in units):
            raise RuntimeError(f'Invalid service list for {group} in {INFERENCE_CONFIG}')
        profile[group] = tuple(dict.fromkeys(units))
    return profile


def configured_user_units(profile):
    gateway = (profile['gateway'],) if profile['gateway'] else ()
    return tuple(dict.fromkeys(gateway + profile['gateway_backends'] + profile['user_services']))


def unit_property(controller, unit, name):
    rc, value = controller('show', unit, f'--property={name}', '--value')
    if rc and value != 'not-found':
        raise RuntimeError(f'Could not read {name} for {unit}: {value}')
    return value


def selected_units(controller, units, previous):
    active_states = ('active', 'activating', 'reloading', 'refreshing')
    return [unit for unit in units if unit in previous
            or unit_property(controller, unit, 'ActiveState') in active_states]


def available_units(controller, units):
    return [unit for unit in units if unit_property(controller, unit, 'LoadState') != 'not-found']


def control_units(controller, action, units):
    if units:
        rc, out = controller(action, *units)
        if rc:
            raise RuntimeError(f'Could not {action} {", ".join(units)}: {out}')


def reg(device, field, timeout=20):
    return run(['/usr/bin/setpci','-s',device,field], timeout=timeout)[1]

def modules():
    return {l.split()[0] for l in pathlib.Path('/proc/modules').read_text().splitlines()}

def qualified_helper():
    manifest = json.loads((QUALIFIED/'manifest.json').read_text())
    helper = QUALIFIED/'evo_bail.ko'
    if os.uname().release != manifest['kernel']:
        raise RuntimeError('Recovery helper kernel mismatch; startup qualification must complete')
    module_path = run(['/usr/sbin/modinfo','-n','nvidia_modeset'])[1]
    if hashlib.sha256(pathlib.Path(module_path).read_bytes()).hexdigest() != manifest['nvidia_modeset_sha256']:
        raise RuntimeError('NVIDIA module changed since automatic qualification')
    if hashlib.sha256(helper.read_bytes()).hexdigest() != manifest['helper_sha256']:
        raise RuntimeError('Recovery helper checksum mismatch')
    return helper

def pulse(hold_seconds=1):
    log(f'Pulsing the GPU-only GPP_E2 PERST gate for {hold_seconds}s')
    _, readback = run(['/usr/bin/python3', str(LIB/'pulse-perst.py'),
                       '--hold-seconds', str(hold_seconds)], timeout=hold_seconds + 7)
    if readback:
        log(readback)
    deadline = time.monotonic() + 15
    vendor = 'unread'
    while time.monotonic() < deadline:
        vendor = reg(GPU, 'VENDOR_ID', timeout=3)
        if vendor == '10de':
            log('GPU PCI vendor ID is readable after PERST')
            return
        time.sleep(.25)
    raise LinkNotReady(f'GPU did not become reachable within 15 seconds after completed PERST (vendor ID {vendor})')


def recover_pci_link():
    try:
        pulse()
    except LinkNotReady as error:
        log(f'{error}. Allowing five more seconds for PCIe before an extended GPU-only pulse.')
        time.sleep(5)
        if reg(GPU, 'VENDOR_ID', timeout=3) == '10de':
            log('GPU PCI vendor ID became readable during the settling interval')
            return
        pulse(hold_seconds=5)

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
                run(['/usr/sbin/insmod',str(qualified_helper()),f'target_pid={p.pid}'])
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

def manage_cdi_service(action):
    _, load_state = run(['/usr/bin/systemctl', 'show', '--property=LoadState', '--value', CDI_SERVICE])
    if load_state != 'not-found':
        run(['/usr/bin/systemctl', action, CDI_SERVICE], timeout=95 if action == 'start' else 20)


def hold_services():
    """Remember desired services across failed recovery and keep them stopped."""
    profile = inference_services()
    user_units = configured_user_units(profile)
    system_units = profile['system_services']
    STATE.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    ACTIVE.write_text('GPU recovery owns module loading; retain until CUDA verification passes.\n')
    saved = json.loads(STATE.read_text()) if STATE.exists() else {}
    desired = {
        'units': selected_units(userctl, user_units, saved.get('units', [])),
        'system_units': selected_units(systemctl, system_units, saved.get('system_units', [])),
        'persist': saved.get('persist', False) or systemctl('is-active', '--quiet', 'nvidia-persistenced.service')[0] == 0,
        'power_timer': saved.get('power_timer', False) or systemctl('is-active', '--quiet', POWER_TIMER)[0] == 0,
    }
    temporary=STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(desired))
    os.chmod(temporary,0o600);temporary.replace(STATE)
    # Stop the front door first so it cannot restart backends during the drain.
    gateway = profile['gateway']
    if gateway:
        control_units(userctl, 'stop', available_units(userctl, (gateway,)))
    control_units(userctl, 'stop', available_units(userctl, (unit for unit in user_units if unit != gateway)))
    control_units(systemctl, 'stop', available_units(systemctl, system_units))
    control_units(systemctl, 'stop', available_units(systemctl, (POWER_TIMER, 'gpu-power-limit.service')))
    manage_cdi_service('stop')
    control_units(systemctl, 'stop', available_units(systemctl, ('nvidia-persistenced.service',)))
    run(['/usr/bin/udevadm','settle','--timeout=20'],timeout=25)


def resume_services():
    if not STATE.exists():return
    saved = json.loads(STATE.read_text())
    profile = inference_services()
    try:
        run(['/usr/bin/udevadm','settle','--timeout=20'],timeout=25)
        ACTIVE.unlink(missing_ok=True)
        # Generate the container device spec while the optional persistence socket is absent.
        manage_cdi_service('start')
        if saved.get('persist'):
            run(['/usr/bin/systemctl', 'start', 'nvidia-persistenced.service'])
        if saved.get('power_timer'):
            run(['/usr/bin/systemctl', 'start', POWER_TIMER])
        system_units = available_units(systemctl, (unit for unit in profile['system_services']
                                                  if unit in saved.get('system_units', [])))
        user_units = available_units(userctl, (unit for unit in configured_user_units(profile)
                                               if unit in saved['units']))
        gateway = profile['gateway']
        if gateway in user_units:
            # The gateway owns when backends start. Independent services resume in their own scope.
            user_units = [unit for unit in user_units if unit == gateway or unit not in profile['gateway_backends']]
        control_units(systemctl, 'start', system_units)
        control_units(userctl, 'start', user_units)
        STATE.unlink()
    except Exception as error:
        try:
            hold_services()
        except Exception as hold_error:
            log(f'Could not reapply the inference hold: {hold_error}')
        raise RuntimeError(f'CUDA passed but service restoration failed: {error}') from error


def initialize_once(resources):
    lost=reg(GPU,'VENDOR_ID')!='10de'
    if lost:
        recover_pci_link()
        restore_pci(resources, False)
    unload()
    pulse();restore_pci(resources,True)
    time.sleep(2)  # Allow endpoint firmware to settle after PERST and BAR restoration.
    if any(n.startswith('nvidia') for n in modules()):
        raise RuntimeError('Unexpected driver autoload before PCI restoration completed')
    rc,out=run(['/usr/sbin/modprobe','--all','nvidia','nvidia_uvm'],timeout=30,check=False)
    if rc:raise InitializationFailed('Fresh module initialization failed: '+out)
    run(['/sbin/ub-device-create'])
    rc,out=run([str(LIB/'cuda-probe')],timeout=30,check=False)
    log(out)
    if rc or 'Transfer correctness check: passed' not in out or 'Kernel correctness check: passed' not in out:
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
    qualified_helper()
    resources = {}
    for dev in (GPU,'0000:01:00.1'):
        resources[dev] = [[int(v,16) for v in l.split()] for l in (PCI/dev/'resource').read_text().splitlines()]
    if resources[GPU][1][1] - resources[GPU][1][0] + 1 != 32 * 1024**3:
        raise RuntimeError('Unexpected GPU BAR1 allocation')
    if check_only:
        inference_services()
        log('Read-only compatibility checks passed: board, topology, kernel, NVIDIA binary, BAR allocation, inference services')
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
    return 75 if isinstance(error,(LinkNotReady,InitializationFailed)) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='Check compatibility without resetting hardware')
    parser.add_argument('--hold-services',action='store_true',help='Stop GPU services and preserve desired state; no hardware reset')
    args = parser.parse_args()
    try: main(args.check,args.hold_services)
    except Exception as e:
        log(str(e));sys.exit(error_exit_status(e))
