#!/usr/bin/env python3
"""pakfmt.py -- UE 5.8 (.pak v11) reader/writer, byte-accurate.

Authoritative sources (full UE 5.8 source is installed at G:\\UE_5.8):
    PakFileUtilities.cpp:3104-3262  WritePakFooter / CreatePakFile  (write layout)
    PakFileUtilities.cpp:2027-2060  FSecondaryIndexWriter           (placeholder)
    PakFile.cpp:404-698             LoadIndexInternal               (mirror for reading)
    PakFile.cpp:699-780             DirectoryIndex load/save
    PakFile.cpp:907-919             HashPath
    PakFile.cpp:1594-1732           EncodePakEntry / DecodePakEntry
    IPlatformFilePak.h:405-570      FPakEntry / GetSerializedSize / Serialize
    IPlatformFilePak.h:605-702      FPakEntryLocation
    Runtime/Core/Private/Misc/Fnv.cpp  FFnv::MemFnv64

Key facts pinned down empirically against real Ready or Not paks
---------------------------------------------------------------
1. FString on disk = int32 byte-length (INCLUDING trailing NUL) + bytes + NUL.
   NOT the UTF-16 "len x 2" form the engine source suggests. Proof:
     mount point len=21 followed by 21 ASCII bytes '../../../ReadyOrNot/\\0';
     FDI directory name len=20 followed by 'Content/Blueprints/\\0'.
   Do not use a bytes//2 heuristic -- ASCII path bytes have zero high bytes and
   the heuristic greedily mis-fires (it read 9 bytes as 18, desyncing the index).

2. PrimaryIndex secondary-index flags are 4 bytes each, because
   FArchive::operator<<(bool&) serializes a bool as int32. (PakFile.cpp:477/490)

3. FPakEntry header size for a single-block compressed entry is exactly 73:
     8+8+8 (offset,size,uncompressed) + 4 (method) + 20 (sha1)
     + 4 + 16*1 (compression blocks array) + 1 (flags) + 4 (block size)

4. Encoded index entry for that same single-block entry is exactly 16 bytes:
     u32 flags + u32 offset + u32 uncompressed + u32 size
   With exactly one block, CompressionBlockSizePacked is 0 and no block table
   follows (PakFile.cpp:1655-1662, 1721).

5. Both secondary indexes are present in these paks (PHI and FDI), and the
   PathHashIndex block is followed by the PrunedDirectoryIndex.
"""

from __future__ import annotations

import binascii
import hashlib
import os
import struct
from dataclasses import dataclass, field

MAGIC = 0x5A6F12E1
PAK_VERSION = 11

U64 = 0xFFFFFFFFFFFFFFFF
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x00000100000001B3

COMPRESSION_METHOD_NAME_LEN = 32
MAX_NUM_COMPRESSION_METHODS = 5

FLAG_NONE = 0x00
FLAG_ENCRYPTED = 0x01
FLAG_DELETED = 0x02

# FPakEntryLocation
LOC_INVALID = -0x80000000
LOC_MAX_INDEX = 0x7FFFFFFE

# Version thresholds used below.
V_INITIAL = 1                    # 最早的版本：条目里带 Timestamp
V_COMPRESSION_ENCRYPTION = 3     # 起：条目带压缩块表 + 标志位 + 块大小
V_RELATIVE_CHUNK_OFFSETS = 5     # 起：offset 相对数据区起点（不再是文件绝对偏移）
V_FNAME_BASED_METHOD = 8         # 起：footer 里带压缩方式名表
V_ENCODED_INDEX = 10             # 起：FPakEntryLocation + FDI/PHI（二级索引）
V_FNV64_BUGFIX = 11
V_UTF8_PAK_DIRECTORY = 12


class PakError(Exception):
    pass


# ===========================================================================
# Hashes
# ===========================================================================
def mem_fnv64(data: bytes, offset: int) -> int:
    """FFnv::MemFnv64 (Fnv.cpp:26).

        uint64 Fnv = Offset + InOffset;
        for (; Length; --Length) { Fnv ^= *Data++; Fnv *= Prime; }

    Note the order: XOR then multiply (FNV-1), and the initial value is
    Offset + seed rather than a plain offset basis.
    """
    fnv = (FNV_OFFSET + offset) & U64
    for b in data:
        fnv ^= b
        fnv = (fnv * FNV_PRIME) & U64
    return fnv


def hash_path(relative_path_from_mount: str, seed: int,
              version: int = PAK_VERSION) -> int:
    """FPakFile::HashPath (PakFile.cpp:907).

    v11 >= PakFile_Version_Fnv64BugFix so it uses FFnv::MemFnv64.
    Input is the lowercased path relative to the mount point, hashed over its
    TCHAR bytes (UTF-16LE), and Length is Len()*sizeof(TCHAR).
    """
    low = relative_path_from_mount.lower()
    return mem_fnv64(low.encode("utf-16-le"), seed)


def path_hash_seed(pak_filename: str) -> int:
    """PathHashSeed = FCrc::StrCrc32(lowercased pak FILENAME).

    PakFileUtilities.cpp:929-931. Equivalent to zlib crc32.
    """
    base = os.path.basename(pak_filename).lower()
    return binascii.crc32(base.encode("utf-8")) & 0xFFFFFFFF


# ===========================================================================
# Archive primitives
# ===========================================================================
def w_str(s: str) -> bytes:
    """FArchive << FString -- int32 byte length (incl. NUL) + bytes + NUL."""
    raw = s.encode("utf-8")
    return struct.pack("<i", len(raw) + 1) + raw + b"\x00"


def r_str(data: bytes, p: int) -> tuple[str, int]:
    """Read the byte-length FString form."""
    n = struct.unpack_from("<i", data, p)[0]
    if n < 0:
        raise PakError(f"negative FString length {n} @{p}")
    if n == 0:
        return "", p + 4
    raw = data[p + 4:p + 4 + n]
    if len(raw) != n:
        raise PakError(f"FString out of range n={n} @{p}")
    return raw.rstrip(b"\x00").decode("utf-8", "replace"), p + 4 + n


