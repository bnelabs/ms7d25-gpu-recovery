#!/usr/bin/python3
"""Pulse the verified MSI MS-7D25 PCI_E1 reset gate and check GPIO readback."""
import argparse, fcntl, os, pathlib, signal, struct, time

def interrupted(signum, frame):
    raise SystemExit(f'Interrupted by signal {signum}')

for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, interrupted)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--hold-seconds', type=int, choices=(1, 5), default=1)
args = parser.parse_args()

board = pathlib.Path('/sys/class/dmi/id/board_name').read_text().strip()
if board != 'PRO Z690-A WIFI DDR4(MS-7D25)':
    raise SystemExit(f'Unexpected motherboard: {board}')
pin = next(l for l in pathlib.Path('/sys/kernel/debug/pinctrl/INTC1056:00/pins').read_text().splitlines()
           if l.startswith('pin 210 (SATAXPCIE_2) '))
if '354:INTC1056:00 GPIO 0x84000201' not in pin:
    raise SystemExit(f'Unexpected GPP_E2 initial configuration: {pin}')

chip = os.open('/dev/gpiochip0', os.O_RDONLY)
handle = None
try:
    info = bytearray(68)
    fcntl.ioctl(chip, 0x8044b401, info)
    name, label, lines = struct.unpack('32s32sI', info)
    if label.rstrip(b'\0') != b'INTC1056:00' or lines != 473:
        raise SystemExit('Unexpected GPIO controller')
    lineinfo = bytearray(struct.pack('II32s32s', 354, 0, b'', b''))
    fcntl.ioctl(chip, 0xc048b402, lineinfo)
    if struct.unpack('II32s32s', lineinfo)[1] != 2:
        raise SystemExit('GPIO line is claimed or no longer an unclaimed output')
    request = bytearray(struct.pack('64II64s32sIi', 354, *([0] * 63), 2,
                                   bytes([1]) + bytes(63), b'gpu-perst-test', 1, 0))
    fcntl.ioctl(chip, 0xc16cb403, request)
    handle = struct.unpack_from('i', request, 360)[0]
    def get():
        data = bytearray(64)
        fcntl.ioctl(handle, 0xc040b408, data)
        return data[0]
    def set_value(value):
        data = bytearray([value]) + bytearray(63)
        fcntl.ioctl(handle, 0xc040b409, data)
    def verify(phase, expected):
        value = get()
        print(f'GPP_E2 {phase}: {value}', flush=True)
        if value != expected:
            raise RuntimeError(f'Unexpected GPP_E2 readback {phase}: expected {expected}, got {value}')
    try:
        verify('before pulse', 1)
        set_value(0)
        verify('asserted', 0)
        time.sleep(args.hold_seconds)
    finally:
        set_value(1)
        verify('released', 1)
finally:
    if handle is not None:
        os.close(handle)
    os.close(chip)
