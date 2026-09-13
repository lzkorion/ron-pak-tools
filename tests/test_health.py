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

    print("\n3) 挂载点在官方清单里找不到 + 完全不覆盖官方 -> 警告")
    print("   （纯新增内容模组可以有自己的命名空间，不算错）")
    bad = FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_BadMount_P.pak"),
                      FX.default_files(),
                      mount="../../../ReadyOrNot/Content/NoSuchFolder/")
    r3 = RH.check(bad, off, peers=[])
    check(find(r3, "挂载点不在游戏命名空间里", RH.LEVEL_ERROR) is None
          and find(r3, "挂载点在官方清单里找不到", RH.LEVEL_WARN) is not None,
          "报出「挂载点找不到 + 不覆盖官方」的警告", titles(r3))

    print("\n4) 一条都对不上官方路径 -> 警告（纯新增内容的模组可忽略）")
    newonly = FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_New_P.pak"),
                          {k: FX._asset_blob(k, b"z" * 40)
                           for k in FX.UNIQUE_ASSETS})
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

    print("\n11) 压缩方式必须和游戏本体一致")
    gdir = os.path.join(WORK, "gamepaks")
    os.makedirs(gdir, exist_ok=True)
    FX.make_pak(os.path.join(gdir, "pakchunk1-Windows.pak"))
    check(RH.game_compression_methods(gdir) == ["Oodle"],
          f"读本体压缩方式：{RH.game_compression_methods(gdir)}")
    r12 = RH.check(good, off, paks_dir=gdir, peers=[])
    check(find(r12, "压缩方式", RH.LEVEL_OK) is not None,
          "Oodle 一致 -> 通过", titles(r12))
    # 一个用 Zlib 的模组（Hospital 地图模组就是这样，游戏是 Oodle）
    zlib_pak = FX.make_pak(os.path.join(WORK, "pakchunk99-Mods_Zlib_P.pak"),
                           methods=["Zlib", "", "", "", ""])
    r13 = RH.check(zlib_pak, off, paks_dir=gdir, peers=[])
    check(find(r13, "压缩方式", RH.LEVEL_ERROR) is not None,
          "Zlib 不一致 -> 报错", titles(r13))

    print("\n12) 「先诊断再转换」的结论")
    # 12a. 有照抄官方的冲突资产 -> 可以转换
    a1 = RH.assess(good, off, peers=[])
    check(a1["verdict"] == "convert" and a1["should_convert"],
          f"照抄的冲突资产 -> 可以转换（{a1['label']}）", str(a1["reasons"]))
    # 12b. 同路径的资产全被模组改过 -> 不建议转换
    names2, sizes2 = FX.official_manifest_entries()
    sizes2 = {k: (v[0] + 1, v[1] + 1) for k, v in sizes2.items()}   # 内容都不同
    off_mod = FX.make_stub_official(names2, sizes2)
    a2 = RH.assess(good, off_mod, peers=[])
    check(a2["verdict"] == "do_not" and not a2["should_convert"],
          f"全是模组改过的 -> 不建议转换（{a2['label']}）", str(a2["reasons"]))
    # 12c. 没东西可剥 -> 不用转换
    a3 = RH.assess(newonly, off, peers=[])
    check(a3["verdict"] == "no_change" and not a3["should_convert"],
          f"没有可剥内容 -> 不用转换（{a3['label']}）")
    # 12d. 文件名没有 _P -> 转换也修不好
    a4 = RH.assess(nop, off, peers=[])
    check(a4["verdict"] == "blocked" and not a4["should_convert"],
          f"结构硬伤 -> 转换也修不好（{a4['label']}）", str(a4["reasons"]))
    # 12e. 读不了
    a5 = RH.assess(bad_file, off, peers=[])
    check(a5["verdict"] == "unreadable", f"坏文件 -> 读不了（{a5['label']}）")
    # 12f. 有孤儿 -> 转换修不了
    a6 = RH.assess(orph, off, peers=[])
    check(a6["verdict"] == "blocked", f"孤儿条目 -> 转换也修不好（{a6['label']}）",
          str(a6["reasons"]))

    print("\n13) 挂载点检查不能误伤「纯新增内容」的模组")
    # 官方没有这个命名空间，但模组完全是新增内容 -> 不该报 error
    ns = FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_NewNS_P.pak"),
                     {f"NewStuff/{i}.uasset": b"\xc1\x83\x2a\x9e" + b"n" * 40
                      for i in range(3)},
                     mount="../../../ReadyOrNot/Content/")
    r14 = RH.check(ns, off, peers=[])
    check(r14["errors"] == 0,
          f"新命名空间 + 无覆盖 -> 不报错（{r14['errors']}）", titles(r14))
    # 命名空间根部就不对 -> 必须报错
    bad_ns = FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_BadNS_P.pak"),
                         FX.default_files(), mount="../../../NotAGame/Content/")
    r15 = RH.check(bad_ns, off, peers=[])
    check(find(r15, "不在游戏命名空间", RH.LEVEL_ERROR) is not None,
          "命名空间不对 -> 报错", titles(r15))

    print("\n14) 地图模组要明确提示「转换救不了」")
    mp = FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_Map_P.pak"),
                     {**FX.default_files(),
                      "Mods/MyMap/MyLevel.umap": b"\xc1\x83\x2a\x9e" + b"m" * 50})
    r16 = RH.check(mp, off, peers=[])
    check(r16.get("is_map") is True, "识别为地图模组")
    check(find(r16, "地图模组") is not None, "给出地图模组的提示", titles(r16))

    print(f"\n=== 体检功能测试 {'PASS' if not fails else 'FAIL ' + str(fails)} ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