def w_archive_bool(v: bool) -> bytes:
    """FArchive << bool -- 4 bytes (int32), NOT 1 byte."""
    return struct.pack("<I", 1 if v else 0)


def w_int32(v: int) -> bytes:
    return struct.pack("<i", v)


# ===========================================================================
# FPakEntry -- in-pak header and index metadata
# ===========================================================================
@dataclass
class PakEntry:
    offset: int = -1              # address in uncompressed space == header start
    size: int = 0                 # compressed byte count
    uncompressed_size: int = 0
    method_index: int = 0         # 0 = stored, n>=1 -> compression method table[n-1]
    flags: int = FLAG_NONE
    compression_block_size: int = 0
    compression_blocks: list = field(default_factory=list)
    sha1: bytes = b"\x00" * 20
    version: int = PAK_VERSION
    # Per-block compressed LENGTHS, as stored in the index for multi-block entries.
    _block_lengths: list = field(default_factory=list, repr=False)

    @property
    def nblocks(self) -> int:
        return len(self._block_lengths) or len(self.compression_blocks)

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & FLAG_ENCRYPTED)

    @property
    def deleted(self) -> bool:
        return bool(self.flags & FLAG_DELETED)

    @property
    def compressed(self) -> bool:
        return self.method_index != 0

    def header_size(self, version: int | None = None) -> int:
        """FPakEntry::GetSerializedSize for v11.

        v11 >= FNameBasedCompressionMethod, >= CompressionEncryption,
        >= NoTimestamps, so:
            Offset(8) Size(8) UncompressedSize(8) MethodIndex(4) Hash(20)
            + [ if compressed: int32 count + count*16 ]
            + Flags(1) CompressionBlockSize(4)
        """
        v = version if version is not None else self.version
        n = 8 + 8 + 8 + 4 + 20
        if v >= V_COMPRESSION_ENCRYPTION:
            n += 1 + 4
            if self.compressed:
                n += 4 + 16 * len(self.compression_blocks)
        return n

    def serialize_header(self, version: int | None = None) -> bytes:
        """The FPakEntry that sits in the data area next to the payload."""
        v = version if version is not None else self.version
        out = bytearray()
        out += struct.pack("<qqq", self.offset, self.size, self.uncompressed_size)
        out += struct.pack("<I", self.method_index)
        out += self.sha1
        if v >= V_COMPRESSION_ENCRYPTION:
            if self.compressed:
                out += struct.pack("<i", len(self.compression_blocks))
                for s, e in self.compression_blocks:
                    out += struct.pack("<qq", s, e)
            out += struct.pack("<B", self.flags)
            out += struct.pack("<I", self.compression_block_size)
        return bytes(out)

    @staticmethod
    def deserialize_header(data: bytes, p: int,
                           version: int = PAK_VERSION) -> tuple["PakEntry", int]:
        e = PakEntry(version=version)
        e.offset, e.size, e.uncompressed_size = struct.unpack_from("<qqq", data, p)
        p += 24
        e.method_index = struct.unpack_from("<I", data, p)[0]
        p += 4
        e.sha1 = data[p:p + 20]
        p += 20
        if version >= V_COMPRESSION_ENCRYPTION:
            if e.method_index != 0:
                n = struct.unpack_from("<i", data, p)[0]
                p += 4
                if n < 0 or n > (1 << 20):
                    raise PakError(f"bad compression block count {n}")
                e.compression_blocks = [struct.unpack_from("<qq", data, p + i * 16)
                                        for i in range(n)]
                p += 16 * n
            e.flags = data[p]
            p += 1
            e.compression_block_size = struct.unpack_from("<I", data, p)[0]
            p += 4
        return e, p


# ===========================================================================
# Encoded index entry
# ===========================================================================
def encode_entry_index(e: PakEntry) -> bytes:
    """FPakFile::EncodePakEntry (PakFile.cpp:1594) for the common case.

    Bitfield (PakFile.cpp:1664-1673):
        bit31 offset 32-bit safe
        bit30 uncompressed size 32-bit safe
        bit29 size 32-bit safe
        bits28-23 CompressionMethodIndex
        bit22 encrypted
        bits21-6 CompressionBlocks.Num()
        bits5-0 CompressionBlockSizePacked = (CompressionBlockSize >> 11) & 0x3F
    Field order (PakFile.cpp:1675-1729):
        u32 Flags
        [u32 CompressionBlockSize if packed == 0x3F]
        Offset        (u32 if safe else i64)
        Uncompressed  (u32 if safe else i64)
        [Size         (u32 if safe else i64)]      if method != 0
        [u32 per block compressed size]            if blocks>1 or (blocks==1 and encrypted)
    """
    # With exactly one block, EncodePakEntry forces packed=0 (PakFile.cpp:1654-1662)
    # and decoding restores CompressionBlockSize = UncompressedSize.
    nblk = len(e.compression_blocks)
    packed = 0
    if nblk > 1:
        packed = (e.compression_block_size >> 11) & 0x3F
        if (packed << 11) != e.compression_block_size:
            packed = 0x3F

    off32 = 0 <= e.offset <= 0xFFFFFFFF
    unc32 = 0 <= e.uncompressed_size <= 0xFFFFFFFF
    size32 = 0 <= e.size <= 0xFFFFFFFF

    flags = 0
    if off32:
        flags |= 1 << 31
    if unc32:
        flags |= 1 << 30
    if size32:
        flags |= 1 << 29
    flags |= (e.method_index & 0x3F) << 23
    if e.encrypted:
        flags |= 1 << 22
    flags |= (nblk & 0xFFFF) << 6
    flags |= packed & 0x3F

    out = bytearray(struct.pack("<I", flags))
    if packed == 0x3F:
        out += struct.pack("<I", e.compression_block_size)
    out += struct.pack("<I", e.offset) if off32 else struct.pack("<q", e.offset)
    out += (struct.pack("<I", e.uncompressed_size) if unc32
            else struct.pack("<q", e.uncompressed_size))
    if e.method_index != 0:
        out += struct.pack("<I", e.size) if size32 else struct.pack("<q", e.size)
        if nblk > 1 or (nblk == 1 and e.encrypted):
            for s, en in e.compression_blocks:
                out += struct.pack("<I", en - s)
    return bytes(out)


