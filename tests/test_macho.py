"""The Mach-O reader behind build records (#326), on bytes built here.

Checked against the real thing once, by hand: all 22 binaries of a production
app's device build gave the same UUIDs as `dwarfdump --uuid`, including fat system
binaries (`/bin/ls`), and the debug maps found were exactly the app's, the
widget's and the extension's -- plus six vendored frameworks whose maps name
their vendors' build machines, which is why object paths are returned.
"""

from __future__ import annotations

import struct
import uuid

from server.builds import macho

ARM64 = 0x0100000C
X86_64 = 0x01000007


def thin(uid: uuid.UUID, *, cputype: int = ARM64, subtype: int = 0,
         objects: tuple[str, ...] = (), others: int = 3) -> bytes:
    """A 64-bit Mach-O: header, LC_UUID, LC_SYMTAB, a symbol table whose
    N_OSO entries name `objects`, and `others` ordinary symbols."""
    names = [f"_sym{i}" for i in range(others)]
    entries = [(n, 0x0F) for n in names] + [(o, macho.N_OSO) for o in objects]
    strings = b"\0"
    nlist = b""
    for name, kind in entries:
        nlist += struct.pack("<IBBHQ", len(strings), kind, 0, 0, 0)
        strings += name.encode() + b"\0"
    header, cmds = 32, 24 + 24
    symoff = header + cmds
    stroff = symoff + len(nlist)
    out = struct.pack("<IiiIIII I", macho.MH_MAGIC_64, cputype, subtype, 6, 2, cmds, 0, 0)
    out += struct.pack("<II", macho.LC_UUID, 24) + uid.bytes
    out += struct.pack("<IIIIII", macho.LC_SYMTAB, 24, symoff, len(entries), stroff, len(strings))
    return out + nlist + strings


def fat(*slices: tuple[int, bytes]) -> bytes:
    head = struct.pack(">II", macho.FAT_MAGIC, len(slices))
    offset = 8 + 20 * len(slices)
    table, body = b"", b""
    for cputype, data in slices:
        table += struct.pack(">iiIII", cputype, 0, offset + len(body), len(data), 0)
        body += data
    return head + table + body


U1 = uuid.UUID("8078f7c1-dc60-38a9-ba72-a17ec2727331")
U2 = uuid.UUID("cc8131b8-ffdb-3d26-b4c3-29cf2541b833")


