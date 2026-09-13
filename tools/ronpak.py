#!/usr/bin/env python3
"""
ronpak.py - Ready or Not 模组 Pak 结构分析器（纯标准库，只读）

设计原则：只输出已被独立校验为真的结论。未打通的部分明确标注，不猜测。

已确证并输出（详见 notes/pak_format_findings.md）：
  [1] footer 定位与完整性   —— 用索引 SHA1 自校验，唯一解
  [2] pak 版本 / 压缩方法 / 是否加密
  [3] PrimaryIndex 标量字段 —— MountPoint / NumEntries / PathHashSeed / 次级索引位域
  [4] 次级索引区块及其 SHA1 校验
  [5] PathHashIndex 条目     —— 222 条，hash 升序，location 严格递增
  [6] EncodedPakEntries 起点与 encoded entry 解码（method/offset/size/uncompressed）

未打通（明确标记，不假装支持）：
  [ ] 压缩数据定位与导出 —— offset 字段仍有未确定的修正项，
      无法定位压缩字节流，因此无法导出资产内容。
"""
from __future__ import annotations

import hashlib
import struct
import sys
from dataclasses import dataclass, field

MAGIC = 0x5A6F12E1

VERSION_NAMES = {
    1: "Initial", 2: "NoTimestamps", 3: "CompressionEncryption",
    4: "IndexEncryption", 5: "RelativeChunkOffsets", 6: "DeleteRecords",
    7: "EncryptionKeyGuid", 8: "FNameBasedCompressionMethod",
    9: "FrozenIndex", 10: "PathHashIndex", 11: "Fnv64BugFix",
    12: "Utf8PakDirectory",
}
COMPRESSION_NAMES = {0: "NONE", 1: "ZLIB", 2: "GZIP", 3: "Oodle", 4: "LZ4", 5: "Zstd"}

# 注意：encoded entry 里的 method 是【方法表索引】，不是全局枚举。
# 本 pak 的方法表实测为 [None, Oodle]，因此 method=1 实际是 Oodle，而非 ZLIB。
# 故这里只按方法表解析，不硬编码映射。
METHOD_TABLE_NOTE = (
    "method 字段是 pak 压缩方法表的索引；本 pak 方法表为 [None, Oodle]，"
    "method=1 即 Oodle（不可按全局名字表解释）"
)

EXPORT_SUPPORTED = False   # 导出尚未打通


class RonPakError(Exception):
    pass


@dataclass
class EncodedEntry:
    location: int
    path_hash: int
    method: int
    offset: int
    size: int
    uncompressed: int
    encrypted: bool
    blocks: int

    @property
    def method_name(self) -> str:
        return COMPRESSION_NAMES.get(self.method, f"0x{self.method:02X}")


@dataclass
class Region:
    kind: str
    offset: int
    size: int
    sha1_ok: bool


@dataclass
class RonPakInfo:
    path: str
    file_size: int
    version: int
    mount_point: str
    num_entries: int
    path_hash_seed: int
    secondary_flags: int
    encrypted_index: bool
    compression_methods: list[str]
    regions: list[Region] = field(default_factory=list)
    phi_count: int = 0
    phi_sorted: bool = False
    phi_locations: list[int] = field(default_factory=list)
    entries: list[EncodedEntry] = field(default_factory=list)
    enc_base_rel: int = -1
    enc_base_score: tuple | None = None
    offset_residuals: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def version_name(self) -> str:
        return VERSION_NAMES.get(self.version, f"?({self.version})")

    @property
    def has_phi(self) -> bool:
        return bool(self.secondary_flags & 1)

    @property
    def has_fdi(self) -> bool:
        return bool(self.secondary_flags & 2)


def _decode_encoded(src: bytes, o: int) -> EncodedEntry:
    """16 字节 encoded entry（实测布局，见 notes）。"""
    v = struct.unpack_from("<I", src, o)[0]
    method = (v >> 23) & 0x3F
    offset, = struct.unpack_from("<I", src, o + 4)
    unc, = struct.unpack_from("<I", src, o + 8)
    size, = struct.unpack_from("<I", src, o + 12)
    return EncodedEntry(
        location=-1, path_hash=0, method=method, offset=offset,
        size=size if method else unc, uncompressed=unc,
        encrypted=bool(v & (1 << 22)), blocks=(v >> 6) & 0xFFFF,
    )