def decode_entry_index(src: bytes, p: int,
                       version: int = PAK_VERSION) -> tuple[PakEntry, int]:
    """FPakFile::DecodePakEntry (PakFile.cpp:1734)."""
    e = PakEntry(version=version)
    flags = struct.unpack_from("<I", src, p)[0]
    p += 4
    off32 = bool(flags & (1 << 31))
    unc32 = bool(flags & (1 << 30))
    size32 = bool(flags & (1 << 29))
    e.method_index = (flags >> 23) & 0x3F
    e.flags = FLAG_ENCRYPTED if (flags & (1 << 22)) else FLAG_NONE
    nblk = (flags >> 6) & 0xFFFF
    packed = flags & 0x3F

    if packed == 0x3F:
        e.compression_block_size = struct.unpack_from("<I", src, p)[0]
        p += 4
    e.offset = struct.unpack_from("<I" if off32 else "<q", src, p)[0]
    p += 4 if off32 else 8
    e.uncompressed_size = struct.unpack_from("<I" if unc32 else "<q", src, p)[0]
    p += 4 if unc32 else 8

    if e.method_index != 0:
        e.size = struct.unpack_from("<I" if size32 else "<q", src, p)[0]
        p += 4 if size32 else 8
        if nblk == 1:
            e.compression_block_size = e.uncompressed_size
        elif nblk > 1:
            e.compression_block_size = packed << 11
        if nblk > 1 or (nblk == 1 and e.encrypted):
            sizes = [struct.unpack_from("<I", src, p + i * 4)[0] for i in range(nblk)]
            p += 4 * nblk
            e._block_lengths = list(sizes)
            # Only the block LENGTHS are stored. The offsets are rebuilt from the
            # engine's fixed multi-block base: the header WITHOUT the block table
            # (53 bytes) plus the 4-byte block count = 57.
            # Verified against an UnrealPak -Create pak: a 2-block entry (header 89)
            # and a 7-block entry (header 169) BOTH start their first block at 57.
            cur = 57
            blocks = []
            for s in sizes:
                blocks.append((cur, cur + s))
                cur += s
            e.compression_blocks = blocks
        else:
            # One block and not encrypted: no on-disk block table, and the payload
            # sits immediately after the header.
            h = e.header_size(version)
            e.compression_blocks = [(h, h + e.size)]
    else:
        e.size = e.uncompressed_size
        e.compression_blocks = []
    return e, p


# ===========================================================================
# Secondary index structures
# ===========================================================================
@dataclass
class SecondaryIndex:
    offset: int
    size: int
    sha1: bytes


def split_path(rel: str) -> tuple[str, str]:
    """Split into (directory key, filename).

    Directory keys always start AND end with '/', so a file directly under the
    mount point lives in the directory '/' (this is what real paks contain, and
    PakFile.cpp:1299 asserts Parent ends with '/'). The mount-relative path used
    for hashing is simply the concatenation of the two parts.
    """
    rel = rel.replace("\\", "/").lstrip("/")
    i = rel.rfind("/")
    if i < 0:
        return "/", rel
    return "/" + rel[:i + 1], rel[i + 1:]


def serialize_directory_index(dirs: dict[str, list[tuple[str, int]]],
                              file_name_form: int = PAK_VERSION) -> bytes:
    """SaveIndexInternal_DirectoryIndex (PakFile.cpp:764).

    int32 NumDirectories, then per directory:
        FString dir name, int32 NumFiles, then per file: name, int32 location.
    Directory names are ALWAYS FString. File names are FString below
    PakFile_Version_Utf8PakDirectory (12) and FUtf8String from 12 on
    (PakFile.cpp:726-744). Both serialize as byte-length + bytes + NUL, so the
    bytes are the same; the parameter documents the v12 semantic.
    """
    out = bytearray()
    out += w_int32(len(dirs))
    for dname in sorted(dirs):
        out += w_str(dname)
        files = sorted(dirs[dname], key=lambda x: x[0])
        out += w_int32(len(files))
        for fname, loc in files:
            out += w_str(fname)
            out += w_int32(loc)
    return bytes(out)