def _write(tmp_path, data: bytes, name="bin"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


class TestUuids:
    def test_a_thin_binary(self, tmp_path):
        m = macho.read(_write(tmp_path, thin(U1)))
        assert m.uuids == {"arm64": "8078F7C1-DC60-38A9-BA72-A17EC2727331"}

    def test_arm64e(self, tmp_path):
        m = macho.read(_write(tmp_path, thin(U1, subtype=2)))
        assert m.uuids == {"arm64e": str(U1).upper()}

    def test_a_fat_binary_has_one_per_slice(self, tmp_path):
        m = macho.read(_write(tmp_path, fat((ARM64, thin(U1)), (X86_64, thin(U2, cputype=X86_64)))))
        assert m.uuids == {"arm64": str(U1).upper(), "x86_64": str(U2).upper()}


class TestDebugMap:
    def test_none_without_n_oso(self, tmp_path):
        m = macho.read(_write(tmp_path, thin(U1)))
        assert not m.has_debug_map and m.slices[0].debug_objects == []

    def test_the_object_paths_it_names(self, tmp_path):
        objs = ("/dd/Build/Intermediates.noindex/A.o", "/dd/libX.a(member.o)")
        m = macho.read(_write(tmp_path, thin(U1, objects=objs, others=500)))
        assert m.has_debug_map and m.slices[0].debug_objects == list(objs)

    def test_in_a_fat_slice_offsets_are_the_slices(self, tmp_path):
        """Symbol table offsets inside a fat file count from the slice."""
        data = fat((X86_64, thin(U2, cputype=X86_64)), (ARM64, thin(U1, objects=("/o/a.o",))))
        m = macho.read(_write(tmp_path, data))
        assert [s.debug_objects for s in m.slices] == [[], ["/o/a.o"]]


class TestNotMachO:
    def test_text(self, tmp_path):
        p = _write(tmp_path, b"hello, world\n")
        assert not macho.is_macho(p) and macho.read(p) is None

    def test_a_java_class_file_shares_the_fat_magic(self, tmp_path):
        p = _write(tmp_path, struct.pack(">IHH", 0xCAFEBABE, 0, 65) + b"\0" * 32)
        assert not macho.is_macho(p) and macho.read(p) is None

    def test_empty(self, tmp_path):
        p = _write(tmp_path, b"")
        assert not macho.is_macho(p) and macho.read(p) is None

    def test_a_missing_file(self, tmp_path):
        assert not macho.is_macho(tmp_path / "nope") and macho.read(tmp_path / "nope") is None


class TestMalformed:
    """Unreadable is None, never an exception: one odd file in a bundle must
    not stop the rest of a build being recorded."""

    def test_truncated_load_commands(self, tmp_path):
        assert macho.read(_write(tmp_path, thin(U1)[:40])) is None

    def test_a_symbol_table_past_the_end(self, tmp_path):
        data = bytearray(thin(U1, objects=("/o/a.o",)))
        struct.pack_into("<I", data, 32 + 24 + 12, 10_000)         # nsyms
        assert macho.read(_write(tmp_path, bytes(data))) is None

    def test_a_fat_slice_past_the_end(self, tmp_path):
        data = bytearray(fat((ARM64, thin(U1))))
        struct.pack_into(">I", data, 8 + 12, 1_000_000)             # size
        assert macho.read(_write(tmp_path, bytes(data))) is None

    def test_a_zero_sized_load_command(self, tmp_path):
        data = bytearray(thin(U1))
        struct.pack_into("<I", data, 32 + 4, 0)                     # LC_UUID cmdsize
        assert macho.read(_write(tmp_path, bytes(data))) is None


def thin32(uid: uuid.UUID, *, cputype: int = 12, subtype: int = 12,
           objects: tuple[str, ...] = ()) -> bytes:
    """A 32-bit Mach-O (armv7k, as a watch builds): 28-byte header, 12-byte nlist."""
    strings, nlist = b"\0", b""
    for name, kind in [("_a", 0x0F)] + [(o, macho.N_OSO) for o in objects]:
        nlist += struct.pack("<IBBhI", len(strings), kind, 0, 0, 0)
        strings += name.encode() + b"\0"
    header, cmds = 28, 48
    symoff = header + cmds
    out = struct.pack("<IiiIIII", macho.MH_MAGIC, cputype, subtype, 6, 2, cmds, 0)
    out += struct.pack("<II", macho.LC_UUID, 24) + uid.bytes
    out += struct.pack("<IIIIII", macho.LC_SYMTAB, 24, symoff, 1 + len(objects),
                       symoff + len(nlist), len(strings))
    return out + nlist + strings


class TestOtherLayouts:
    def test_a_32_bit_slice(self, tmp_path):
        m = macho.read(_write(tmp_path, thin32(U1, objects=("/o/w.o",))))
        assert m.uuids == {"armv7k": str(U1).upper()}
        assert m.slices[0].debug_objects == ["/o/w.o"]

    def test_a_64_bit_fat_header(self, tmp_path):
        """`lipo -fat64`: 32-byte entries with 64-bit offsets."""
        slices = [(ARM64, thin(U1, objects=("/o/a.o",))), (X86_64, thin(U2, cputype=X86_64))]
        head = struct.pack(">II", macho.FAT_MAGIC_64, len(slices))
        offset = 8 + 32 * len(slices)
        table, body = b"", b""
        for cputype, data in slices:
            table += struct.pack(">iiQQII", cputype, 0, offset + len(body), len(data), 0, 0)
            body += data
        m = macho.read(_write(tmp_path, head + table + body))
        assert m.uuids == {"arm64": str(U1).upper(), "x86_64": str(U2).upper()}
        assert m.slices[0].debug_objects == ["/o/a.o"]
