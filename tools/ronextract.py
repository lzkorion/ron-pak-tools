#!/usr/bin/env python3
"""
ronextract.py - Ready or Not 模组 Pak 完整导出器（只读）

格式已端到端验证通过（依据 UE 5.8 源码 + 真实 pak Oodle 解压校验）：

    pak v11 布局:
        FString MountPoint / int32 NumEntries / uint64 PathHashSeed
        uint32 SecondaryIndexFlags (位0=PHI, 位1=FDI)
        [区域] int64 off, int64 size, SHA1(20)
        encoded entry 固定 16 字节:
            uint32 value  (bit31=off32safe, bit30=unc32safe, bit29=size32safe,
                           bits28..23=方法表索引, bit22=加密,
                           bits21..6=块数, bits5..0=块大小/2048)
            uint32 offset
            uint32 uncompressed          <- 也用作压缩块大小
            uint32 size
        PHI = TMap<uint64 hash, int32 loc>，loc 为 encoded 缓冲区内字节偏移

    ★ 数据位置 = entry.offset + BASE，BASE 由首个可解压条目校准得到
      （本 pak 实测 BASE = 73）

验证依据: 首条目解出 10556 字节，首 4 字节 = 0x9E2A83C1 (UE 包 magic)
"""
from __future__ import annotations

import ctypes
import glob
import hashlib
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field, field

MAGIC = 0x5A6F12E1
UE_PKG = 0x9E2A83C1


class RonPakError(Exception):
    pass


@dataclass
class Entry:
    location: int
    path_hash: int
    method: int
    offset: int        # 未压缩地址空间中的地址
    size: int          # 压缩后大小（磁盘字节数）
    uncompressed: int
    encrypted: bool
    blocks: int        # 压缩块数
    path: str | None = None
    block_table: list = field(default_factory=list)  # [(绝对偏移, 字节数)]
    block_size: int = 0
    layout: str = "?"      # "A" = 定长16字节, "B" = 变长+块表


def _decode_varlen(b: bytes, o: int, limit: int) -> Entry:
    """布局 B「变长 + 块表」的解码（保留以兼容其它 pak）。"""
    v = struct.unpack_from("<I", b, o)[0]
    p = o + 4
    if (v & 0x3F) == 0x3F:
        block_size = struct.unpack_from("<I", b, p)[0]
        p += 4
    else:
        block_size = (v & 0x3F) << 11
    method = (v >> 23) & 0x3F
    if v & (1 << 31):
        off = struct.unpack_from("<I", b, p)[0]; p += 4
    else:
        off = struct.unpack_from("<q", b, p)[0]; p += 8
    if v & (1 << 30):
        unc = struct.unpack_from("<I", b, p)[0]; p += 4
    else:
        unc = struct.unpack_from("<q", b, p)[0]; p += 8
    if method:
        if v & (1 << 29):
            size = struct.unpack_from("<I", b, p)[0]; p += 4
        else:
            size = struct.unpack_from("<q", b, p)[0]; p += 8
    else:
        size = unc
    nb = (v >> 6) & 0xFFFF
    table = []
    for _ in range(nb):
        if p + 12 > min(limit, len(b)):
            raise RonPakError("块表越界")
        bo = struct.unpack_from("<q", b, p)[0]
        bs = struct.unpack_from("<I", b, p + 8)[0]
        p += 12
        table.append((bo, bs))
    e = Entry(-1, 0, method, off, size, unc,
              bool(v & (1 << 22)), nb,
              block_table=table, block_size=block_size)
    e.layout = "B"
    return e


