import importlib.util
import json
import os
import pathlib
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('qualify', ROOT / 'qualify.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


def elf_fixture(target=b'call_target', instruction=b'\x90'):
    """Small actual ELF structure, with one relocation in each guarded function."""
    names = b'\0.text\0.symtab\0.strtab\0.rela.text\0.shstrtab\0'
    strings = b'\0' + b'\0'.join(n.encode() for n in q.FUNCTIONS) + b'\0' + target + b'\0'
    code = (instruction + b'\x00\x00\x00\x00') * 3
    symbols = bytes(24)
    for i, name in enumerate(q.FUNCTIONS):
        symbols += struct.pack('<IBBHQQ', strings.index(name.encode()), 2, 0, 1, i * 5, 5)
    symbols += struct.pack('<IBBHQQ', strings.index(target), 0, 0, 0, 0, 0)
    rel = b''.join(struct.pack('<QQq', i * 5 + 1, 4 << 32 | 2, -4) for i in range(3))
    chunks = [b'', code, symbols, strings, rel, names]
    data = bytearray(64)
    data[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', data, 18, 62)
    headers = []
    for i, chunk in enumerate(chunks):
        name = [b'', b'.text', b'.symtab', b'.strtab', b'.rela.text', b'.shstrtab'][i]
        kind = [0, 1, 2, 3, 4, 3][i]
        headers.append((names.index(name) if name else 0, kind, 4 if i == 1 else 0, 0,
                        len(data), len(chunk), 3 if i == 2 else 2 if i == 4 else 0,
                        1 if i == 4 else 0, 1, 24 if i in (2, 4) else 0))
        data.extend(chunk)
    struct.pack_into('<Q', data, 40, len(data))
    struct.pack_into('<HHH', data, 58, 64, len(headers), 5)
    for h in headers:
        data.extend(struct.pack('<IIQQQQIIQQ', *h))
    return bytes(data)


class QualificationTests(unittest.TestCase):
    def test_extracts_instructions_and_call_targets(self):
        result = q.fingerprints(elf_fixture())
        self.assertEqual(set(result), set(q.FUNCTIONS))
        self.assertEqual(result['nvEvoMakeRoom']['relocations'], [[1, 2, 'call_target', -4]])

    def test_call_target_change_is_detected_even_with_identical_instructions(self):
        first = q.fingerprints(elf_fixture())
        second = q.fingerprints(elf_fixture(target=b'wrong_target'))
        self.assertEqual(first['nvEvoMakeRoom']['sha256'], second['nvEvoMakeRoom']['sha256'])
        self.assertNotEqual(first, second)

    def test_instruction_change_is_detected(self):
        self.assertNotEqual(q.fingerprints(elf_fixture()), q.fingerprints(elf_fixture(instruction=b'\xcc')))

    def test_wrong_architecture_and_truncated_elf_are_rejected(self):
        for raw in (b'bad', elf_fixture()[:50], elf_fixture().replace(b'\x7fELF\x02\x01', b'\x7fELF\x02\x02', 1)):
            with self.assertRaises((ValueError, struct.error)):
                q.fingerprints(raw)


if __name__ == '__main__':
    unittest.main()
