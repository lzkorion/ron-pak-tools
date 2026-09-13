#!/usr/bin/env python3
"""
ronunreal.py - 用官方 UnrealPak 处理 RoN 模组 pak（权威方案）

为什么需要它
    自己逆推 pak 格式在两处受阻：
      1. 多块(multi-block)条目的块表语义无法从数据逆推
      2. 路径需要靠明文扫描（能拿到清单，但拿不到精确的 offset/size/sha1）
    本机 G:\\UE_5.8 自带官方 UnrealPak.exe，可直接：
      - 列出全部条目（含精确 offset / size / SHA1 / 压缩方法）
      - 完整解包（含多块条目）
      - 重新打包（用于本地修复）

自动探测 UnrealPak 位置；找不到时给出明确提示，不静默失败。

用法
    python tools/ronunreal.py --check
    python tools/ronunreal.py --list  "mod.pak"
    python tools/ronunreal.py --extract "mod.pak" -o outdir
    python tools/ronunreal.py --analyze "mod.pak" --manifest game_manifest.json
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict

UNREALPAK_CANDIDATES = [
    r"G:\UE_5.8\Engine\Binaries\Win64\UnrealPak.exe",
    r"C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealPak.exe",
]
UNREALPAK_GLOBS = [
    r"G:\UE_*\Engine\Binaries\Win64\UnrealPak.exe",
    r"C:\Program Files\Epic Games\UE_*\Engine\Binaries\Win64\UnrealPak.exe",
    r"D:\Epic Games\UE_*\Engine\Binaries\Win64\UnrealPak.exe",
    r"E:\Epic Games\UE_*\Engine\Binaries\Win64\UnrealPak.exe",
]

ENTRY_RE = re.compile(
    r'^LogPakFile: Display: "(?P<path>[^"]+)"\s+'
    r'offset:\s*(?P<offset>\d+),\s*'
    r'size:\s*(?P<size>\d+)\s*bytes,\s*'
    r'sha1:\s*(?P<sha1>[0-9A-Fa-f]+),\s*'
    r'compression:\s*(?P<compression>\w+)')
MOUNT_RE = re.compile(r'with mount point "([^"]*)"')


def find_unrealpak() -> str | None:
    for p in UNREALPAK_CANDIDATES:
        if os.path.isfile(p):
            return p
    for g in UNREALPAK_GLOBS:
        hits = sorted(glob.glob(g))
        if hits:
            return hits[-1]
    # 兜底：在常见盘符的 UE_* 目录下扫
    for drive in ("C:", "D:", "E:", "F:", "G:"):
        hits = glob.glob(os.path.join(drive, "**", "UnrealPak.exe"),
                         recursive=True)
        hits = [h for h in hits if "Engine" in h]
        if hits:
            return hits[0]
    return None


@dataclass
class PakListEntry:
    path: str
    offset: int
    size: int
    sha1: str
    compression: str

    @property
    def ext(self) -> str:
        return os.path.splitext(self.path)[1].lower()


@dataclass
class PakListing:
    pak: str
    mount_point: str = ""
    entries: list = field(default_factory=list)
    total_size: int = 0
    compressed_size: int = 0

    def by_ext(self) -> dict:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.ext] = out.get(e.ext, 0) + 1
        return out

    @property
    def asset_stems(self) -> set:
        s = set()
        for e in self.entries:
            if e.ext in (".uasset", ".uexp", ".umap", ".ubulk", ".uptnl"):
                s.add(os.path.splitext(os.path.basename(e.path))[0])
        return s

    @property
    def game_paths(self) -> list:
        """带目录的完整相对路径（去掉扩展名），用于与游戏清单对比。"""
        out = []
        for e in self.entries:
            if e.ext != ".uasset":
                continue
            p = e.path
            p = re.sub(r"\.uasset$", "", p)
            out.append(p)
        return sorted(set(out))


def _run(pak: str, args: list, timeout: int = 1800) -> str:
    exe = find_unrealpak()
    if not exe:
        raise RuntimeError(
            "未找到 UnrealPak.exe。请安装 UE5.8 或修改 "
            "tools/ronunreal.py 里的 UNREALPAK_CANDIDATES。")
    cmd = [exe, pak] + args
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    return (proc.stdout or "") + (proc.stderr or "")


def list_pak(pak: str) -> PakListing:
    out = _run(pak, ["-List"])
    res = PakListing(pak=pak)
    m = MOUNT_RE.search(out)
    if m:
        res.mount_point = m.group(1)
    for line in out.splitlines():
        mm = ENTRY_RE.match(line.strip())
        if not mm:
            continue
        e = PakListEntry(
            path=mm.group("path"),
            offset=int(mm.group("offset")),
            size=int(mm.group("size")),
            sha1=mm.group("sha1"),
            compression=mm.group("compression"),
        )
        res.entries.append(e)
        res.total_size += e.size
    res.compressed_size = os.path.getsize(pak) if os.path.isfile(pak) else 0
    return res


def extract_pak(pak: str, outdir: str, timeout: int = 1800) -> tuple[int, int]:
    """解包。返回 (文件数, 总字节数)。

    ★ UnrealPak 会把输出路径里的非 ASCII 字符写成 '?'
      （日志里可见 "ready or not ????"），所以只要目标路径含非 ASCII，
      就先解到纯 ASCII 的临时目录，再搬到目标位置。
    """
    os.makedirs(outdir, exist_ok=True)
    # ★ 必须看【解析后的绝对路径】——工作目录本身可能含非 ASCII
    #   （本仓库路径含"模组管理"），UnrealPak 会把它写成 '?' 导致写入失败。
    abs_out = os.path.abspath(outdir)
    need_tmp = not abs_out.isascii()
    work = tempfile.mkdtemp(prefix="ronpak_") if need_tmp else outdir
    try:
        out = _run(pak, ["-Extract", work], timeout=timeout)
        if "Extracted" not in out:
            raise RuntimeError(f"解包未产生任何文件:\n{out[-2000:]}")
        if need_tmp:
            for item in os.listdir(work):
                src = os.path.join(work, item)
                dst = os.path.join(outdir, item)
                if os.path.isdir(src):
                    if os.path.isdir(dst):
                        shutil.rmtree(dst)
                    shutil.move(src, dst)
                else:
                    shutil.move(src, dst)
        cnt = total = 0
        for root, _dirs, files in os.walk(outdir):
            for f in files:
                cnt += 1
                total += os.path.getsize(os.path.join(root, f))
        return cnt, total
    finally:
        if need_tmp and os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)


def compare_with_manifest(listing: PakListing, manifest: str) -> dict:
    """与游戏清单对比，给出冗余判定。"""
    with open(manifest, encoding="utf-8") as f:
        man = json.load(f)
    game = set()
    for n in man["names"]:
        game.add(re.sub(r"\.(uasset|uexp|umap|ubulk|uptnl)$", "", n))

    mod = listing.asset_stems
    in_game = sorted(n for n in mod if n in game)
    not_in_game = sorted(n for n in mod if n not in game)
    tot = len(mod)
    ratio = (len(in_game) / tot) if tot else 0.0
    if tot == 0:
        verdict = "无法判定"
    elif len(in_game) == 0:
        verdict = "可用"
    elif len(not_in_game) == 0:
        verdict = "冗余"
    elif ratio >= 0.7:
        verdict = "高危冗余"
    else:
        verdict = "部分冲突"
    return {
        "mod_assets": tot,
        "in_game": in_game,
        "not_in_game": not_in_game,
        "overlap_ratio": ratio,
        "verdict": verdict,
    }


def main() -> int:
    import argparse
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(description="用官方 UnrealPak 处理 pak")
    ap.add_argument("pak", nargs="?")
    ap.add_argument("--check", action="store_true", help="探测 UnrealPak")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("-o", "--out")
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--manifest", default="game_manifest.json")
    ap.add_argument("--json", metavar="FILE")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    exe = find_unrealpak()
    if args.check or exe is None:
        print(f"UnrealPak: {exe or '未找到'}")
        if exe is None:
            print("请安装 UE5.8，或把 UnrealPak.exe 路径填入 "
                  "tools/ronunreal.py 的 UNREALPAK_CANDIDATES")
            return 2
        if args.check:
            return 0

    if not args.pak:
        ap.print_help()
        return 1
    if not os.path.isfile(args.pak):
        print(f"文件不存在: {args.pak}", file=sys.stderr)
        return 2

    listing = list_pak(args.pak)
    print(f"pak 文件    : {args.pak}")
    print(f"挂载点      : {listing.mount_point!r}")
    print(f"条目数      : {len(listing.entries):,}")
    print(f"条目 size 合计: {listing.total_size:,} 字节 "
          f"({listing.total_size/1048576:.1f} MiB)")
    print(f"pak 体积    : {listing.compressed_size:,} 字节 "
          f"({listing.compressed_size/1048576:.1f} MiB)")
    print(f"文件类型    : {listing.by_ext()}")

    if args.list:
        print("-" * 70)
        for e in listing.entries[:args.limit]:
            print(f"  off={e.offset:>10} size={e.size:>9} "
                  f"{e.compression:<7} {e.path}")
        if len(listing.entries) > args.limit:
            print(f"  ... 其余 {len(listing.entries)-args.limit} 条")

    if args.extract:
        out = args.out or "unpacked"
        cnt, total = extract_pak(args.pak, out)
        print(f"\n解包完成    : {cnt:,} 个文件, {total:,} 字节 "
              f"({total/1048576:.1f} MiB) -> {out}")

    if args.analyze:
        if not os.path.isfile(args.manifest):
            print(f"\n缺少游戏清单 {args.manifest}，无法对比", file=sys.stderr)
        else:
            r = compare_with_manifest(listing, args.manifest)
            print("-" * 70)
            print(f"模组资产    : {r['mod_assets']:,}")
            print(f"官方已有    : {len(r['in_game']):,}")
            print(f"官方缺失    : {len(r['not_in_game']):,}")
            print(f"重叠率      : {r['overlap_ratio']:.0%}")
            print(f"结论        : [{r['verdict']}]")
            if r["not_in_game"]:
                print(f"\n模组独有（前 {args.limit}）:")
                for n in r["not_in_game"][:args.limit]:
                    print(f"  + {n}")

    if args.json:
        data = asdict(listing)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n清单已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