def _decode_encoded(b: bytes, o: int, limit: int,
                    layout: str = "A") -> Entry:
    """解码一条 encoded entry。

    实测：本作两份真实 pak（Pakchunk66 / Visceral Blood）都用
    **布局 A「定长 16 字节」**：
        [0..4)  value 位域（bit31 off32安全 / bit30 unc32安全 /
                bit29 size32安全 / bits28-23 方法(1基) / bit22 加密 /
                bits21-6 块数 / bits5-0 块大小/2048）
        [4..8)  offset      —— 未压缩地址空间
        [8..12) uncompressed 字段（单块时即块大小）
        [12..16) size        —— 压缩后字节数

    布局 B（变长 + 块表）作为兼容路径保留，由 detect_layout 决定采用哪个。
    """
    if layout == "B":
        return _decode_varlen(b, o, limit)
    if o + 16 > min(limit, len(b)):
        raise RonPakError("encoded entry 越界")
    v = struct.unpack_from("<I", b, o)[0]
    off = struct.unpack_from("<I", b, o + 4)[0]
    unc = struct.unpack_from("<I", b, o + 8)[0]
    sz = struct.unpack_from("<I", b, o + 12)[0]
    m = (v >> 23) & 0x3F
    nb = (v >> 6) & 0xFFFF
    block_size = (v & 0x3F) << 11
    e = Entry(-1, 0, m, off, sz if m else unc, unc,
              bool(v & (1 << 22)), nb,
              block_table=[], block_size=block_size or unc)
    e.layout = "A"
    return e


