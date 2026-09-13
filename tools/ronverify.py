#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ronverify.py —— 对比「原模组」与「转换后」的 pak，找出可能出问题的地方。

用法:
    python tools/ronverify.py "原模组.pak" "converted\\原模组.pak"

检查项（都是会真的导致「装了没效果 / 崩溃」的原因）：
  1. 条目数 / 路径差异：谁被剥了、谁留下了
  2. 孤儿：留下了 .uexp/.ubulk/.bak，但对应的 .uasset 被剥了
  3. 缺件：留下了 .uasset，但它的 .uexp/.ubulk 被剥了
  4. 写出的 pak 能否被官方 UnrealPak 读取（-List / -Test）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pakfmt as P
import ronconvert as RC

UP = r"G:\UE_5.8\Engine\Binaries\Win64\UnrealPak.exe"


def paths_of(pak_path: str) -> tuple[str, list[str]]:
    """(挂载点, [挂载内相对路径...])。

    用 PakFile.paths_with_entries()（基于解析时记下的真实偏移）——
    不要自己拿 encode_entry_index 重新编码去凑偏移，打包方编码宽度不同时会漂移。
    """
    pk = P.read_pak(pak_path)
    return pk.mount_point, list(pk.paths_with_entries())


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    for p in (src, dst):
        if not os.path.isfile(p):
            print(f"✘ 找不到文件：{p}")
            return 1

    m1, a = paths_of(src)
    m2, b = paths_of(dst)
    sa, sb = set(a), set(b)
    dropped = sorted(sa - sb)
    added = sorted(sb - sa)

    print(f"原模组    : {src}")
    print(f"            {len(a)} 条  挂载点 {m1!r}")
    print(f"转换后    : {dst}")
    print(f"            {len(b)} 条  挂载点 {m2!r}")
    print()
    if m1 != m2:
        print(f"⚠ 挂载点不一致！原 {m1!r} → 新 {m2!r}")
    print(f"被剥掉    : {len(dropped)} 条")
    print(f"新增      : {len(added)} 条（正常应为 0）")

    # ---- 1. 被剥掉的清单 ----
    if dropped:
        print("\n--- 被剥掉的条目 ---")
        for r in dropped[:40]:
            print(f"   - {r}")
        if len(dropped) > 40:
            print(f"   ... 其余 {len(dropped)-40} 条")

    # ---- 2. 孤儿检查 ----
    kept = sb
    orphans = []
    for r in sorted(kept):
        stem = RC.asset_stem(r)
        if r == stem:
            continue                       # 不是可识别资产
        # 该资产的包文件在不在？
        for pkg in (".uasset", ".umap"):
            owner = stem + pkg
            if owner in kept:
                break
        else:
            # 没有包文件，只有派生/备份 → 孤儿
            # 但如果这个 stem 本来就没有包文件（例如只有 .ini），跳过
            if any((stem + x) in sa for x in (".uasset", ".umap")):
                orphans.append(r)
    print(f"\n--- 孤儿（留下派生文件但包文件被剥了）: {len(orphans)} ---")
    for r in orphans[:30]:
        print(f"   ! {r}")
        print(f"     缺少 {RC.asset_stem(r)}.uasset/.umap")
    if len(orphans) > 30:
        print(f"   ... 其余 {len(orphans)-30} 条")

    # ---- 3. 缺件检查 ----
    missing = []
    for r in sorted(kept):
        low = r.lower()
        if not low.endswith((".uasset", ".umap")):
            continue
        stem = r[:r.rfind(".")]
        for side in (".uexp", ".ubulk"):
            if (stem + side) in sa and (stem + side) not in kept:
                missing.append((r, stem + side))
    print(f"\n--- 缺件（留下包文件但派生文件被剥了）: {len(missing)} ---")
    for owner, side in missing[:20]:
        print(f"   ! {owner}")
        print(f"     缺少 {side}")

    # ---- 4. 官方工具复核 ----
    if os.path.isfile(UP):
        print(f"\n--- 官方 UnrealPak 复核 ---")
        tmp = None
        target = os.path.abspath(dst)
        if not all(ord(c) < 128 for c in target):
            base = r"C:\ronwork" if os.path.isdir(r"C:\ronwork") else None
            tmp = os.path.join(base or os.environ.get("TEMP", "."), "ronverify")
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp, exist_ok=True)
            target = os.path.join(tmp, "check.pak")
            shutil.copy2(dst, target)
        for flag in ("-List", "-Test"):
            p = subprocess.run([UP, target, flag], capture_output=True, timeout=900)
            txt = (p.stdout or b"").decode("utf-8", "replace")
            extra = ""
            if flag == "-List":
                import re
                n = len(re.findall(r'Display: "(.+?)" offset: \d+, size: \d+ bytes', txt))
                extra = f"（列出 {n} 条）"
            print(f"   {flag:8s} rc={p.returncode} {extra}")
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        print("\n（未找到 UnrealPak，跳过官方复核）")

    print()
    bad = bool(orphans or missing or added or m1 != m2)
    print("=== 结论 ===")
    if bad:
        print("   ✘ 发现问题（上面带 ! 的项会导致模组失效或崩溃）")
    else:
        print("   ✔ 未发现结构性问题")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
