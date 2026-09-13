#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全底线测试：「同名」不等于「同路径」。

本次事故的根因：旧逻辑用【文件名】判定「官方已有」，而真实模组的路径是

    mount = '../../../ReadyOrNot/Content/'
    rel   = 'ReadyOrNot/Character/Gore/.../T_Gore_Limb_Amputations_body_BC.uasset'

路径里多带一层 ReadyOrNot，和官方永远「同路径匹配不上」，但**文件名**和
官方某个资产一样 —— 于是模组自己的贴图/网格被当成官方内容剥掉，模组直接失效
（实测 wound 9 条剥到 0 条、VisceralBlud 剥掉 151 条贴图）。

这个文件把三条底线锁死：
  1. 只有裸文件名（bare）时 —— 默认【一条都不剥】
  2. 真实路径形状（多一层 ReadyOrNot）—— full 匹配仍然要能命中
  3. 生成的清单必须是全路径（否则 full 索引为空、工具空转）
另外覆盖 --match-name 激进模式的边界。

不依赖任何真实模组样本。
"""
import json
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

WORK = os.path.join(HERE, "_work", "safety")
fails = []


def check(cond, label, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label
          + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def stub_from_paths(paths):
    class Stub(RC.OfficialAssets):
        def __init__(self):
            super().__init__(None)
            self.add_paths(paths)
            self.source = "(合成桩)"
    return Stub()


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    # ------------------------------------------------------------------
    print("1) 兜底：清单里【只有文件名】时，一条都不能剥")
    print("   （这正是旧逻辑的误杀场景：同名就当官方已有 → 贴图/网格被剥光）")
    mod = FX.make_pak(os.path.join(WORK, "BareName_P.pak"))
    bare = stub_from_paths(FX.official_names())      # 只有裸名
    check(bare.has_full_index is False, "这份清单确实没有全路径索引")
    d = RC.diagnose(mod, bare, verbose=False)
    check(d.matched_bare == len(FX.CONFLICT_ASSETS + FX.KEEP_ASSETS),
          f"官方清单里的每一条都只是 bare 命中（{d.matched_bare}）")
    check(d.dropped == 0, f"剥离数 = 0（实际 {d.dropped}）")
    check(d.kept == d.total_entries, "全部保留")
    check(d.verdict == "本来就可用", f"结论：{d.verdict}")
    out = os.path.join(WORK, "bare_out")
    RC.convert(d, out, verify=False)
    got = set(P.read_pak(d.out_path).all_paths())
    check(got == set(FX.default_files()), f"转换后内容一字不少（{len(got)} 条）")

    # ------------------------------------------------------------------
    print("\n2) 真实路径形状：模组路径多带一层 ReadyOrNot/")
    print("   官方 = '../../../'          + 'ReadyOrNot/Content/Blueprints/...'")
    print("   模组 = '../../../ReadyOrNot/Content/' + 'ReadyOrNot/Content/Blueprints/...'")
    paks = os.path.join(WORK, "gamepaks")
    os.makedirs(paks, exist_ok=True)
    FX.make_official_pak_like_real(os.path.join(paks, "pakchunk1-Windows.pak"))
    man = os.path.join(WORK, "full_manifest.json")
    RC.build_manifest_from_game_paks(paks, man, verbose=False)
    names = json.load(open(man, encoding="utf-8"))["names"]
    check(all("/" in n for n in names),
          "生成的本体清单是全路径（不是裸文件名）")
    off = RC.OfficialAssets(man)
    check(off.has_full_index is True, f"全路径 {len(off.full)} 条")

    doubled = {("ReadyOrNot/Content/" + r): (f"x{i}-".encode() * 30)
               for i, r in enumerate(FX.CONFLICT_ASSETS + FX.KEEP_ASSETS)}
    mod2 = FX.make_pak(os.path.join(WORK, "Doubled_P.pak"), doubled)
    d2 = RC.diagnose(mod2, off, verbose=False)
    check(d2.matched_full == len(doubled),
          f"全部按【路径一致】命中（{d2.matched_full}/{len(doubled)}）")
    check(d2.matched_bare == 0, "没有一条退化成 bare")
    for rel in doubled:
        if rel.endswith(".uasset") and "Textures/" in rel:
            check(rel not in d2._drop, f"贴图类保留: {rel}")
    for rel in FX.CONFLICT_ASSETS:
        check(("ReadyOrNot/Content/" + rel) in d2._drop,
              f"蓝图/数据类剥离: {rel}")

    # ------------------------------------------------------------------
    print("\n3) --match-name 激进模式：同名的边界")
    print("   3a) 同名只出现一次 + 冲突型 -> 允许剥（这是它存在的意义）")
    uniq = RC.OfficialAssets.full_path_of(
        "../../../", "ReadyOrNot/Content/Somewhere/Else/SampleDataTable.uasset")
    d3 = RC.diagnose(FX.make_pak(os.path.join(WORK, "Aggr_P.pak"),
                                 {FX.CONFLICT_ASSETS[2]: b"d" * 40}),
                     stub_from_paths([uniq]), match_name=True, verbose=False)
    check(FX.CONFLICT_ASSETS[2] in d3._drop,
          "唯一同名 + 冲突型 -> 剥离", f"({FX.CONFLICT_ASSETS[2]})")

    print("   3b) 同名出现在多个目录 -> 仍然不剥（不敢赌）")
    amb = [RC.OfficialAssets.full_path_of(
        "../../../", f"ReadyOrNot/Content/Dir{n}/SampleDataTable.uasset")
        for n in (1, 2)]
    st = stub_from_paths(amb)
    check("sampledatatable.uasset" in st.bare_ambiguous, "已记录为歧义同名")
    d4 = RC.diagnose(FX.make_pak(os.path.join(WORK, "Amb_P.pak"),
                                 {FX.CONFLICT_ASSETS[2]: b"d" * 40}),
                     st, match_name=True, verbose=False)
    check(d4.dropped == 0, f"歧义同名 -> 不剥（实际剥 {d4.dropped}）")

    print("   3c) 贴图类即使同名也不剥（资源替换是正常 mod 行为）")
    d5 = RC.diagnose(mod, bare, match_name=True, verbose=False)
    for rel in FX.KEEP_ASSETS:
        check(rel not in d5._drop, f"贴图仍然保留: {rel}")

    # ------------------------------------------------------------------
    print("\n4) 「内容和官方不一样」的提示（只警告，不改变剥离）")
    print("   官方那份和模组不同 → 应该提示，但剥离结果不变")
    check(len(d2.size_mismatch) == len(FX.CONFLICT_ASSETS),
          f"4 个被剥的蓝图/数据都被标出内容不同（{len(d2.size_mismatch)}）")
    for rel, msz, osz in d2.size_mismatch:
        check(msz != osz, f"{rel.rsplit('/',1)[-1]}: 模组 {msz}B != 官方 {osz}B")
    check(any("内容和官方不一样" in a for a in d2.actions),
          "结论里给出了这条提示")
    check(d2.dropped == 4, f"剥离行为没变（仍然剥 4 条，实际 {d2.dropped}）")

    print("   官方那份和模组逐字节一样 → 不该有任何提示")
    paks2 = os.path.join(WORK, "gamepaks_same")
    os.makedirs(paks2, exist_ok=True)
    FX.make_official_pak_like_real(os.path.join(paks2, "pakchunk1-Windows.pak"),
                                   identical=True)
    man2 = os.path.join(WORK, "same_manifest.json")
    RC.build_manifest_from_game_paks(paks2, man2, verbose=False)
    off2 = RC.OfficialAssets(man2)
    check(bool(off2.sizes), f"清单带上了大小信息（{len(off2.sizes)} 条）")
    # 模组内容和「官方」那份逐字节一样（都和 default_files() 相同）
    mod_same = FX.make_pak(os.path.join(WORK, "Identical_P.pak"),
                           {"ReadyOrNot/Content/" + rel: blob
                            for rel, blob in FX.default_files().items()})
    d6 = RC.diagnose(mod_same, off2, verbose=False)
    check(d6.dropped == len(FX.CONFLICT_ASSETS),
          f"照旧剥离（{d6.dropped} 条）")
    check(d6.size_mismatch == [],
          f"内容一致 -> 无提示（实际 {d6.size_mismatch}）")

    print("   老清单没有 sizes 段 -> 不报错、只是没有这层提示")
    old_man = FX.write_manifest(os.path.join(WORK, "oldschool.json"),
                                FX.official_paths())
    off3 = RC.OfficialAssets(old_man)
    check(off3.sizes == {}, "没有大小信息")
    d7 = RC.diagnose(mod2, off3, verbose=False)
    check(d7.size_mismatch == [] and d7.dropped == len(FX.CONFLICT_ASSETS),
          "老清单下照常剥离、不误报")

    print(f"\n=== 安全底线测试 {'PASS' if not fails else 'FAIL ' + str(fails)} ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
