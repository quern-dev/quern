"""The little of an ELF file that matching an Android crash to a build needs.

A tombstone names each native library by its GNU BuildId -- `(BuildId: cd79…)`
on every frame -- so a build's `.so` files are matched to a crash by the same
id, read from the `.note.gnu.build-id` note. The note is found through the
program headers (PT_NOTE), which is where the loader reads it, so a library
whose section headers were stripped still yields its id.

Read with the standard library alone, so a test can build the bytes it reads.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

_MAGIC = b"\x7fELF"
_PT_NOTE = 4
_NT_GNU_BUILD_ID = 3
#: e_machine values Android ships.
_MACHINES = {183: "arm64-v8a", 40: "armeabi-v7a", 62: "x86_64", 3: "x86"}


@dataclass
class Elf:
    path: Path
    build_id: str = ""               # lower-case hex, as a tombstone prints it
    abi: str = ""


def is_elf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == _MAGIC
    except OSError:
        return False


def read(path: Path) -> Elf | None:
    """The library's BuildId and ABI, or None if it is not a readable ELF file.

    A malformed or truncated file is None too: it cannot be matched to a
    crash, and reading it must not stop the rest of a build being recorded.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        return _parse(path, data)
    except (ValueError, struct.error):
        return None


def _parse(path: Path, data: bytes) -> Elf | None:
    if data[:4] != _MAGIC or len(data) < 52:
        return None
    wide, endian = data[4], data[5]
    if wide not in (1, 2) or endian not in (1, 2):
        return None
    e = "<" if endian == 1 else ">"
    (machine,) = struct.unpack_from(e + "H", data, 18)
    if wide == 2:
        phoff, = struct.unpack_from(e + "Q", data, 32)
        phentsize, phnum = struct.unpack_from(e + "HH", data, 54)
    else:
        phoff, = struct.unpack_from(e + "I", data, 28)
        phentsize, phnum = struct.unpack_from(e + "HH", data, 42)
    elf = Elf(path=path, abi=_MACHINES.get(machine, f"machine{machine}"))
    for i in range(phnum):
        at = phoff + i * phentsize
        if at + phentsize > len(data):
            raise ValueError("a program header runs past the end of the file")
        if wide == 2:
            p_type, _, p_offset, _, _, p_filesz = struct.unpack_from(e + "IIQQQQ", data, at)
        else:
            p_type, p_offset, _, _, p_filesz = struct.unpack_from(e + "IIIII", data, at)
        if p_type != _PT_NOTE:
            continue
        found = _build_id(data, p_offset, p_filesz, e)
        if found:
            elf.build_id = found
            break
    return elf


def _build_id(data: bytes, offset: int, size: int, e: str) -> str:
    end = offset + size
    if end > len(data):
        raise ValueError("a note runs past the end of the file")
    at = offset
    while at + 12 <= end:
        namesz, descsz, kind = struct.unpack_from(e + "III", data, at)
        name_at = at + 12
        desc_at = name_at + ((namesz + 3) & ~3)
        next_at = desc_at + ((descsz + 3) & ~3)
        if next_at > end:
            raise ValueError("a note entry runs past its segment")
        if kind == _NT_GNU_BUILD_ID and data[name_at:name_at + namesz].rstrip(b"\0") == b"GNU":
            return data[desc_at:desc_at + descsz].hex()
        at = next_at
    return ""