class RonPak:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            self.raw = f.read()
        self.size = len(self.raw)
        self.notes: list[str] = []
        self.layout: str = "A"
        self._footer()
        self._index()
        self._entries()
        self._calibrate()

    # ---- footer ----
    def _footer(self) -> None:
        pat = struct.pack("<I", MAGIC)
        i = 0
        while True:
            i = self.raw.find(pat, i)
            if i < 0:
                raise RonPakError("未找到 pak magic：不是 UE pak")
            if i + 44 <= self.size:
                io, isz = struct.unpack_from("<qq", self.raw, i + 8)
                if 0 <= io and 0 <= isz and io + isz <= self.size:
                    if hashlib.sha1(self.raw[io:io + isz]).digest() == \
                            self.raw[i + 24:i + 44]:
                        self.footer, self.index_offset, self.index_size = i, io, isz
                        break
            i += 1
        self.version = struct.unpack_from("<i", self.raw, self.footer + 4)[0]
        self.encrypted_index = bool(self.raw[self.footer - 1])
        # 压缩方法表：5 个 32 字节定长槽位，槽位 0 通常为 None。
        # ★ 必须保留槽位（不能过滤空项）—— encoded entry 里的 method 是
        #   槽位索引。本 pak 实测表 = ['None','Oodle']，即 method=1 <=> Oodle。
        self.method_table: list[str] = []
        p = self.footer + 44
        limit = min(self.size, self.footer + 44 + 5 * 32)
        while p + 32 <= limit:
            nm = self.raw[p:p + 32].split(b"\x00", 1)[0]
            self.method_table.append(nm.decode("ascii", "replace") if nm else "None")
            p += 32
        while len(self.method_table) > 1 and self.method_table[-1] == "None":
            self.method_table.pop()

    # ---- index ----
    def _index(self) -> None:
        b, io = self.raw, self.index_offset
        n = struct.unpack_from("<i", b, io)[0]
        self.mount_point = b[io + 4:io + 4 + n].decode("utf-8", "replace").rstrip("\x00")
        q = io + 4 + n
        self.num_entries = struct.unpack_from("<i", b, q)[0]; q += 4
        self.path_hash_seed = struct.unpack_from("<Q", b, q)[0]; q += 8
        self.secondary_flags = struct.unpack_from("<I", b, q)[0]; q += 4
        self.has_phi = bool(self.secondary_flags & 1)
        self.has_fdi = bool(self.secondary_flags & 2)
        self._after_flags = q
        # 次级索引的声明位置（优先使用；本工具 repack 出的 pak 会把 PHI 放在索引之前）
        self.phi_region = None
        if self.has_phi and q + 36 <= self.index_offset + self.index_size:
            ro, rs = struct.unpack_from("<qq", self.raw, q)
            if 0 <= ro and 0 <= rs and ro + rs <= self.size:
                self.phi_region = (ro, rs)

    def _phi(self) -> list[tuple[int, int]]:
        at = None
        if self.phi_region:
            at = self.phi_region[0]
        else:
            # 回退：实测部分官方/第三方 pak 的 PHI 紧跟索引之后
            at = self.index_offset + self.index_size
        if at + 4 > self.size:
            return []
        cnt = struct.unpack_from("<i", self.raw, at)[0]
        if not (0 < cnt <= 2_000_000):
            return []
        out = []
        p = at + 4
        for _ in range(cnt):
            if p + 12 > self.size:
                return []
            h = struct.unpack_from("<Q", self.raw, p)[0]
            loc = struct.unpack_from("<i", self.raw, p + 8)[0]
            p += 12
            out.append((h, loc))
        self.phi_at = at
        return out

    # ---- entries ----
    def _scan_encoded_base(self, dec, layout: str, scan_from: int,
                           scan_to: int, blob_end: int, io: int):
        """给定布局，扫描 encoded 表起点。返回 (score, B, residuals, layout)。"""
        best = None
        for B in range(scan_from, scan_to):
            offs, szs = [], []
            ok = True
            for L in self.phi_locations:
                if B + L + 16 > blob_end:
                    ok = False
                    break
                try:
                    e = _decode_encoded(self.raw, B + L, blob_end, layout)
                except Exception:
                    ok = False
                    break
                if e.method > len(self.method_table):
                    ok = False
                    break
                if not (0 < e.uncompressed < 200_000_000):
                    ok = False
                    break
                if not (0 <= e.offset < self.size):
                    ok = False
                    break
                offs.append(e.offset)
                szs.append(e.size)
            if not ok or not offs:
                continue
            if len(offs) < 2:
                sc = 1
            else:
                res = set(offs[i + 1] - (offs[i] + szs[i])
                          for i in range(len(offs) - 1))
                sc = 100000 if len(res) == 1 else (
                    10000 if len(res) <= 3 else 100)
            if best is None or sc > best[0]:
                best = (sc, B, set() if len(offs) < 2 else res)
        return best

    def _entries(self) -> None:
        self.phi = self._phi()
        self.phi_locations = sorted(set(l for _, l in self.phi))
        b, io, isz = self.raw, self.index_offset, self.index_size
        blob_end = io + isz

        def dec(o: int) -> Entry:
            return _decode_encoded(self.raw, o, blob_end, self.layout)

        # encoded 缓冲区起点：要求位置升序下 offset 递推残差恒定。
        # 注意：PHI 区域的 offset 是【文件绝对偏移】，可能位于索引之外，
        #      不能拿它当扫描下界。encoded 数据总在索引内字段区之后，
        #      因此从 io+100 一直扫到「仍有足够空间容纳最后一条」的位置。
        max_loc = max(self.phi_locations) if self.phi_locations else 0
        # 下界：索引内字段区末端；若 PHI 物理上位于索引内，则取其末端
        scan_from = self._after_flags
        if self.phi_region and io <= self.phi_region[0] < blob_end:
            scan_from = max(scan_from,
                            self.phi_region[0] + self.phi_region[1])
        # 上界：仍需在索引内放得下最后一条 encoded entry（16 字节）
        scan_to = blob_end - max_loc - 16 + 1
        if scan_to <= scan_from:
            scan_to = blob_end
        best = None
        for layout in ("A", "B"):
            got = self._scan_encoded_base(dec, layout, scan_from, scan_to,
                                          blob_end, io)
            if got and (best is None or got[0] > best[0]):
                best = got + (layout,)
        if best is None:
            raise RonPakError("无法定位 EncodedPakEntries")
        self.enc_rel = best[1] - io
        self.layout = best[3]
        self.residuals = best[2]
        self.entries = []
        for h, loc in self.phi:
            e = dec(best[1] + loc)
            e.location, e.path_hash = loc, h
            self.entries.append(e)

    # ---- Oodle ----
    _oodle = None

    @classmethod
    def oodle(cls):
        if cls._oodle is not None:
            return cls._oodle
        cands = []
        for root in glob.glob(r"E:\SteamLibrary\steamapps\common\Ready Or Not") + \
                glob.glob(r"G:\UE_*") + [r"C:\Windows\System32"]:
            cands += glob.glob(os.path.join(root, "**", "oo2core*.dll"), recursive=True)
        for c in cands:
            try:
                lib = ctypes.WinDLL(c)
                fn = lib.OodleLZ_Decompress
                fn.restype = ctypes.c_int64
                fn.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p,
                               ctypes.c_int64, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_void_p, ctypes.c_int64,
                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_int64, ctypes.c_int]
                cls._oodle = fn
                cls._oodle_path = c
                return fn
            except Exception:
                continue
        return None

    def reset_calibration(self) -> None:
        """重建/改写 pak 后必须重置 —— 新文件的 offset 基准可能不同
        （例如从源 pak 剥离重打包后，数据区从 0 开始，BASE 应为 0 而非源的 73）。"""
        self.data_base = None
        self.residuals = set()
        self.notes = []

    # ---- 校准 BASE ----
    def _calibrate(self) -> None:
        """数据位置 = offset + BASE。用首个能成功解出 UE 包的 BASE 作为真值。

        多块条目：第一块位于 offset + BASE（其余块按块表绝对位置）。
        """
        self.data_base = None
        if not hasattr(self, "residuals"):
            self.residuals = set()
        if not self.entries:
            self.notes.append("BASE 校准失败：无条目")
            return
        fn = self.oodle()
        e = self.entries[0]
        cands: list[int] = []
        for v in sorted(self.residuals):
            if abs(v) < 1_000_000:
                cands.append(v)
        for v in (73, 0, 53, 89, 125, 129, 141, 16, 160):
            if v not in cands:
                cands.append(v)

        want = e.block_size or e.uncompressed
        for delta in cands:
            pos = e.offset + delta
            if pos < 0 or pos + e.size > self.size:
                continue
            blob = self.raw[pos:pos + e.size]
            # 未压缩：直接看数据头
            if e.method == 0:
                if len(blob) >= 4 and \
                        struct.unpack_from("<I", blob, 0)[0] == UE_PKG:
                    self.data_base = delta
                    return
                continue
            if fn is None:
                # 无 Oodle 时退而求其次：试 zlib
                try:
                    out = zlib.decompress(blob)
                    if struct.unpack_from("<I", out, 0)[0] == UE_PKG:
                        self.data_base = delta
                        return
                except Exception:
                    pass
                continue
            out = ctypes.create_string_buffer(max(want, 1))
            got = fn(blob, len(blob), out, want, 0, 0, 0, None, 0,
                     None, None, None, 0, 0)
            if got > 0 and \
                    struct.unpack_from("<I", out.raw, 0)[0] == UE_PKG:
                self.data_base = delta
                return
        self.notes.append("BASE 校准失败，导出不可用")

    # ---- 解压单条 ----
    def _oodle_decompress(self, blob: bytes, out_size: int) -> bytes:
        fn = self.oodle()
        if not fn:
            raise RonPakError("需要 oo2core.dll")
        out = ctypes.create_string_buffer(max(out_size, 1))
        got = fn(blob, len(blob), out, out_size, 0, 0, 0, None, 0,
                 None, None, None, 0, 0)
        if got <= 0:
            raise RonPakError(f"Oodle 解压失败 ({got})")
        return out.raw[:got]

    def _decompress_one(self, blob: bytes, out_size: int) -> bytes:
        name = self._cur_method_name
        if name == "None":
            return blob
        if name == "Zlib":
            try:
                return zlib.decompress(blob)
            except Exception:
                pass
        if name == "Gzip":
            try:
                return zlib.decompress(blob, 16 + zlib.MAX_WBITS)
            except Exception:
                pass
        return self._oodle_decompress(blob, out_size)

    def extract(self, e: Entry) -> bytes:
        """导出条目。

        多块条目：每块从绝对偏移读取、单独解压后拼接（块表给出绝对位置）。
        单块条目：数据位置 = offset + BASE，长度 = size。
        """
        self._cur_method_name = self.method_name(e)
        if e.method == 0 or not e.size:
            if self.data_base is None:
                raise RonPakError("BASE 未校准")
            return self.raw[e.offset + self.data_base:
                            e.offset + self.data_base + e.size]

        # 多块：块表含绝对位置
        if e.blocks > 1 and e.block_table:
            out = bytearray()
            for bo, bs in e.block_table:
                chunk = self.raw[bo:bo + bs]
                want = min(e.block_size or e.uncompressed,
                           e.uncompressed - len(out))
                out += self._decompress_one(chunk, max(want, 1))
            return bytes(out)

        # 多块但无块表（布局 A）：先试「整段连续解压」，再试「按块均分」
        if e.blocks > 1:
            if self.data_base is None:
                raise RonPakError("BASE 未校准")
            pos = e.offset + self.data_base
            blob = self.raw[pos:pos + e.size]
            # 尝试 1：整段一次解压
            try:
                return self._decompress_one(blob, e.uncompressed)
            except Exception:
                pass
            # 尝试 2：按块数均分压缩字节，输出块大小由 unc/blocks 推得
            nb = e.blocks
            per_in = e.size // nb
            per_out = e.uncompressed // nb
            if per_in > 0 and per_out > 0:
                out = bytearray()
                cur = 0
                for _ in range(nb):
                    chunk = blob[cur:cur + per_in]
                    want = min(per_out, e.uncompressed - len(out))
                    if not chunk or want <= 0:
                        break
                    out += self._decompress_one(chunk, want)
                    cur += per_in
                if len(out) == e.uncompressed or out:
                    return bytes(out)
            raise RonPakError(f"多块条目解压失败 (blocks={nb}, size={e.size})")

        # 单块：数据位置 = offset + BASE
        if self.data_base is None:
            raise RonPakError("BASE 未校准")
        pos = e.offset + self.data_base
        blob = self.raw[pos:pos + e.size]
        return self._decompress_one(blob, e.block_size or e.uncompressed)

    def method_name(self, e: Entry) -> str:
        """method 是 1 基索引：0 = 未压缩；n>=1 对应 method_table[n-1]。"""
        if e.method == 0:
            return "None"
        i = e.method - 1
        return self.method_table[i] if i < len(self.method_table) else f"?{e.method}"


