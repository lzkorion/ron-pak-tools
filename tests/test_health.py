#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ronhealth 体检测试：每一条「为什么没效果」的判断都要能被触发。

全部用合成 pak（tests/fixtures.py），不依赖游戏或真实模组。
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)
import fixtures as FX
import ronconvert as RC
import ronhealth as RH

WORK = os.path.join(HERE, "_work", "health")
fails = []


def check(cond, label, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label
          + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def titles(r):
    return " | ".join(f["title"] for f in r["findings"])


def find(r, kw, level=None):
    for f in r["findings"]:
        if kw in f["title"] and (level is None or f["level"] == level):
            return f
    return None


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    # 官方清单（全路径 + 大小）
    names, sizes = FX.official_manifest_entries()
    off = FX.make_stub_official(names, sizes)

    print("1) 正常模组：不该报错")
    good = FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_Good_P.pak"))
    r = RH.check(good, off, peers=[])
    check(r["ok"], "能读")
    check(r["errors"] == 0, f"无 error（实际 {r['errors']}）", titles(r))
    check(r["overrides"] == len(FX.CONFLICT_ASSETS + FX.KEEP_ASSETS),
          f"对上官方路径 {r['overrides']} 条")
    check(find(r, "成组完整") is not None, "资产成组检查通过")
    check(r["verdict"] == "没发现结构性问题", f"结论：{r['verdict']}")

    print("\n2) 文件名没有 _P 后缀 -> 报错（指南明确点名）")
    nop = FX.make_pak(os.path.join(WORK, "pakchunk99-Mods_NoPatch.pak"))
    r2 = RH.check(nop, off, peers=[])
    check(find(r2, "_P.pak", RH.LEVEL_ERROR) is not None,
          "报出「不是 _P.pak 结尾」", titles(r2))
    check(r2["errors"] >= 1, f"计入 error（{r2['errors']}）")

    print("\n3) 挂载点在游戏里不存在 -> 报错（必然无效）")
    bad = FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_BadMount_P.pak"),
                      FX.default_files(),
                      mount="../../../ReadyOrNot/Content/NoSuchFolder/")
    r3 = RH.check(bad, off, peers=[])
    check(find(r3, "不存在", RH.LEVEL_ERROR) is not None,
          "报出「挂载点在游戏里不存在」", titles(r3))

    print("\n4) 一条都对不上官方路径 -> 警告（纯新增内容的模组可忽略）")
    newonly = FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_New_P.pak"),
                          {k: b"z" * 40 for k in FX.UNIQUE_ASSETS})
    r4 = RH.check(newonly, off, peers=[])
    check(find(r4, "一条都没对上", RH.LEVEL_WARN) is not None,
          "报出「一条都没对上」", titles(r4))
    check(r4["overrides"] == 0 and r4["new"] == len(FX.UNIQUE_ASSETS),
          f"覆盖 0 / 新增 {r4['new']}")

    print("\n5) 孤儿条目（只有 .uexp，没有 .uasset）-> 报错")
    stem = "Blueprints/Items/Orphan/BP_Lonely"
    orph = FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_Orphan_P.pak"),
                       {f"{stem}.uexp": b"o" * 30,
                        f"{stem}.ubulk": b"p" * 30})
    r5 = RH.check(orph, off, peers=[])
    check(find(r5, "孤儿", RH.LEVEL_ERROR) is not None,
          "报出孤儿条目", titles(r5))

    print("\n6) 加载顺序：被 pakchunk 更大的模组覆盖 -> 报错")
    mine = FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_Mine_P.pak"))
    peer_full = set(RC.OfficialAssets.full_path_of(FX.MOUNT, r)
                    for r in FX.CONFLICT_ASSETS + FX.KEEP_ASSETS)
    peers = [{"name": "pakchunk99999-Mods_Other_P.pak", "mount": FX.MOUNT,
              "chunk": 99999, "full": peer_full, "size": 1}]
    r6 = RH.check(mine, off, peers=peers)
    check(find(r6, "被 pakchunk99999", RH.LEVEL_ERROR) is not None,
          "报出被更大的 chunk 覆盖", titles(r6))

    print("   同号（谁生效不确定）-> 警告")
    peers_same = [dict(peers[0], name="pakchunk9999-Mods_Tie_P.pak", chunk=9999)]
    r7 = RH.check(mine, off, peers=peers_same)
    check(find(r7, "抢同一个", RH.LEVEL_WARN) is not None,
          "报出同号冲突", titles(r7))

    print("   对方 chunk 更小 -> 只是提示，不算问题")
    peers_lo = [dict(peers[0], name="pakchunk99-Mods_Low_P.pak", chunk=99)]
    r8 = RH.check(mine, off, peers=peers_lo)
    check(find(r8, "你覆盖了", RH.LEVEL_INFO) is not None,
          "提示你覆盖了对方", titles(r8))
    check(r8["errors"] == 0, f"没有 error（{r8['errors']}）")

    print("\n7) 文件名里解析 pakchunk 号")
    check(RH.pak_chunk("pakchunk9999-Mods_X_P.pak") == 9999, "9999")
    check(RH.pak_chunk("pakchunk0-Windows.pak") == 0, "0")
    check(RH.pak_chunk("Loose.pak") is None, "解析不出来 -> None")
    check(RH.mount_root("../../../ReadyOrNot/Content/") == "readyornot/content",
          "挂载点归一化")
    check(RH.mount_root("../../../") == "", "游戏根 -> 空")

    print("\n8) 坏文件不该崩")
    bad_file = os.path.join(WORK, "broken.pak")
    open(bad_file, "wb").write(b"not a pak" * 20)
    r9 = RH.check(bad_file, off, peers=[])
    check(not r9["ok"] and r9["error"], "读出错误而不是抛异常")
    check(r9["findings"][0]["level"] == RH.LEVEL_ERROR, "第一条是 error")

    print("\n9) 没有官方清单时也要能用（跳过路径比对）")
    r10 = RH.check(good, None, peers=[])
    check(r10["ok"], "仍然能读")
    check(find(r10, "没有官方清单", RH.LEVEL_WARN) is not None,
          "明确说明跳过了路径比对", titles(r10))

    print("\n10) peers=None + paks_dir=None 时不该崩")
    r11 = RH.check(good, off)
    check(r11["ok"], "能读")
    check(r11["errors"] == 0, f"无 error（{r11['errors']}）", titles(r11))

    print(f"\n=== 体检功能测试 {'PASS' if not fails else 'FAIL ' + str(fails)} ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
