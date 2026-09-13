#!/usr/bin/env python3
"""
ronassets.py - 从导出的 RoN 模组资产中恢复"这个模组改了什么"

原理：UE 包的 Name Table 里直接存放资产自身路径与所引用的对象路径。
      因此无需游戏本体即可列出模组覆盖清单（区别于剪枝 pak 的 PHI 哈希反查）。

用法:
    python tools/ronassets.py out
    python tools/ronassets.py out --json report.json
"""
from __future__ import annotations

import glob
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field, asdict

UE_PKG = 0x9E2A83C1


@dataclass
class AssetInfo:
    file: str
    size: int
    is_ue_package: bool
    package_path: str | None = None
    self_path: str | None = None
    references: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)


PATH_RE = re.compile(rb"(/(?:Game|Script|Engine|ReadyOrNot|Plugins)/[ -~]{2,200}?)\x00")
NAME_RE = re.compile(rb"([ -~]{3,120}?)\x00")


def ue_strings(blob: bytes) -> list[str]:
    """提取 UE 包内所有以 NUL 结尾的路径串。"""
    out = []
    for m in PATH_RE.finditer(blob):
        s = m.group(1).decode("ascii", "replace")
        out.append(s)
    return out


def all_strings(blob: bytes) -> list[str]:
    out = []
    for m in NAME_RE.finditer(blob):
        s = m.group(1).decode("ascii", "replace")
        if len(s) >= 3:
            out.append(s)
    return out


def analyze_file(path: str) -> AssetInfo:
    with open(path, "rb") as f:
        blob = f.read()
    info = AssetInfo(file=os.path.basename(path), size=len(blob),
                     is_ue_package=False)
    if len(blob) < 8:
        return info
    tag, = struct.unpack_from("<I", blob, 0)
    if tag != UE_PKG:
        return info
    info.is_ue_package = True

    paths = ue_strings(blob)
    if paths:
        # 第一个 /Game/... 串通常是包自身的路径
        info.package_path = paths[0]
        info.self_path = paths[0]
        info.references = [p for p in dict.fromkeys(paths[1:])]
    names = all_strings(blob)
    info.names = list(dict.fromkeys(names))
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

    ap = argparse.ArgumentParser(description="恢复 RoN 模组的覆盖清单")
    ap.add_argument("dir", help="导出的资产目录")
    ap.add_argument("--json", metavar="FILE")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, "*")))
    infos = [analyze_file(f) for f in files]

    pkgs = [i for i in infos if i.is_ue_package]
    print(f"文件总数      : {len(infos)}")
    print(f"合法 UE 包    : {len(pkgs)}")
    print()

    # 按包路径归类（uasset/uexp 会指向同一路径）
    by_path: dict[str, list[AssetInfo]] = {}
    for i in pkgs:
        if i.package_path:
            by_path.setdefault(i.package_path, []).append(i)

    print(f"唯一资产路径  : {len(by_path)}")
    print("=" * 74)
    for p in sorted(by_path):
        group = by_path[p]
        refs = sorted(set(r for g in group for r in g.references))
        print(f"\n{p}")
        print(f"    文件 {len(group)} 个, 引用 {len(refs)} 个")
        for r in refs[:12]:
            print(f"      -> {r}")
        if len(refs) > 12:
            print(f"      ... 其余 {len(refs)-12} 个")

    # 汇总引用，找出模组依赖的"外部资产"（最可能是被游戏更新破坏的）
    print()
    print("=" * 74)
    print("模组引用的全部外部路径（去重）:")
    allrefs: dict[str, int] = {}
    for i in pkgs:
        for r in i.references:
            allrefs[r] = allrefs.get(r, 0) + 1
    for r, n in sorted(allrefs.items(), key=lambda kv: -kv[1]):
        print(f"  [{n:>3}] {r}")
    print(f"\n共 {len(allrefs)} 个外部引用")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(i) for i in infos], f, ensure_ascii=False, indent=2)
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