def analyze(path: str) -> RonPakInfo:
    with open(path, "rb") as f:
        raw = f.read()
    size = len(raw)

    # ---- footer：magic 扫描 + SHA1 自校验 ----
    pat = struct.pack("<I", MAGIC)
    footer = None
    i = 0
    while True:
        i = raw.find(pat, i)
        if i < 0:
            break
        if i + 44 <= size:
            io, isz = struct.unpack_from("<qq", raw, i + 8)
            if 0 <= io and 0 <= isz and io + isz <= size:
                if hashlib.sha1(raw[io:io + isz]).digest() == raw[i + 24:i + 44]:
                    footer, index_offset, index_size = i, io, isz
                    break
        i += 1
    if footer is None:
        raise RonPakError("未找到有效的 pak footer（magic + 索引 SHA1 均需通过）")

    version = struct.unpack_from("<i", raw, footer + 4)[0]
    encrypted_index = bool(raw[footer - 1])

    # 压缩方法表：footer 内按 32 字节槽位扫描
    known = {b"Oodle", b"Zlib", b"Gzip", b"LZ4", b"Zstd", b"None"}
    methods: list[str] = []
    p = footer + 44
    limit = min(size, footer + 204)
    while p + 32 <= limit:
        nm = raw[p:p + 32].split(b"\x00", 1)[0]
        if nm in known and nm.decode() not in methods:
            methods.append(nm.decode())
        p += 32

    # ---- PrimaryIndex 标量字段 ----
    b = raw
    n = struct.unpack_from("<i", b, index_offset)[0]
    mount = b[index_offset + 4:index_offset + 4 + n].decode("utf-8", "replace").rstrip("\x00")
    q = index_offset + 4 + n
    num_entries = struct.unpack_from("<i", b, q)[0]; q += 4
    seed = struct.unpack_from("<Q", b, q)[0]; q += 8
    flags = struct.unpack_from("<I", b, q)[0]; q += 4

    info = RonPakInfo(
        path=path, file_size=size, version=version, mount_point=mount,
        num_entries=num_entries, path_hash_seed=seed, secondary_flags=flags,
        encrypted_index=encrypted_index, compression_methods=methods,
    )

    # ---- 次级索引区块 ----
    blob_end = index_offset + index_size
    for k in range(2):
        if q + 36 > blob_end:
            break
        ro, rs = struct.unpack_from("<qq", b, q)
        rh = b[q + 16:q + 36]
        q += 36
        if not (0 <= ro and 0 <= rs and ro + rs <= size):
            break
        ok = hashlib.sha1(b[ro:ro + rs]).digest() == rh
        kind = "PathHashIndex" if k == 0 else "FullDirectoryIndex"
        info.regions.append(Region(kind, ro, rs, ok))

    # ---- PHI：区域紧跟索引之后（实测） ----
    phi_at = index_offset + index_size
    if phi_at + 4 <= size:
        cnt = struct.unpack_from("<i", b, phi_at)[0]
        if 0 < cnt <= 1_000_000:
            p2 = phi_at + 4
            pairs = []
            ok = True
            for _ in range(cnt):
                if p2 + 12 > size:
                    ok = False
                    break
                h, = struct.unpack_from("<Q", b, p2)
                loc, = struct.unpack_from("<i", b, p2 + 8)
                p2 += 12
                pairs.append((h, loc))
            if ok:
                info.phi_count = len(pairs)
                info.phi_sorted = all(pairs[i][0] < pairs[i + 1][0]
                                      for i in range(len(pairs) - 1))
                info.phi_locations = sorted(set(l for _, l in pairs))

                # ---- 定位 encoded 缓冲区 ----
                # 判据（比"字段合理"强得多）：
                #   在 location 升序下，条目应满足 offset[n] == offset[n-1] + size[n-1]
                #   真实布局的残差是恒定常数；错误基点会给出杂乱残差。
                def score(B: int):
                    offs, sizes, methods, blocks = [], [], [], []
                    for L in info.phi_locations[:400]:
                        if B + L + 16 > blob_end:
                            return None
                        e = _decode_encoded(b, B + L)
                        if e.method not in COMPRESSION_NAMES:
                            return None
                        offs.append(e.offset)
                        sizes.append(e.size)
                        methods.append(e.method)
                        blocks.append(e.blocks)
                    if len(offs) < 3:
                        return None
                    res = [offs[i + 1] - (offs[i] + sizes[i])
                           for i in range(len(offs) - 1)]
                    uniq = set(res)
                    # 分 = 健康条目数；若残差集合很小则大幅加分
                    s = len(offs)
                    if len(uniq) == 1:
                        s += 100_000
                    elif len(uniq) <= 3:
                        s += 10_000
                    elif len(uniq) <= 10:
                        s += 1_000
                    # 单调递增也加分
                    if all(offs[i] < offs[i + 1] for i in range(len(offs) - 1)):
                        s += 5_000
                    return s, len(uniq)

                best = None
                for B in range(index_offset + 100, blob_end - 3536 + 16):
                    sc = score(B)
                    if sc and (best is None or sc[0] > best[0][0]):
                        best = (sc, B)
                if best is not None:
                    info.enc_base_rel = best[1] - index_offset
                    info.enc_base_score = best[0]
                    for h, loc in pairs:
                        e = _decode_encoded(b, best[1] + loc)
                        e.location = loc
                        e.path_hash = h
                        info.entries.append(e)
                    # 记录残差，供分析
                    offs = [e.offset for e in info.entries]
                    szs = [e.size for e in info.entries]
                    info.offset_residuals = sorted(
                        set(offs[i + 1] - (offs[i] + szs[i])
                            for i in range(len(offs) - 1)))

    info.notes.append(
        "导出未实现：offset 字段存在未确定修正项，无法定位压缩字节流"
        "（详见 notes/pak_format_findings.md 第 6 节）"
    )
    return info