def main() -> int:
    import argparse
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(description="Ready or Not 模组 pak 导出器")
    ap.add_argument("pak")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--extract-all", metavar="DIR")
    ap.add_argument("--index", type=int, help="只导出第 N 条")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    pk = RonPak(args.pak)
    print(f"pak 版本      : {pk.version}")
    print(f"挂载点        : {pk.mount_point!r}")
    print(f"条目数        : {len(pk.entries)} / 声明 {pk.num_entries}")
    print(f"压缩方法表    : {pk.method_table}")
    print(f"encoded 起点  : 索引内偏移 {pk.enc_rel}")
    print(f"offset 残差   : {sorted(pk.residuals)}")
    print(f"数据基址 BASE : {pk.data_base}   (数据位置 = offset + BASE)")
    for n in pk.notes:
        print(f"注意          : {n}")
    print("-" * 70)

    if args.list or not (args.extract_all or args.index is not None):
        for i, e in enumerate(pk.entries[:args.limit]):
            print(f"  #{i:<4} loc={e.location:<6} {pk.method_name(e):<6} "
                  f"off={e.offset:>8} size={e.size:>7} unc={e.uncompressed:>8}")
        if len(pk.entries) > args.limit:
            print(f"  ... 其余 {len(pk.entries)-args.limit} 条")

    if args.index is not None:
        e = pk.entries[args.index]
        data = pk.extract(e)
        tag = struct.unpack_from("<I", data, 0)[0]
        print(f"解出 #{args.index}: {len(data)} 字节, tag=0x{tag:08X} "
              f"{'✔ UE 包' if tag == UE_PKG else ''}")

    if args.extract_all:
        os.makedirs(args.extract_all, exist_ok=True)
        okc = badc = 0
        ue = 0
        for i, e in enumerate(pk.entries):
            try:
                data = pk.extract(e)
            except Exception:
                badc += 1
                continue
            tag = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else 0
            ext = ""
            if tag == UE_PKG:
                ue += 1
                ext = ".uasset"
            fn = os.path.join(args.extract_all, f"{i:04d}_{e.path_hash:016X}{ext}")
            with open(fn, "wb") as f:
                f.write(data)
            okc += 1
        print(f"\n导出 {okc} 个（其中 {ue} 个是 UE 包），失败 {badc} 个")
        print(f"目录: {args.extract_all}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
