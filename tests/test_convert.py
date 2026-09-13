#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ronconvert 全分支测试：用合成 pak 覆盖每一种判定结果。

不依赖任何真实模组样本。
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)
import pakfmt as P
import ronconvert as RC
import fixtures as FX

WORK = os.path.join(HERE, "_work", "convert")
UP = r"G:\UE_5.8\Engine\Binaries\Win64\UnrealPak.exe"
fails = []


def check(cond, label, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label
          + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def stub(names):
    """造一个「官方已有这些名字」的桩清单。"""
    class Stub(RC.OfficialAssets):
        def __init__(self):
            super().__init__(None)
            keys = [RC.OfficialAssets._normalize(n) for n in names]
            self.full = set(k for k in keys if "/" in k)
            self.bare = set(k.rsplit("/", 1)[-1] for k in keys)
            self.source = "(合成桩清单)"
    return Stub()


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    cases = []

    # A. 混合：蓝图/数据（剥） + 贴图（留） + 独有（留）
    p = FX.make_pak(os.path.join(WORK, "A_mixed_P.pak"))
    cases.append(("A 混合（蓝图+贴图）", p, FX.official_paths(),
                  {"verdict": "已转换（剥离冲突）", "dropped": 4, "kept": 5},
                  {"keep": FX.KEEP_ASSETS + FX.UNIQUE_ASSETS,
                   "drop": FX.CONFLICT_ASSETS}))

    # B. 全是冲突型官方资产 -> 已被官方完全取代（且不应产出空 pak）
    p = FX.make_pak(os.path.join(WORK, "B_allconflict_P.pak"),
                    {k: b"x" * 50 for k in FX.CONFLICT_ASSETS})
    cases.append(("B 全是冲突型", p, FX.official_paths(),
                  {"verdict": "已被官方完全取代", "dropped": 4, "kept": 0},
                  {"keep": [], "drop": FX.CONFLICT_ASSETS}))

    # C. 全是资源替换型 -> 本来就可用
    p = FX.make_pak(os.path.join(WORK, "C_textures_P.pak"),
                    {k: b"t" * 80 for k in FX.KEEP_ASSETS})
    cases.append(("C 全贴图替换", p, FX.official_paths(),
                  {"verdict": "本来就可用", "dropped": 0, "kept": 3},
                  {"keep": FX.KEEP_ASSETS, "drop": []}))

    # D. 全新内容（官方都没有）-> 本来就可用
    p = FX.make_pak(os.path.join(WORK, "D_new_P.pak"),
                    {k: b"n" * 60 for k in FX.UNIQUE_ASSETS})
    cases.append(("D 全新内容", p, FX.official_paths(),
                  {"verdict": "本来就可用", "dropped": 0, "kept": 2},
                  {"keep": FX.UNIQUE_ASSETS, "drop": []}))

    # E. 损坏文件
    bad = os.path.join(WORK, "E_broken_P.pak")
    with open(bad, "wb") as f:
        f.write(b"this is not a pak file at all" * 10)
    cases.append(("E 损坏文件", bad, [], {"verdict": "无法处理"}, None))

    # F. 空文件
    empty = os.path.join(WORK, "F_empty_P.pak")
    open(empty, "wb").close()
    cases.append(("F 空文件", empty, [], {"verdict": "无法处理"}, None))

    print("=== ronconvert 全分支（合成 pak）===")
    for label, src, official, expect, content in cases:
        d = RC.diagnose(src, stub(official), verbose=False)
        errs = []
        for k, v in expect.items():
            got = getattr(d, k)
            if got != v:
                errs.append(f"{k}={got!r} 期望 {v!r}")
        if content and d.ok:
            rels = set(e.rel for e in d._elist)
            for k in content["keep"]:
                if k not in rels or k in d._drop:
                    errs.append(f"应保留但被剥/丢失: {k}")
            for k in content["drop"]:
                if k not in d._drop:
                    errs.append(f"应剥离但保留了: {k}")
            outdir = os.path.join(WORK, "out_" + os.path.basename(src))
            RC.convert(d, outdir, verify=False)
            if not content["keep"]:
                # ★ 一条都不剩时不应写出空 pak（空 pak 挂上去 = 模组变废）
                if d.out_path:
                    errs.append(f"不该产出文件，却有 {d.out_path}")
                if os.path.isfile(os.path.join(outdir, os.path.basename(src))):
                    errs.append("不该产出文件，但输出目录里有一个空 pak")
            else:
                chk = P.read_pak(d.out_path)
                if sorted(chk.all_paths()) != sorted(content["keep"]):
                    errs.append(f"写回内容不符: {sorted(chk.all_paths())}")
                if os.path.isfile(UP):
                    r = subprocess.run([UP, d.out_path, "-Test"],
                                       capture_output=True, timeout=600)
                    if r.returncode != 0:
                        errs.append(f"官方 -Test 失败 rc={r.returncode}")

        if errs:
            print(f"[FAIL] {label}")
            for e in errs:
                print(f"        {e}")
            fails.append(label)
        else:
            extra = f"  kept={d.kept} dropped={d.dropped}" if d.ok else ""
            print(f"[PASS] {label:<18} -> {d.verdict}{extra}")

    print(f"\n=== ronconvert 测试: "
          f"{'PASS' if not fails else 'FAIL ' + str(fails)} ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
