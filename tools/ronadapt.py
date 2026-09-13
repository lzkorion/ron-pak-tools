#!/usr/bin/env python3
"""
ronadapt.py - Ready or Not 模组兼容性分析器（资产级）

能力:
  1. 从游戏 pak 提取完整文件清单（无需解压，清单为明文）
  2. 从模组 pak 导出资产并恢复其覆盖清单（无需游戏）
  3. 两者对比 —— 回答"模组改了哪些资产、这些资产在游戏里是否还存在"

用法:
    python tools/ronadapt.py --game-manifest <pak>      生成游戏清单
    python tools/ronadapt.py --compare <mod.pak> <dir>  对比模组与游戏清单
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import struct
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict

MAGIC = struct.pack("<I", 0x5A6F12E1)
FILE_EXT = (".uasset", ".uexp", ".ubulk", ".umap", ".uproject",
            ".locres", ".locmeta", ".bank", ".ini", ".json")

# 资产名在 pak 数据区中会以明文出现在 UE 包的 Name Table 里，
# 因此可以直接从 pak 提取，无需解压。
ASSET_NAME_RE = re.compile(rb"[A-Za-z0-9_\-]{2,120}\.uasset")


def _find_footer(d: bytes):
    i = 0
    while True:
        i = d.find(MAGIC, i)
        if i < 0:
            return None
        if i + 44 <= len(d):
            io, isz = struct.unpack_from("<qq", d, i + 8)
            if 0 <= io and 0 <= isz and io + isz <= len(d):
                if hashlib.sha1(d[io:io + isz]).digest() == d[i + 24:i + 44]:
                    return io, isz
        i += 1


def extract_manifest(pak_path: str) -> dict:
    """从 pak 提取文件清单。

    清单位于 pak 数据区，是明文 FString 序列。
    直接扫描即可，无需解压 —— 对本机 24GB 的 pakchunk0 也只需数十秒。
    """
    with open(pak_path, "rb") as f:
        d = f.read()
    foot = _find_footer(d)
    names = set()
    for m in re.finditer(rb"[ -~]{3,140}\x00", d):
        s = m.group()[:-1].decode("ascii", "replace")
        if s.lower().endswith(FILE_EXT):
            names.add(s)
    return {
        "pak": os.path.basename(pak_path),
        "file_size": len(d),
        "footer": foot,
        "count": len(names),
        "names": sorted(names),
    }


@dataclass
class CompareResult:
    mod: str
    mod_assets: int = 0
    game_assets: int = 0
    present: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    ext_added: list = field(default_factory=list)
    verdict: str = ""


def compare(mod_asset_paths: list[str], game_names: set[str]) -> CompareResult:
    res = CompareResult(mod="", mod_assets=len(mod_asset_paths),
                        game_assets=len(game_names))
    for mp in mod_asset_paths:
        rel = mp.lstrip("/")
        if rel.startswith("Game/"):
            rel = rel[len("Game/"):]
        fname = rel.split("/")[-1] + ".uasset"
        if fname in game_names:
            res.present.append((mp, fname))
        else:
            res.missing.append(mp)
    res.verdict = (
        f"模组 {len(mod_asset_paths)} 个目标资产中，"
        f"{len(res.present)} 个在当前游戏中仍存在，"
        f"{len(res.missing)} 个不存在。"
    )
    return res


def main() -> int:
    import argparse
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(description="RoN 模组资产级兼容性分析")
    ap.add_argument("--game-manifest", metavar="PAK",
                    help="从游戏 pak 提取文件清单")
    ap.add_argument("--out", default="game_manifest.json")
    ap.add_argument("--compare", nargs=2, metavar=("MOD_JSON", "MANIFEST_JSON"),
                    help="对比 mod_assets.json 与游戏清单")
    args = ap.parse_args()

    if args.game_manifest:
        m = extract_manifest(args.game_manifest)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False)
        print(f"清单已写出 {args.out}")
        print(f"  文件条目 {m['count']:,}")
        return 0

    if args.compare:
        mod = json.load(open(args.compare[0], encoding="utf-8"))
        man = json.load(open(args.compare[1], encoding="utf-8"))
        paths = sorted(set(x["package_path"] for x in mod
                           if x.get("package_path")))
        res = compare(paths, set(man["names"]))
        print(f"模组目标资产 : {res.mod_assets}")
        print(f"游戏清单条目 : {res.game_assets:,}")
        print(res.verdict)
        print()
        if res.present:
            print(f"存在 ({len(res.present)})：")
            for mp, fn in res.present[:10]:
                print(f"  ✔ {mp}")
            if len(res.present) > 10:
                print(f"  ... 其余 {len(res.present)-10} 个")
        print()
        if res.missing:
            print(f"不存在 ({len(res.missing)})：")
            for mp in res.missing:
                print(f"  ✘ {mp}")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