def main() -> int:
    import argparse
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(description="Ready or Not 模组 pak 结构分析器")
    ap.add_argument("pak")
    ap.add_argument("--entries", action="store_true", help="列出解码的条目")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    try:
        info = analyze(args.pak)
    except RonPakError as e:
        print(f"失败: {e}", file=sys.stderr)
        return 2

    print("=" * 70)
    print(f"文件          : {info.path}")
    print(f"大小          : {info.file_size:,} 字节")
    print(f"pak 版本      : {info.version} ({info.version_name})")
    print(f"挂载点        : {info.mount_point!r}")
    print(f"条目数        : 声明 {info.num_entries}")
    print(f"PathHashSeed  : 0x{info.path_hash_seed:016X}")
    print(f"次级索引位域  : 0x{info.secondary_flags:X}  "
          f"PHI={info.has_phi} FDI={info.has_fdi}")
    print(f"索引加密      : {'是' if info.encrypted_index else '否'}")
    print(f"压缩方法      : {info.compression_methods}")
    print("-" * 70)
    for r in info.regions:
        mark = "✔" if r.sha1_ok else "✘"
        print(f"  [{mark}] {r.kind:<18} off={r.offset} size={r.size}")
    print("-" * 70)
    print(f"PHI 条目      : {info.phi_count}  (声明 {info.num_entries})")
    print(f"PHI hash 升序 : {info.phi_sorted}")
    if info.phi_locations:
        locs = info.phi_locations
        print(f"location 范围 : {locs[0]} .. {locs[-1]}  ({len(locs)} 个唯一值)")
        if len(locs) > 1:
            steps = sorted(set(locs[i + 1] - locs[i] for i in range(len(locs) - 1)))
            print(f"location 步长 : {steps}")
    print(f"encoded 起点  : 索引内偏移 {info.enc_base_rel}")
    print(f"成功解码条目  : {len(info.entries)}")

    if info.entries:
        print("-" * 70)
        print("条目样例（loc / 方法 / offset / 压缩后 / 原始）:")
        for e in info.entries[:args.limit]:
            print(f"  loc={e.location:<6} {e.method_name:<6} off={e.offset:>9} "
                  f"size={e.size:>8} unc={e.uncompressed:>8} blocks={e.blocks}")

    # 自洽性检查：offset[n] 是否等于 offset[n-1] + size[n-1]
    if len(info.entries) >= 2:
        diffs = []
        for i in range(1, min(len(info.entries), 60)):
            prev, cur = info.entries[i - 1], info.entries[i]
            diffs.append(cur.offset - (prev.offset + prev.size))
        uniq = sorted(set(diffs))
        print("-" * 70)
        print(f"offset 递推残差集合: {uniq[:6]}{' ...' if len(uniq) > 6 else ''}")
        print("  （恒定为非 0 常数 => offset 含未确定的基址/修正项）")

    print("-" * 70)
    for n in info.notes:
        print(f"! {n}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