# ===========================================================================
# Writer
# ===========================================================================
class PakWriter:
    """Write a pak v11 from scratch.

    Layout (CreatePakFile / WritePakFooter):
        [data area: for each entry, FPakEntry header + payload]
        [PrimaryIndex]
        [PathHashIndex = hash table + PrunedDirectoryIndex]
        [FullDirectoryIndex]                       (optional)
        [footer]
    IndexOffset points at the first byte of PrimaryIndex.
    """

    def __init__(self, mount_point: str = "../../../ReadyOrNot/",
                 methods: list[str] | None = None,
                 version: int = PAK_VERSION,
                 str_form: str = "auto"):
        self.mount_point = mount_point
        # Compression method table. Real Ready or Not paks put 'Oodle' in slot 0 and
        # use method_index 1 for it, so that is the default. Whatever is used MUST
        # match the payload bytes or the engine picks the wrong decoder.
        self.methods = list(methods) if methods else ["Oodle", "", "", "", ""]
        self.version = version
        # Directory-index string form: v11 uses FString, v12 uses FUtf8String.
        # Both are byte-length + bytes + NUL, so the bytes are identical; the flag
        # exists to document intent and to allow forcing a form.
        self.str_form = str_form
        self._entries: list[tuple[str, PakEntry, bytes]] = []

    def add(self, rel_path: str, entry: PakEntry, payload: bytes):
        """payload = compressed bytes (or raw bytes when method_index == 0)."""
        self._entries.append((rel_path.replace("\\", "/").lstrip("/"), entry, payload))

    @property
    def num_entries(self) -> int:
        return len(self._entries)

    def _method_table(self) -> list[str]:
        """footer 里的压缩方式名表：**永远**写满 5 个槽位（每个 32 字节）。

        ★ 这里必须补齐：少写几个槽位会让整个 footer 短一截，UnrealPak 直接
          「Unable to open pak file」（实测：只传 1 个方法名时，包连打开都打不开，
          而自研读取器因为按 magic 扫描 + 校验索引 SHA1，反而读得出来）。
        """
        table = [str(m or "") for m in self.methods][:MAX_NUM_COMPRESSION_METHODS]
        table += [""] * (MAX_NUM_COMPRESSION_METHODS - len(table))
        return table

    def build(self, out_path: str, *, write_fdi: bool = True,
              write_phi: bool = True,
              write_guid: bool = True,
              pak_name_for_seed: str | None = None,
              verify_roundtrip: bool = True) -> dict:
        seed = path_hash_seed(pak_name_for_seed or out_path)
        V = self.version

        # ---- data area ----
        prepared = []
        for rel, e, payload in self._entries:
            entry = PakEntry(
                offset=0,
                size=e.size,
                uncompressed_size=e.uncompressed_size,
                method_index=e.method_index,
                flags=e.flags,
                compression_block_size=e.compression_block_size,
                sha1=e.sha1,
                version=V,
                _block_lengths=list(e._block_lengths),
            )
            prepared.append([rel, entry, payload])
        prepared.sort(key=lambda x: x[0])

        data = bytearray()
        for rel, e, payload in prepared:
            hdr_off = len(data)
            if e.compressed:
                lengths = list(e._block_lengths)
                if sum(lengths) != len(payload):
                    # No stored length table (a caller-supplied payload we have not
                    # seen before): treat it as a single block.
                    lengths = [len(payload)]
                blk_size = e.compression_block_size
                if len(lengths) <= 1:
                    blk_size = blk_size or e.uncompressed_size
                else:
                    blk_size = blk_size or 0x10000
                # Index form: block offsets are relative to the fixed 57 base.
                offs = [57]
                for ln in lengths[:-1]:
                    offs.append(offs[-1] + ln)
                index_blocks = [(o, o + ln) for o, ln in zip(offs, lengths)]
            else:
                lengths = []
                blk_size = 0
                index_blocks = []

            # The index form deliberately does NOT carry Flags (EncodePakEntry never
            # writes them; PakFile.cpp:1594-1732), and UnrealPak compares the payload
            # header against the index entry with IndexDataEquals, which does include
            # Flags. So the on-disk header must also use 0.
            # The header form uses block offsets relative to the HEADER start, and
            # UnrealPak writes Offset = 0 there (PakFileUtilities.cpp:1051/1143
            # "Don't serialize offsets here.").
            hdr_entry = PakEntry(
                offset=0, size=e.size, uncompressed_size=e.uncompressed_size,
                method_index=e.method_index, flags=FLAG_NONE,
                compression_block_size=blk_size, sha1=e.sha1,
                version=V,
            )
            if lengths:
                hdr_entry._block_lengths = lengths
                hdr_entry.compression_blocks = [(o, o + ln)
                                                for o, ln in zip(offs, lengths)]
                hs = hdr_entry.header_size(V)
                delta = hs - 57
                hdr_entry.compression_blocks = [(s + delta, t + delta)
                                                for s, t in hdr_entry.compression_blocks]
            header = hdr_entry.serialize_header(V)
            if len(header) != hdr_entry.header_size(V):
                raise PakError(f"header size mismatch for {rel}")

            # The index entry keeps the real offset and the 57-based blocks.
            e._block_lengths = lengths
            e.compression_blocks = index_blocks
            e.compression_block_size = blk_size
            e.flags = FLAG_NONE
            e.offset = hdr_off
            data += header
            data += payload

        # ---- EncodedPakEntries + directory/hash tables ----
        encoded = bytearray()
        dirs: dict[str, list[tuple[str, int]]] = {}
        hash_pairs: list[tuple[int, int]] = []
        seen: dict[int, str] = {}
        collisions = 0
        for rel, e, _payload in prepared:
            loc = len(encoded)
            blob = encode_entry_index(e)
            if verify_roundtrip:
                back_e, back_p = decode_entry_index(blob, 0, V)
                if back_p != len(blob):
                    raise PakError(f"encoded entry length self-check failed for {rel}")
                if (back_e.offset != e.offset or back_e.size != e.size
                        or back_e.uncompressed_size != e.uncompressed_size
                        or back_e.method_index != e.method_index):
                    raise PakError(f"encoded entry roundtrip mismatch for {rel}")
            encoded += blob
            d, f = split_path(rel)
            dirs.setdefault(d, []).append((f, loc))
            h = hash_path(rel, seed)
            if h in seen:
                collisions += 1
            seen[h] = rel
            hash_pairs.append((h, loc))
        encoded = bytes(encoded)

        # ---- PathHashIndex = hash table + PrunedDirectoryIndex ----
        phi = bytearray()
        if write_phi:
            pairs = sorted(hash_pairs, key=lambda x: x[0])
            phi += w_int32(len(pairs))
            for h, loc in pairs:
                phi += struct.pack("<Qi", h, loc)
            phi += serialize_directory_index({})   # empty pruned index
        phi = bytes(phi)
        fdi = serialize_directory_index(dirs) if write_fdi else b""

        # ---- PrimaryIndex with placeholders ----
        prim = bytearray()
        prim += w_str(self.mount_point)
        prim += w_int32(len(prepared))
        prim += struct.pack("<Q", seed)
        prim += w_archive_bool(write_phi)
        phi_off_at = len(prim)
        if write_phi:
            prim += struct.pack("<qq", 0, 0) + b"\x00" * 20
        prim += w_archive_bool(write_fdi)
        fdi_off_at = len(prim)
        if write_fdi:
            prim += struct.pack("<qq", 0, 0) + b"\x00" * 20
        prim += w_int32(len(encoded))
        prim += encoded
        prim += w_int32(0)                        # NonEncodableEntriesNum

        index_offset = len(data)
        phi_offset = index_offset + len(prim)
        fdi_offset = phi_offset + len(phi)

        if write_phi:
            prim[phi_off_at:phi_off_at + 16] = struct.pack("<qq", phi_offset, len(phi))
            prim[phi_off_at + 16:phi_off_at + 36] = hashlib.sha1(phi).digest()
        if write_fdi:
            prim[fdi_off_at:fdi_off_at + 16] = struct.pack("<qq", fdi_offset, len(fdi))
            prim[fdi_off_at + 16:fdi_off_at + 36] = hashlib.sha1(fdi).digest()
        primary = bytes(prim)

        # ---- assemble ----
        # Footer layout (measured against real Ready or Not paks, footer = 205 bytes):
        #   bEncryptedIndex(1) + Magic(4) + Version(4) + IndexOffset(8) +
        #   IndexSize(8) + IndexHash(20) + methods(5*32 = 160)
        # = 205. Note: NO EncryptionKeyGuid. The engine's FPakInfo::Serialize
        # writes the guid only when Ar.IsSaving() or version >= 7, and reading it
        # back shifts every later field, so a guid must NOT be present here.
        out = bytearray(data)
        out += primary
        out += phi
        if write_fdi:
            out += fdi
        if write_guid:
            out += b"\x00" * 16                 # EncryptionKeyGuid (not used)
        out += struct.pack("<B", 0)             # bEncryptedIndex
        out += struct.pack("<I", MAGIC)
        out += struct.pack("<i", V)
        out += struct.pack("<qq", index_offset, len(primary))
        out += hashlib.sha1(primary).digest()
        for nm in self._method_table():
            out += nm.encode("ascii").ljust(COMPRESSION_METHOD_NAME_LEN, b"\x00")

        with open(out_path, "wb") as f:
            f.write(bytes(out))
        return {
            "out": out_path,
            "entries": len(prepared),
            "data_bytes": len(data),
            "index_offset": index_offset,
            "primary_bytes": len(primary),
            "phi_offset": phi_offset,
            "phi_bytes": len(phi),
            "fdi_offset": fdi_offset,
            "fdi_bytes": len(fdi),
            "total_bytes": len(out),
            "seed": seed,
            "hash_collisions": collisions,
            "directories": len(dirs),
        }


