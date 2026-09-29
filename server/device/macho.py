"""The little of a Mach-O file that matching a crash to a build needs.

A crash report names each binary by UUID, so a build is matched to a crash by
the UUIDs of the binaries it produced. And a binary built from source by that
build carries a *debug map* -- `N_OSO` symbols naming the object files that
hold its DWARF -- which is what `dsymutil` turns into a dSYM. Having one is not
enough: six vendored frameworks in a production app measured here ship with
maps naming object files on their vendors' build machines. So the objects' paths are
returned, for the caller to check exist. A binary with no map at all makes
`dsymutil` exit 0 with a warning and write an empty dSYM (measured on a vendored framework),
which is why none of this is inferred from what `dsymutil` says.

Read with the standard library alone, so it needs no Xcode and a test can build
the bytes it reads.
"""

from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass, field
from pathlib import Path

MH_MAGIC = 0xFEEDFACE
MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
#: The magics as the first four bytes of a file: thin headers are written in
#: the target's byte order (little-endian on every Apple CPU), fat ones big.
_THIN = {struct.pack("<I", MH_MAGIC), struct.pack("<I", MH_MAGIC_64)}
_FAT = {struct.pack(">I", FAT_MAGIC), struct.pack(">I", FAT_MAGIC_64)}

LC_SYMTAB = 0x2
LC_UUID = 0x1B
N_OSO = 0x66
#: More would not be a fat header: Java class files share its magic, and their
#: next field (a version) is far larger than any real count of slices.
_MAX_FAT_ARCHES = 16

_CPU_ARCH_ABI64 = 0x01000000
_CPU_ARCH_ABI64_32 = 0x02000000
_CPU_TYPE_ARM = 12
_CPU_TYPE_X86 = 7


@dataclass
class Slice:
    """One architecture of a binary."""

    arch: str
    uuid: str = ""
    #: The object files the debug map names; empty when there is no map.
    debug_objects: list[str] = field(default_factory=list)

    @property
    def has_debug_map(self) -> bool:
        return bool(self.debug_objects)


@dataclass
class MachO:
    path: Path
    slices: list[Slice] = field(default_factory=list)

    @property
    def uuids(self) -> dict[str, str]:
        return {s.arch: s.uuid for s in self.slices if s.uuid}

    @property
    def has_debug_map(self) -> bool:
        return any(s.has_debug_map for s in self.slices)


def is_macho(path: Path) -> bool:
    """Whether the file starts like a Mach-O binary, thin or fat."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    if head[:4] in _THIN:
        return True
    if head[:4] in _FAT and len(head) == 8:
        return 0 < struct.unpack(">I", head[4:8])[0] <= _MAX_FAT_ARCHES
    return False


def read(path: Path) -> MachO | None:
    """The binary's slices, or None if it is not a readable Mach-O file.

    A malformed or truncated file is None too: it cannot be matched to a
    crash, and reading it must not stop the rest of a build being recorded.
    """
    try:
        with open(path, "rb") as f:
            if f.seek(0, 2) < 8:
                return None
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                slices = _slices(data)
    except (OSError, ValueError, struct.error):
        return None
    return MachO(path=path, slices=slices) if slices else None


def _slices(data) -> list[Slice]:
    magic = data[:4]
    if magic in _THIN:
        return [_thin(data, 0, len(data))]
    if magic not in _FAT:
        return []
    wide = struct.unpack(">I", magic)[0] == FAT_MAGIC_64
    (count,) = struct.unpack_from(">I", data, 4)
    if not 0 < count <= _MAX_FAT_ARCHES:
        return []
    out, entry = [], 8
    for _ in range(count):
        if wide:
            _, _, offset, size, _, _ = struct.unpack_from(">iiQQII", data, entry)
            entry += 32
        else:
            _, _, offset, size, _ = struct.unpack_from(">iiIII", data, entry)
            entry += 20
        if offset + size > len(data):
            raise ValueError("a slice runs past the end of the file")
        out.append(_thin(data, offset, size))
    return out


def _thin(data, base: int, size: int) -> Slice:
    (magic,) = struct.unpack_from("<I", data, base)
    if magic == MH_MAGIC_64:
        header, nlist_size = 32, 16
    elif magic == MH_MAGIC:
        header, nlist_size = 28, 12
    else:
        raise ValueError("not a Mach-O slice")
    cputype, cpusubtype, _, ncmds, _ = struct.unpack_from("<iiIII", data, base + 4)
    s = Slice(arch=_arch(cputype, cpusubtype))
    cmd_at = base + header
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", data, cmd_at)
        if cmdsize < 8 or cmd_at + cmdsize > base + size:
            raise ValueError("a load command runs past its slice")
        if cmd == LC_UUID:
            raw = bytes(data[cmd_at + 8:cmd_at + 24])
            h = raw.hex().upper()
            s.uuid = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
        elif cmd == LC_SYMTAB:
            symoff, nsyms, stroff, strsize = struct.unpack_from("<IIII", data, cmd_at + 8)
            start, strings = base + symoff, base + stroff
            if (start + nsyms * nlist_size > base + size
                    or strings + strsize > base + size):
                raise ValueError("the symbol table runs past its slice")
            # n_type is the fifth byte of every nlist entry: a strided slice
            # reads the lot at C speed, where a loop over 497,000 symbols
            # (a production app's .debug.dylib) would not. Only the N_OSO entries,
            # 2,668 of them there, are then visited one by one.
            types = bytes(data[start + 4:start + nsyms * nlist_size:nlist_size])
            i = types.find(N_OSO)
            while i != -1:
                (strx,) = struct.unpack_from("<I", data, start + i * nlist_size)
                if strx < strsize:
                    end = data.find(b"\0", strings + strx, strings + strsize)
                    name = bytes(data[strings + strx:end if end != -1 else strings + strsize])
                    if name:
                        s.debug_objects.append(name.decode(errors="replace"))
                i = types.find(N_OSO, i + 1)
        cmd_at += cmdsize
    return s


def _arch(cputype: int, cpusubtype: int) -> str:
    sub = cpusubtype & 0xFF
    if cputype == _CPU_TYPE_ARM | _CPU_ARCH_ABI64:
        return "arm64e" if sub == 2 else "arm64"
    if cputype == _CPU_TYPE_ARM | _CPU_ARCH_ABI64_32:
        return "arm64_32"
    if cputype == _CPU_TYPE_X86 | _CPU_ARCH_ABI64:
        return "x86_64"
    if cputype == _CPU_TYPE_ARM:
        return {9: "armv7", 11: "armv7s", 12: "armv7k"}.get(sub, "arm")
    if cputype == _CPU_TYPE_X86:
        return "i386"
    return f"cpu{cputype:#x}"
