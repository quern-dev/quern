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
from typing import BinaryIO

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


#: A build-id note is 36 bytes; notes segments are small. Past this, a
#: "note" is a corrupt header, not something to read into memory.
_MAX_NOTES = 1 << 16


def read(path: Path) -> Elf | None:
    """The library's BuildId and ABI, or None if it is not a readable ELF file.

    A malformed or truncated file is None too: it cannot be matched to a
    crash, and reading it must not stop the rest of a build being recorded.
    Only the headers and notes are read, not the library.
    """
    try:
        with open(path, "rb") as f:
            return _parse(path, f)
    except (OSError, ValueError, struct.error):
        return None


def _at(f: BinaryIO, offset: int, size: int) -> bytes:
    f.seek(offset)
    data = f.read(size)
    if len(data) != size:
        raise ValueError("the file ends inside a header")
    return data


def _parse(path: Path, f: BinaryIO) -> Elf | None:
    head = f.read(64)
    if head[:4] != _MAGIC or len(head) < 52:
        return None
    wide, endian = head[4], head[5]
    if wide not in (1, 2) or endian not in (1, 2):
        return None
    e = "<" if endian == 1 else ">"
    (machine,) = struct.unpack_from(e + "H", head, 18)
    if wide == 2:
        phoff, = struct.unpack_from(e + "Q", head, 32)
        phentsize, phnum = struct.unpack_from(e + "HH", head, 54)
    else:
        phoff, = struct.unpack_from(e + "I", head, 28)
        phentsize, phnum = struct.unpack_from(e + "HH", head, 42)
    elf = Elf(path=path, abi=_MACHINES.get(machine, f"machine{machine}"))
    table = _at(f, phoff, phentsize * phnum)
    for i in range(phnum):
        at = i * phentsize
        if wide == 2:
            p_type, _, p_offset, _, _, p_filesz = struct.unpack_from(e + "IIQQQQ", table, at)
        else:
            p_type, p_offset, _, _, p_filesz = struct.unpack_from(e + "IIIII", table, at)
        if p_type != _PT_NOTE:
            continue
        if p_filesz > _MAX_NOTES:
            raise ValueError("a notes segment is implausibly large")
        found = _build_id(_at(f, p_offset, p_filesz), e)
        if found:
            elf.build_id = found
            break
    return elf


def _build_id(data: bytes, e: str) -> str:
    end = len(data)
    at = 0
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