# ===========================================================================
# Reader
# ===========================================================================
def read_pak(path: str, *, strict: bool = True) -> "PakFile":
    return PakFile(path, strict=strict)


def read_pak_index(path: str, *, tail_size: int = 4096) -> "PakIndex":
    """只读目录索引（不碰数据区）。

    给游戏本体 pak 建官方资产清单时用：pakchunk0 有 24 GB，
    PakFile 会把它整个读进内存 —— 那不是慢，是直接爆内存。
    我们只需要挂载点 + 路径，所以读「尾部 4 KB + 索引区」就够了。
    """
    return PakIndex(path, tail_size=tail_size)


def decompress_payload(method: str, payload: bytes, block_lengths=(),
                       uncompressed_size: int = 0) -> bytes:
    """按压缩方式解开一个条目的载荷（多块的话逐块解）。

    ★ 目前只支持 Zlib。Oodle 是商业编解码器（RAD Game Tools），
      拿不到就不解 —— 调用方应当退回「原样搬运」。

    用途：某些模组用了游戏根本没编进去的压缩方式（实测 Hospital 地图模组
    用 Zlib，而本体全是 Oodle），读它的资产会失败。把载荷解开、以
    【不压缩】重新写进 pak，就能绕开这个解码器问题（代价是包变大）。
    """
    m = (method or "").strip().lower()
    if m == "zlib":
        import zlib
        lens = list(block_lengths) or [len(payload)]
        out = bytearray()
        off = 0
        for n in lens:
            out += zlib.decompress(payload[off:off + n])
            off += n
        if uncompressed_size and len(out) != uncompressed_size:
            raise PakError(f"解压后大小不符：{len(out)} != {uncompressed_size}")
        return bytes(out)
    if not m:
        return payload                    # 本来就是不压缩
    raise PakError(f"不支持的压缩方式：{method}（只有 Zlib 能解）")


