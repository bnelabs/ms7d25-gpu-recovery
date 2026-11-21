#!/usr/bin/python3
"""Qualify the known EVO rescue against a kernel build without touching the GPU."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
FUNCTIONS = ('nvEvoMakeRoom', 'EvoCheckNotifier', 'nvkms_yield')


def prepare_udev_guard():
    """Keep NVIDIA's module events from racing the recovery-owned load order."""
    vendor = Path('/usr/lib/udev/rules.d/71-nvidia.rules')
    override = Path('/etc/udev/rules.d/71-nvidia.rules')
    if override.exists():
        raise RuntimeError('An existing local NVIDIA udev override needs review')
    if not vendor.exists():
        raise RuntimeError('Expected NVIDIA udev rules are missing')
    runtime = Path('/run/udev/rules.d/71-nvidia.rules')
    content = ('# Generated GPU recovery guard; vendor behavior outside recovery is unchanged.\n'
               'TEST=="/run/gpu-recovery-active", GOTO="gpu_recovery_vendor_end"\n'
               + vendor.read_text() + '\nLABEL="gpu_recovery_vendor_end"\n')
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if not runtime.exists() or runtime.read_text() != content:
        temporary = runtime.with_suffix('.tmp')
        temporary.write_text(content)
        temporary.replace(runtime)
        subprocess.run(['/usr/bin/udevadm', 'control', '--reload-rules'], check=True, timeout=10)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fingerprints(data):
    """ELF64 little-endian function bytes AND relocation targets, not addresses."""
    if data[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', data, 18)[0] != 62:
        raise ValueError('Expected x86-64 little-endian ELF')
    offset = struct.unpack_from('<Q', data, 40)[0]
    stride, count, names_index = struct.unpack_from('<HHH', data, 58)
    if stride != 64 or count == 0:
        raise ValueError('Unsupported ELF section layout')
    headers = [struct.unpack_from('<IIQQQQIIQQ', data, offset + i * stride) for i in range(count)]
    def section(i):
        h = headers[i]
        if h[4] + h[5] > len(data):
            raise ValueError('Truncated ELF section')
        return data[h[4]:h[4] + h[5]]
    def string(table, position):
        return table[position:].split(b'\0', 1)[0].decode()
    names = section(names_index)
    section_names = [string(names, h[0]) for h in headers]
    tables = {}
    for i, h in enumerate(headers):
        if h[1] != 2:  # SHT_SYMTAB
            continue
        strings = section(h[6])
        raw = section(i)
        if h[9] != 24 or len(raw) % 24:
            raise ValueError('Unsupported symbol table')
        tables[i] = []
        for at in range(0, len(raw), 24):
            name, info, other, index, value, size = struct.unpack_from('<IBBHQQ', raw, at)
            tables[i].append({'name': string(strings, name), 'info': info,
                             'section': index, 'value': value, 'size': size})
    result = {}
    for name in FUNCTIONS:
        matches = [s for table in tables.values() for s in table if s['name'] == name and s['info'] & 15 == 2]
        if len(matches) != 1:
            raise ValueError('Missing or ambiguous function ' + name)
        symbol = matches[0]
        index, start, size = symbol['section'], symbol['value'], symbol['size']
        if index >= len(headers) or not headers[index][2] & 4 or size == 0:
            raise ValueError('Invalid executable function ' + name)
        body = section(index)[start:start + size]
        if len(body) != size:
            raise ValueError('Function extends outside section')
        relocations = []
        for i, h in enumerate(headers):
            if h[1] != 4 or h[7] != index:  # SHT_RELA, target section
                continue
            raw = section(i)
            if h[9] != 24 or len(raw) % 24:
                raise ValueError('Unsupported relocation table')
            for at in range(0, len(raw), 24):
                position, info, addend = struct.unpack_from('<QQq', raw, at)
                if start <= position < start + size:
                    target = tables[h[6]][info >> 32]
                    target_name = target['name']
                    if not target_name and target['section'] < len(headers):
                        target_name = section_names[target['section']]
                    relocations.append([position - start, info & 0xffffffff, target_name, addend])
        result[name] = {'size': size, 'sha256': sha(body), 'relocations': relocations}
    return result


def module_bytes(path):
    if path.suffix == '.zst':
        return subprocess.check_output(['/usr/bin/zstd', '-dc', str(path)])
    if path.suffix == '.xz':
        import lzma
        return lzma.decompress(path.read_bytes())
    return path.read_bytes()


def prepare(kernel, output, source=ROOT / 'evo-bail', policy_path=ROOT / 'qualified-driver.json'):
    policy = json.loads(policy_path.read_text())
    module = Path(subprocess.check_output(['/usr/sbin/modinfo', '-k', kernel, '-n', 'nvidia_modeset'], text=True).strip())
    version = subprocess.check_output(['/usr/sbin/modinfo', '-F', 'version', str(module)], text=True).strip()
    if version != policy['driver'] or fingerprints(module_bytes(module)) != policy['functions']:
        raise RuntimeError('NVIDIA EVO instructions or relocation targets changed; this helper does not match')
    source_hash = sha((source / 'evo_bail.c').read_bytes())
    module_hash = sha(module.read_bytes())
    manifest_path = output / 'manifest.json'
    helper = output / 'evo_bail.ko'
    if manifest_path.exists() and helper.exists():
        saved = json.loads(manifest_path.read_text())
        if (saved.get('kernel'), saved.get('nvidia_modeset_sha256'), saved.get('helper_source_sha256'), saved.get('helper_sha256')) == (
                kernel, module_hash, source_hash, sha(helper.read_bytes())):
            print('GPU recovery qualification current for ' + kernel, flush=True)
            return saved
    headers = Path('/lib/modules') / kernel / 'build'
    if not headers.exists():
        raise RuntimeError('Install matching kernel headers to build GPU recovery helper: ' + kernel)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.qualify-', dir=output.parent) as directory:
        build = Path(directory)
        for name in ('evo_bail.c', 'Makefile'):
            shutil.copy2(source / name, build / name)
        subprocess.run(['/usr/bin/make', '-j2', '-C', str(headers), 'M=' + str(build), 'modules'], check=True, timeout=180)
        built = build / 'evo_bail.ko'
        vermagic = subprocess.check_output(['/usr/sbin/modinfo', '-F', 'vermagic', str(built)], text=True).split()[0]
        if vermagic != kernel:
            raise RuntimeError('Built helper kernel mismatch')
        saved = {'kernel': kernel, 'driver': version, 'nvidia_modeset_sha256': module_hash,
                 'helper_source_sha256': source_hash, 'helper_sha256': sha(built.read_bytes()),
                 'qualification': 'Known function bytes and ELF relocation targets match the proven driver'}
        output.mkdir(mode=0o700, exist_ok=True)
        temporary = output / '.evo_bail.ko.tmp'
        shutil.copy2(built, temporary)
        temporary.replace(helper)
        temporary = output / '.manifest.json.tmp'
        temporary.write_text(json.dumps(saved, indent=2) + '\n')
        temporary.replace(manifest_path)
    print('Qualified and built GPU recovery for ' + kernel, flush=True)
    return saved


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel', default=os.uname().release)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if os.geteuid() == 0:
        prepare_udev_guard()
    prepare(args.kernel, args.output or Path('/var/lib/gpu-recovery/qualified') / args.kernel)
