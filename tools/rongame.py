#!/usr/bin/env python3
"""
rongame.py - 从游戏 paks 建立 (路径 -> 哈希) 字典

为什么需要:
    模组 pak 是"剪枝包"，只有 PathHashIndex（哈希 -> 条目），无路径字符串。
    游戏自己的 pak 带 FullDirectoryIndex（完整路径表）。
    用它即可反查任意条目对应的真实文件名，进而正确配对 .uasset / .uexp。

设计: 全部用 seek 定位读取，不把 GB 级 pak 读进内存。

FDI 布局（UE5.8 SaveIndexInternal_DirectoryIndex）:
    int32 NumDirectories
    每个目录: FString DirName, int32 NumFiles
    每个文件: FString FileName, FPakEntryLocation(int32)
    （TMap 按 key 有序序列化）

用法:
    python tools/rongame.py --scan
    python tools/rongame.py --build game_paths.json
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import struct
import sys

MAGIC = struct.pack("<I", 0x5A6F12E1)
M64 = 0xFFFFFFFFFFFFFFFF
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x00000100000001B3

GAME_PAKS = (r"E:\SteamLibrary\steamapps\common\Ready Or Not"
             r"\ReadyOrNot\Content\Paks\*.pak")


def mem_fnv64(data: bytes, seed: int) -> int:
    x = (FNV_OFFSET + seed) & M64
    for b in data:
        x = (x * FNV_PRIME) & M64
        x ^= b
    return x


def hash_path(rel: str, seed: int) -> int:
    return mem_fnv64(rel.lower().encode("utf-16-le"), seed)


def read_footer(f) -> tuple | None:
    """从文件尾部向前找 magic，返回 (footer_off, index_off, index_size)。"""
    f.seek(0, os.SEEK_END)
    n = f.tell()
    win = min(n, 8192)
    f.seek(n - win)
    tail = f.read(win)
    i = len(tail)
    while True:
        i = tail.rfind(MAGIC, 0, i)
        if i < 0:
            return None
        off = n - win + i
        if off + 44 <= n:
            ver, = struct.unpack_from("<i", tail, i + 4)
            io, isz = struct.unpack_from("<qq", tail, i + 8)
            if 1 <= ver <= 12 and 0 <= io and 0 <= isz and io + isz <= n:
                # SHA1 校验索引
                f.seek(io)
                blob = f.read(isz)
                if hashlib.sha1(blob).digest() == tail[i + 24:i + 44]:
                    return off, io, isz, ver
        i = i - 1 if i > 0 else -1


def read_header(f, io: int, isz: int):
    """读 PrimaryIndex 头部，返回 (mount, num, seed, phi, fdi)。"""
    f.seek(io)
    head = f.read(min(isz, 4096))
    n, = struct.unpack_from("<i", head, 0)
    mount = head[4:4 + n].decode("utf-8", "replace").rstrip("\x00")
    q = 4 + n
    num, = struct.unpack_from("<i", head, q); q += 4
    seed, = struct.unpack_from("<Q", head, q); q += 8
    flags, = struct.unpack_from("<I", head, q); q += 4
    phi = fdi = None
    if flags & 1:
        ro, rs = struct.unpack_from("<qq", head, q)
        rh = head[q + 16:q + 36]
        q += 36
        phi = (ro, rs, rh)
    if flags & 2:
        ro, rs = struct.unpack_from("<qq", head, q)
        rh = head[q + 16:q + 36]
        q += 36
        fdi = (ro, rs, rh)
    return mount, num, seed, flags, phi, fdi


def parse_fdi(f, off: int, size: int, limit: int = 0):
    """流式解析 FDI，返回 [(相对路径, location)] 与消耗字节数。"""
    f.seek(off)
    # FDI 通常不大（MB 级），整块读入即可
    blob = f.read(size)
    nd, = struct.unpack_from("<i", blob, 0)
    p = 4
    out = []
    for _ in range(nd):
        if p + 4 > len(blob):
            break
        dn, = struct.unpack_from("<i", blob, p)
        p += 4
        if dn <= 0 or p + dn > len(blob):
            break
        dirname = blob[p:p + dn - 1].decode("utf-8", "replace")
        p += dn
        nf, = struct.unpack_from("<i", blob, p)
        p += 4
        for _ in range(nf):
            fn, = struct.unpack_from("<i", blob, p)
            p += 4
            if fn <= 0 or p + fn > len(blob):
                break
            fname = blob[p:p + fn - 1].decode("utf-8", "replace")
            p += fn
            loc, = struct.unpack_from("<i", blob, p)
            p += 4
            out.append((dirname + fname, loc))
            if limit and len(out) >= limit:
                return out, p
    return out, p


def main() -> int:
    import argparse
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(description="从游戏 paks 建立路径字典")
    ap.add_argument("--glob", default=GAME_PAKS)
    ap.add_argument("--build", metavar="OUT.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", help="只处理文件名包含该子串的 pak")
    args = ap.parse_args()

    paks = sorted(glob.glob(args.glob))
    if args.only:
        paks = [p for p in paks if args.only in os.path.basename(p)]
    if not paks:
        print(f"未找到 pak: {args.glob}")
        return 1

    all_paths: dict[str, int] = {}
    seeds: dict[str, int] = {}
    for pak in paks:
        name = os.path.basename(pak)
        sz = os.path.getsize(pak)
        with open(pak, "rb") as f:
            ft = read_footer(f)
            if not ft:
                print(f"  {name:<32} 无有效 footer")
                continue
            _, io, isz, ver = ft
            mount, num, seed, flags, phi, fdi = read_header(f, io, isz)
            if not fdi:
                print(f"  {name:<32} v{ver} 条目 {num:>7,}  无 FDI（剪枝包）")
                continue
            paths, consumed = parse_fdi(f, fdi[0], fdi[1], args.limit)
            ok = hashlib.sha1(f.read(0) or b"").digest()  # 占位，见下
            print(f"  {name:<32} v{ver} 条目 {num:>7,}  "
                  f"FDI {len(paths):>7,} 条  消耗 {consumed:,}/{fdi[1]:,}")
            seeds[name] = seed
            for p, _loc in paths:
                all_paths[p] = seed
            if args.limit:
                for p, _ in paths[:3]:
                    print(f"        {p}")

    print(f"\n共 {len(all_paths):,} 条唯一路径（相对挂载点）")
    if not all_paths:
        print("没有任何 pak 带 FDI —— 游戏全部是剪枝包，无法用此法恢复路径。")
        return 2

    if args.build:
        out = {p: f"{hash_path(p, s):016X}" for p, s in all_paths.items()}
        with open(args.build, "w", encoding="utf-8") as fh:
            json.dump({"count": len(out), "paths": out}, fh, ensure_ascii=False)
        print(f"已写入 {args.build}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