class PakFile:
    """Read-only pak parser with index and data-area access."""

    def __init__(self, path: str, strict: bool = True):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        self.strict = strict
        self._read_footer()
        self._read_index()

    def _read_footer(self):
        """整份数据都在内存里，直接扫它自己的尾部。"""
        self._load_footer(self.data, 0, len(self.data))

    def _load_footer(self, win: bytes, win_base: int, file_size: int,
                     read_index=None) -> int:
        """在 win（文件尾部窗口）里定位 footer，并填好 pak 级字段。

        win_base:    win[0] 在整个文件里的绝对偏移
        file_size:   整个文件的字节数（偏移合法性用得到）
        read_index:  read_index(off, size) -> bytes，只用于索引 SHA1 自检。
                     不给就表示 win 里已经含有索引区（整份数据在内存的情形）。
        返回 footer 在 win 内的相对偏移。

        拆出来是为了让 PakIndex 只读「尾部 + 索引区」也能复用同一套校验，
        而不是把 24 GB 的本体 pak 整个读进内存。
        """
        d = win
        if read_index is None:
            def read_index(off, sz):          # noqa: F811
                return d[off - win_base:off - win_base + sz]
        found = None
        for i in range(len(d) - 4, max(0, len(d) - 4096), -1):
            if struct.unpack_from("<I", d, i)[0] != MAGIC:
                continue
            ver = struct.unpack_from("<i", d, i + 4)[0]
            if ver not in range(1, 13):
                continue
            idx_off, idx_sz = struct.unpack_from("<qq", d, i + 8)
            if idx_off < 0 or idx_sz < 0 or idx_off + idx_sz > file_size:
                continue
            if hashlib.sha1(read_index(idx_off, idx_sz)).digest() == d[i + 24:i + 44]:
                found = i
                break
        if found is None:
            raise PakError("no valid pak footer (magic + index SHA1 self-check failed)")
        self.footer_offset = win_base + found
        self.version = struct.unpack_from("<i", d, found + 4)[0]
        self.index_offset, self.index_size = struct.unpack_from("<qq", d, found + 8)
        self.index_hash = d[found + 24:found + 44]
        self.b_encrypted_index = d[found - 1]
        base = found + 44
        self.compression_methods = []
        for k in range(MAX_NUM_COMPRESSION_METHODS):
            m = d[base + k * COMPRESSION_METHOD_NAME_LEN:
                  base + (k + 1) * COMPRESSION_METHOD_NAME_LEN]
            self.compression_methods.append(m.split(b"\x00")[0].decode("ascii", "replace"))
        return found

    def _read_index(self, data: bytes | None = None, base: int = 0):
        """解析索引区。

        data/base: data[0] 在文件里的绝对偏移是 base。
                   PakFile 传整份数据（base=0）；PakIndex 只传索引区，
                   于是 phi/fdi 的偏移会被换算成「相对 data」，
                   这样 read_directory_index() 等下游代码两边都能直接用。

        ★ v10 之前是【老格式】：索引就是「挂载点 + 条目数 + 每条(路径, FPakEntry)」，
          没有 FPakEntryLocation / FDI / PHI。社区里那些很久没更新的模组常常是
          这种老格式（实测遇到一个 v3 + Zlib 的），所以必须单独走一条路读。
        """
        d = self.data if data is None else data
        if self.version < V_ENCODED_INDEX:
            self._read_legacy_index(d, base)
            return
        self.legacy_entries = None
        p = self.index_offset - base
        self.mount_point, p = r_str(d, p)
        self.num_entries = struct.unpack_from("<i", d, p)[0]
        p += 4
        self.path_hash_seed = struct.unpack_from("<Q", d, p)[0]
        p += 8

        # Each flag is 4 bytes (FArchive bool -> int32).
        self.has_phi = bool(struct.unpack_from("<I", d, p)[0])
        p += 4
        if self.has_phi:
            off, size = struct.unpack_from("<qq", d, p)
            self.phi = SecondaryIndex(off - base, size, d[p + 16:p + 36])
            p += 36
        else:
            self.phi = None

        self.has_fdi = bool(struct.unpack_from("<I", d, p)[0])
        p += 4
        if self.has_fdi:
            off, size = struct.unpack_from("<qq", d, p)
            self.fdi = SecondaryIndex(off - base, size, d[p + 16:p + 36])
            p += 36
        else:
            self.fdi = None

        n_enc = struct.unpack_from("<i", d, p)[0]
        p += 4
        if n_enc < 0 or p + n_enc > len(d):
            raise PakError(f"bad EncodedPakEntries length {n_enc}")
        self.encoded = d[p:p + n_enc]
        p += n_enc
        n_nonenc = struct.unpack_from("<i", d, p)[0]
        p += 4
        self.non_encodable = []
        for _ in range(n_nonenc):
            e, p = PakEntry.deserialize_header(d, p, self.version)
            self.non_encodable.append(e)
        self.index_parsed_end = p

        self.encoded_entries = []
        self.encoded_offsets: list[int] = []      # 每条在 encoded 区里的【真实】起始偏移
        q = 0
        while q < len(self.encoded):
            self.encoded_offsets.append(q)
            e, q = decode_entry_index(self.encoded, q, self.version)
            self.encoded_entries.append(e)

    def _read_legacy_index(self, d: bytes, base: int) -> None:
        """v1..v9 的老格式索引：挂载点 + 条目数 + 每条 (路径, FPakEntry)。

        v1 的条目里多一个 Timestamp(i64)；v3 起压缩条目带块表（块是
        「(起点, 终点)」两个 i64，不是起点+长度），标志位和块大小【总是】写。

        ★ 解析完必须正好停在索引区末尾：对不上就报错。老格式没有二级索引可以
          兜底，宁可明确说「读不了」，也不能拿半截索引去喂后面的判断。
        """
        p = self.index_offset - base
        limit = p + self.index_size
        self.legacy_entries: list[tuple[str, PakEntry]] = []
        self.has_phi = self.has_fdi = False
        self.phi = self.fdi = None
        self.encoded = b""
        self.encoded_entries = []
        self.encoded_offsets = []
        self.non_encodable = []
        # 老格式的 footer 里没有「压缩方式名」表（v8 才有），用的是内置枚举：
        # 0=None 1=Zlib 2=Gzip 3=Custom —— 正好对上 method_index 的 1 基下标。
        # v8/v9 的 footer 里已经有真表，别覆盖它。
        if self.version < V_FNAME_BASED_METHOD or not any(
                self.compression_methods):
            self.compression_methods = []
        try:
            self.mount_point, p = r_str(d, p)
            self.num_entries = struct.unpack_from("<i", d, p)[0]
            p += 4
            for _ in range(self.num_entries):
                rel, p = r_str(d, p)
                e, p = PakEntry.deserialize_header(d, p, self.version)
                if self.version <= V_INITIAL:
                    # v1 在 CompressionMethod 与 Hash 之间还有 Timestamp(i64)；
                    # deserialize_header 没读它，这里补上（v2 起就没有了）。
                    p += 8
                if e.compressed and e.compression_blocks:
                    # (起点, 终点) -> 每块长度；解压和多块搬运都要用
                    e._block_lengths = [max(0, en - st)
                                        for st, en in e.compression_blocks]
                    # 老格式的块偏移是【文件绝对偏移】。正常情况下块紧跟条目头，
                    # 直接切片就行；万一不连续（规范允许），记下来按块拼。
                    hdr_end = e.offset + e.header_size(self.version)
                    if e.compression_blocks[0][0] != hdr_end:
                        e._abs_blocks = list(e.compression_blocks)
                self.legacy_entries.append((rel, e))
        except (struct.error, ValueError, IndexError) as ex:
            raise PakError(
                f"老格式（v{self.version}）索引解析失败：{ex}。"
                f"如果这个 pak 是老工具打的、或者启用了索引加密，本工具暂时读不了") from ex
        if p != limit:
            raise PakError(
                f"老格式（v{self.version}）索引没解析完：停在 {p}，索引末尾在 "
                f"{limit}（差 {limit - p} 字节）—— 不敢拿半截索引下结论")
        # 只把【真正用到】的压缩方式列进表里（下标要对齐 method_index）：
        # 老格式的 method 是内置枚举，全列出来会让上层以为这个包什么都用了。
        # 槽位数按 v11 的规矩补齐（写 pak 时 footer 必须写满 5 个）。
        if self.version < V_FNAME_BASED_METHOD:
            used = {e.method_index for _rel, e in self.legacy_entries
                    if e.method_index}
            enum = ["Zlib", "Gzip", "Custom"]
            table = [enum[i - 1] if 1 <= i <= len(enum) else ""
                     for i in range(1, (max(used) if used else 0) + 1)]
            table += [""] * (MAX_NUM_COMPRESSION_METHODS - len(table))
            self.compression_methods = table
        self.index_parsed_end = p

    def location_map(self) -> dict[int, "PakEntry"]:
        """FPakEntryLocation -> 条目。

        ★ 必须用【解析时记下来的真实偏移】，不能拿 encode_entry_index 重新编码
          去凑长度。打包方的编码宽度可能和我们的编码器不一样（比如偏移/大小
          一律用 64 位），重新编码会让累积偏移整体漂移，FDI 里的 location 就
          大面积对不上 —— 路径恢复不出来，重新打包时会把条目整片丢掉。

          实测 Hospital 地图模组：真实偏移 730/730 全中，重新编码只有 117/730。

        编码条目的 location 是「在 encoded 区里的字节偏移」；
        非编码条目是 -(下标+1)。
        """
        out: dict[int, PakEntry] = {}
        for off, e in zip(self.encoded_offsets, self.encoded_entries):
            out[off] = e
        for i, e in enumerate(self.non_encodable):
            out[-i - 1] = e
        return out

    def paths_with_entries(self) -> dict[str, "PakEntry"]:
        """{挂载内相对路径: 条目} —— 按 FDI/PHI 把路径和条目接起来。

        老格式（v1..v9）没有 FDI/PHI：路径就在索引里，直接给。
        """
        if self.legacy_entries is not None:
            return dict(self.legacy_entries)
        idx = self.read_directory_index("fdi") or self.read_directory_index("phi")
        loc2e = self.location_map()
        out: dict[str, PakEntry] = {}
        for dname, files in idx.items():
            for fname, loc in files.items():
                e = loc2e.get(loc)
                if e is not None:
                    out[(dname + fname).lstrip("/")] = e
        return out

    def read_path_hash_index(self) -> list[tuple[int, int]]:
        """FPathHashIndex: int32 count + (u64 hash, i32 location) pairs."""
        if not self.phi:
            return []
        d = self.data
        p = self.phi.offset
        n = struct.unpack_from("<i", d, p)[0]
        p += 4
        return [struct.unpack_from("<Qi", d, p + i * 12) for i in range(n)]

    def read_directory_index(self, which: str = "fdi") -> dict[str, dict[str, int]]:
        """int32 NumDirectories + per dir {FString, int32 count, {FString, int32}}."""
        sec = self.phi if which == "phi" else self.fdi
        if not sec:
            return {}
        d = self.data
        p = sec.offset
        if which == "phi":
            n = struct.unpack_from("<i", d, p)[0]
            p += 4 + n * 12
        out: dict[str, dict[str, int]] = {}
        n_dirs = struct.unpack_from("<i", d, p)[0]
        p += 4
        for _ in range(n_dirs):
            name, p = r_str(d, p)
            n_files = struct.unpack_from("<i", d, p)[0]
            p += 4
            files: dict[str, int] = {}
            for _ in range(n_files):
                fname, p = r_str(d, p)
                loc = struct.unpack_from("<i", d, p)[0]
                p += 4
                files[fname] = loc
            out[name] = files
        return out

    def all_paths(self) -> list[str]:
        """Relative paths recovered from the FDI (or the pruned index in the PHI).

        Directory keys are stored as '/Content/Blueprints/' and the mount-relative
        path is the concatenation, so strip the single leading '/'.

        老格式（v1..v9）：索引里本来就有完整路径。
        """
        if self.legacy_entries is not None:
            return [rel for rel, _e in self.legacy_entries]
        idx = self.read_directory_index("fdi")
        if not idx:
            idx = self.read_directory_index("phi")
        out = []
        for dname, files in idx.items():
            for fname in files:
                out.append((dname + fname).lstrip("/"))
        return out

    def all_paths_with_sizes(self) -> list[tuple[str, int, int]]:
        """[(路径, 未压缩大小, 压缩后大小), ...] —— 全部来自索引，不碰数据区。

        ★ 为什么需要这两个大小：路径一致不代表内容一致。模组可能「故意」改过
          某个蓝图/数据表（比如血腥 mod 改 Blood_Standard），这时把它的两个大小
          和官方比一比就能看出来：
            · 两个都相同  -> 几乎一定是照抄官方（压缩器对相同输入是确定性的），
                             剥掉零风险
            · 任一不同    -> 内容不一样，保守当作「模组改过」，别剥
          实测：BP_RoNBloodPool 未压缩大小和官方一样（6,057），但压缩后
          2,260 vs 2,198 —— 只看未压缩大小会误判成「照抄」。

        拿不到条目的记 -1。大小是 FPakEntry 里的原始值。
        """
        if self.legacy_entries is not None:
            return [(rel, e.uncompressed_size, e.size)
                    for rel, e in self.legacy_entries]
        idx = self.read_directory_index("fdi")
        if not idx:
            idx = self.read_directory_index("phi")
        loc2entry = self.location_map()
        out = []
        for dname, files in idx.items():
            for fname, loc in files.items():
                e = loc2entry.get(loc)
                out.append(((dname + fname).lstrip("/"),
                            e.uncompressed_size if e is not None else -1,
                            e.size if e is not None else -1))
        return out

    def entry_physical_offset(self, e: PakEntry) -> int:
        """Physical position of the FPakEntry header for this entry.

        e.offset IS the header position: the engine does
        Seek(PakEntry.Offset) then Serialize(FPakEntry) (IPlatformFilePak.h:1882),
        and PakFile.cpp:1848 sets CompressedStart = BaseOffset + GetSerializedSize.
        The payload therefore starts at e.offset + header_size. Confirmed against
        the real sample: entry 0 header at 0, payload (UE package magic) at 73.
        """
        return e.offset

    def read_at(self, off: int, n: int) -> bytes:
        """读文件的任意区间（PakFile 整个文件都在内存里，直接切片）。"""
        return self.data[off:off + n] if off >= 0 else b""

    def read_entry_bytes(self, e: PakEntry) -> bytes:
        """Header + payload bytes for one entry."""
        pos = self.entry_physical_offset(e)
        return self.read_at(pos, e.header_size(self.version) + e.size)

    def payload_of(self, e: PakEntry) -> bytes:
        """Just the payload (compressed bytes when compressed).

        ★ 只有老格式里「块不紧跟条目头」的条目才需要按块拼（那时 _abs_blocks 里
          存的是文件的绝对偏移）。v11 索引里的块偏移是「57 基准」的相对值，
          绝不能拿来当文件偏移用 —— 那会读到别的条目的数据。
        """
        blocks = getattr(e, "_abs_blocks", None)
        if blocks:
            return b"".join(self.read_at(st, max(0, en - st))
                            for st, en in blocks)
        start = e.offset + e.header_size(self.version)
        return self.read_at(start, e.size)

    def locate_by_path(self, rel_path: str) -> PakEntry | None:
        """Look up an entry the way the engine does."""
        if self.has_phi:
            h = hash_path(rel_path, self.path_hash_seed)
            for hh, loc in self.read_path_hash_index():
                if hh == h:
                    return self._entry_at_location(loc)
            return None
        idx = self.read_directory_index("fdi")
        d, f = split_path(rel_path)
        loc = idx.get(d, {}).get(f)
        return self._entry_at_location(loc) if loc is not None else None

    def _entry_at_location(self, loc: int) -> PakEntry | None:
        if loc >= 0:
            for i, e in enumerate(self.encoded_entries):
                pass
            q = 0
            for e in self.encoded_entries:
                if q == loc:
                    return e
                q += len(encode_entry_index(e))
            return None
        i = -loc - 1
        if 0 <= i < len(self.non_encodable):
            return self.non_encodable[i]
        return None


