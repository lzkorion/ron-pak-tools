#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「资产整组剥离」：.bak 变体必须跟着一起剥，不能留孤儿。"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)
import pakfmt as P
import ronconvert as RC
import fixtures as FX

WORK = os.path.join(HERE, "_work", "grouping")
fails = []


def check(cond, label, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label
          + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    # 一个冲突型资产，带全套变体（这就是之前会留孤儿的情形）
    stem = "Blueprints/Items/WeaponsRevised/BP_WithBak"
    files = {
        f"{stem}.uasset": b"a" * 100,
        f"{stem}.uasset.bak": b"b" * 100,
        f"{stem}.uexp": b"c" * 80,
        f"{stem}.uexp.bak": b"d" * 80,
        f"{stem}.ubulk": b"e" * 60,
    }
    # 一个资源替换型资产（必须完整保留，包括 .bak）
    keep_stem = "Textures/Blood/T_KeepMe"
    files.update({
        f"{keep_stem}.uasset": b"k" * 50,
        f"{keep_stem}.uasset.bak": b"l" * 50,
        f"{keep_stem}.uexp": b"m" * 40,
    })
    # 一个独有资产（官方没有）
    uniq = "Blueprints/Items/SampleMod/BP_Unique"
    files.update({f"{uniq}.uasset": b"u" * 30, f"{uniq}.uexp": b"v" * 20})

    pak = FX.make_pak(os.path.join(WORK, "GroupingTest_P.pak"), files)

    # 官方清单：冲突型 + 贴图型都算「官方已有」——必须是【全路径】，
    # 否则默认策略（只信路径一致）一条都不会剥。
    # 大小也要给：模组这份就是【照抄官方】，所以该剥。
    official, sizes = FX.paths_and_sizes({
        f"{stem}.uasset": files[f"{stem}.uasset"],
        f"{keep_stem}.uasset": files[f"{keep_stem}.uasset"],
    })

    d = RC.diagnose(pak, FX.make_stub_official(official, sizes), verbose=False)
    print(f"  条目 {d.total_entries}  剥离 {d.dropped}  保留 {d.kept}")
    print(f"  被剥: {sorted(d._drop)}")

    dropped = set(d._drop)
    kept = {e.rel for e in d._elist} - dropped

    print("\n--- 冲突型资产的【全部】变体都应被剥 ---")
    for rel in files:
        if rel.startswith(stem):
            check(rel in dropped, f"已剥: {rel}")

    print("\n--- 资源替换型资产的【全部】变体都应保留 ---")
    for rel in files:
        if rel.startswith(keep_stem):
            check(rel in kept, f"保留: {rel}")

    print("\n--- 独有资产应保留 ---")
    for rel in files:
        if rel.startswith(uniq):
            check(rel in kept, f"保留: {rel}")

    print("\n--- 无孤儿：留下的派生文件都有对应的包文件 ---")
    orphans = []
    for rel in sorted(kept):
        s = RC.asset_stem(rel)
        if s == rel:
            continue
        if not any((s + x) in kept for x in (".uasset", ".umap")):
            orphans.append(rel)
    check(not orphans, f"孤儿数 = {len(orphans)}", str(orphans) if orphans else "")

    # 实际转换并复核
    print("\n--- 实际转换 ---")
    out = os.path.join(WORK, "out")
    RC.convert(d, out, verify=False)
    chk = P.read_pak(d.out_path)
    got = set(chk.all_paths())
    check(got == kept, f"写回内容一致（{len(got)} / {len(kept)}）")
    check(chk.mount_point == FX.MOUNT, f"挂载点正确（{chk.mount_point!r}）")

    print(f"\n=== 分组剥离测试 {'PASS' if not fails else 'FAIL ' + str(fails)} ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
