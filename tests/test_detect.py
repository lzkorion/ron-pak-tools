#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""游戏目录自动检测 / 本体-模组区分 / 清单新鲜度 测试。

不依赖你本机是否装了游戏：游戏相关用例用合成目录模拟。
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)
import ronconvert as RC
import fixtures as FX

WORK = os.path.join(HERE, "_work", "detect")
fails = []


def check(cond, label, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label
          + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    print("1) 本体 pak vs 玩家模组 pak")
    official = ["pakchunk0-Windows.pak", "pakchunk1-Windows.pak",
                "pakchunk24-Windows.pak"]
    mods = ["pakchunk9999-Mods_AnyMod_P.pak", "MyMod_P.pak", "Loose.pak",
            "pakchunk9000-X.pak", "pakchunk0-Windows.sig"]
    for n in official:
        check(RC.is_official_pak(n) is True, f"本体: {n}")
    for n in mods:
        check(RC.is_official_pak(n) is False, f"模组: {n}")

    print("\n2) 生成清单时必须跳过模组")
    print("   （否则模组内容会被当成官方已有 → 被自己的工具全剥掉）")
    paks = os.path.join(WORK, "paks")
    os.makedirs(paks, exist_ok=True)
    FX.make_official_pak(os.path.join(paks, "pakchunk1-Windows.pak"))
    FX.make_pak(os.path.join(paks, "pakchunk9999-Mods_Test_P.pak"),
                {"Content/Blueprints/FromMod.uasset": b"mod-only"})
    out = os.path.join(WORK, "full.json")
    n = RC.build_manifest_from_game_paks(paks, out, verbose=False)
    data = json.load(open(out, encoding="utf-8"))
    names = [x.lower() for x in data["names"]]
    check(n == 2, f"只收录本体内容（实得 {n} 条）")
    check(any("t_official_sample.uasset" in x for x in names), "本体资产在清单里")
    check(not any("frommod.uasset" in x for x in names), "模组资产未被写入")
    check(data.get("skipped_paks") == ["pakchunk9999-Mods_Test_P.pak"],
          "记录了被跳过的模组")
    check("generated_at" in data, "清单带生成时间戳")

    print("\n3) 清单新鲜度")
    fresh = RC.check_manifest_freshness(out, paks)
    check(fresh["status"] in ("ok", "stale"), f"可判断状态: {fresh['status']}")
    # 把清单时间改成 30 天前 -> 应判 stale
    old = os.path.join(WORK, "old.json")
    RC.write_manifest(old, ["a.uasset"])
    old_t = time.time() - 30 * 86400
    d = json.load(open(old, encoding="utf-8"))
    d["generated_at"] = old_t
    json.dump(d, open(old, "w", encoding="utf-8"))
    check(RC.check_manifest_freshness(old, paks)["status"] == "stale",
          "清单比游戏旧 -> stale")
    check(RC.check_manifest_freshness(os.path.join(WORK, "no.json"), paks)["status"]
          == "missing", "清单不存在 -> missing")
    check(RC.check_manifest_freshness(old, os.path.join(WORK, "nogame"))["status"]
          == "no_game", "找不到游戏 -> no_game")

    print("\n4) full / bare 两级匹配")
    man = FX.write_manifest(os.path.join(WORK, "m.json"), FX.official_names())
    off = RC.OfficialAssets(man)
    check(len(off) == len(off.bare), f"len() = 文件名条目数（{len(off)}）")
    fp = RC.OfficialAssets.full_path_of(
        "../../../ReadyOrNot/", "Content/Blueprints/Items/X.uasset")
    check(fp == "readyornot/content/blueprints/items/x.uasset",
          f"路径归一化: {fp}")
    check(off.has(fp)[0] is False, "清单里没有的 -> 不命中")
    sample = next(iter(off.bare))
    hit, how = off.has(RC.OfficialAssets.full_path_of(
        "../../../ReadyOrNot/Content/", "AnyDir/" + sample))
    check(hit is True, f"已知存在的文件名能命中（方式={how}）")

    print("\n5) 本机没装游戏时不应崩")
    check(RC.find_game_paks([r"Z:\definitely\not\here"]) is None
          or isinstance(RC.find_game_paks([r"Z:\definitely\not\here"]), str),
          "自动查找函数可安全调用")

    print(f"\n=== 检测功能测试 {'PASS' if not fails else 'FAIL ' + str(fails)} ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