class PakIndex(PakFile):
    """只读 pak 的【目录索引】，不加载数据区。

    ★ 为什么需要它：游戏本体 pakchunk0 有 24 GB。PakFile 是 f.read() 全量载入，
      在 32 GB 内存的机器上勉强能跑，但既慢又危险；而给官方资产清单建索引
      只需要「挂载点 + 路径 + 压缩方法」，全都在索引区里。

    对外接口与 PakFile 一致（mount_point / all_paths / read_directory_index /
    encoded_entries / version / compression_methods ...），差别只有两点：
      · self.data 只覆盖索引区（payload_of / read_entry_bytes 按需从文件里读）
      · 索引 SHA1 自检照做，损坏的 pak 一样会被拒绝
    """

    def read_at(self, off: int, n: int) -> bytes:
        """从文件里按需读一段 —— 索引区之外的数据（比如某条目的载荷）也读得到。

        ★ 以前这里继承 PakFile 的实现（切 self.data），但 PakIndex 的 self.data
          只有索引区，切出来是【静默截断的错字节】。20 GB 的本体 pak 不可能整个
          读进内存，所以必须按需 seek 读。
        """
        if off < 0 or n <= 0:
            return b""
        with open(self.path, "rb") as f:
            f.seek(off)
            return f.read(n)

    def __init__(self, path: str, strict: bool = True, tail_size: int = 4096):
        self.path = path
        self.strict = strict
        size = os.path.getsize(path)
        win_base = max(0, size - tail_size)

        def read_at(fh, off, n):
            fh.seek(off)
            return fh.read(n)

        with open(path, "rb") as fh:
            win = read_at(fh, win_base, tail_size)
            self._load_footer(win, win_base, size,
                              lambda off, n: read_at(fh, off, n))
            # 索引 = 主索引（index_offset..index_offset+index_size）
            #      + 紧随其后的两个二级索引（FPathHashIndex / FDirectoryIndex），
            #      一直到 footer 为止 —— fdi 的偏移在 index_size 之外。
            span = self.footer_offset - self.index_offset
            if span < self.index_size:
                raise PakError(f"索引区范围异常：index_offset={self.index_offset} "
                               f"footer={self.footer_offset}")
            idx = read_at(fh, self.index_offset, span)
        if len(idx) != span:
            raise PakError(f"索引区读不全：要 {span} 字节，实际 {len(idx)} 字节")
        self.data = idx
        self._read_index(idx, self.index_offset)

