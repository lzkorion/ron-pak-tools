#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引用分析测试（tools/ronrefs.py）。

★ 全部用【不压缩】的合成 pak —— 这样不需要 oo2core 也能跑，
  CI（Ubuntu/Windows）上都能过。真实模组的 Oodle 路径由手工回归验证。

覆盖：UE 包路径映射、引用抽取、自己/官方/断链三分类、
      「改名过的」与「彻底没有的」区分、位置不符检测、
      没有 oo2core 时不崩且明确降级。
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)
import fixtures as FX
import pakfmt as P
import ronconvert as RC
import ronhealth as RH
import ronrefs as RR

WORK = os.path.join(HERE, "_work", "refs")
fails = []


def check(cond, label, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label
          + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def pkg_blob(*paths: str) -> bytes:
    """合成一个「里面有这些包路径串」的资产载荷。"""
    magic = FX.PKG_MAGIC
    body = b"".join(p.encode() + b"\x00" for p in paths)
    return magic + b"\x00" * 20 + body + b"\x00" * 8


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    print("1) UE 包路径 -> 引擎路径映射")
    check(RR.ue_pkg_to_engine("/Game/Blueprints/Items/BP_X")
          == ["readyornot/content/blueprints/items/bp_x"],
          "/Game/ -> readyornot/content/")
    got = RR.ue_pkg_to_engine("/ReadyOrNotDLC4/Level/RoN_Tower/BP_X")
    check("readyornot/plugins/gamefeatures/readyornotdlc4/content/"
          "level/ron_tower/bp_x" in got,
          f"DLC 走 gamefeatures 命名空间（{got}）")
    check(RR.ue_pkg_to_engine("/Engine/Foo/Bar")[0] == "engine/content/foo/bar",
          "/Engine/ -> engine/content/")
    check(RR.ue_pkg_to_engine("/Nonsense") == [],
          "只有一段的路径不算包名")
    cands = RR.engine_candidates("/Game/ReadyOrNot/Character/T_X")
    check("readyornot/content/readyornot/character/t_x.uasset" in cands,
          "重复的 ReadyOrNot/ 原样保留（官方树里真有这种路径）")
    check("readyornot/content/character/t_x.uasset" in cands,
          "同时也试剥掉一层重复前缀")
    check("readyornot/content/readyornot/character/t_x.umap" in cands,
          ".uasset / .umap 两种扩展名都试")

    print("\n2) 从资产载荷里抽引用")
    blob = pkg_blob("/Game/A/BP_Self", "/Game/B/T_Other",
                    "/Script/Engine.Actor", "/Game/B/T_Other",
                    "/Game/C/M_Third.M_Third_C")
    strs = RR.scan_pkg_strings(blob)
    check("/Script/Engine.Actor" not in strs, "跳过 /Script（C++ 类，不在清单里）")
    check(strs.count("/Game/B/T_Other") == 1, "去重")
    info = RR.asset_refs(blob, "Mods/X/BP_Self.uasset")
    check(info["self_pkg"] == "/Game/A/BP_Self",
          f"按文件名认出它自己（{info['self_pkg']}）")
    check("/Game/A/BP_Self" not in info["refs"], "自己不算引用")
    check(all(".M_Third_C" not in r for r in info["refs"]), "保留原始串（映射时再剥）")
    info2 = RR.asset_refs(pkg_blob("/Game/A/SomethingElse"), "Mods/X/BP_Self.uasset")
    check(info2["self_pkg"] == "", "对不上文件名就不硬认（宁可不报）")

    print("\n3) 压缩方式：不压缩 / Zlib 能解，Oodle 缺失时明确报错")
    check(RR.decompress_any("", b"abc") == b"abc", "不压缩原样返回")
    import zlib
    raw = b"hello" * 100
    z = zlib.compress(raw)
    check(RR.decompress_any("Zlib", z, [len(z)], len(raw)) == raw, "Zlib 能解")
    real_find = RR.find_oodle
    RR.find_oodle = lambda *a, **k: (None, "")
    try:
        try:
            RR.decompress_any("Oodle", b"x" * 10, [], 100)
            check(False, "没有 oo2core 时应当报错")
        except Exception as ex:
            check("oo2core" in str(ex), f"报错说清楚原因（{ex}）")
    finally:
        RR.find_oodle = real_find

    print("\n4) 「游戏里现在最可能叫什么」的建议")
    names = [
        RC.OfficialAssets.full_path_of(FX.MOUNT, "IconTextures/Loadout/icn_12g_bucknew_1024.uasset"),
        RC.OfficialAssets.full_path_of(FX.MOUNT, "Textures/Blood/T_Sample_Blood_BA.uasset"),
        RC.OfficialAssets.full_path_of(FX.MOUNT, "Blueprints/Items/Curve_Damage_Shotgun_590.uasset"),
    ]
    off = FX.make_stub_official(names)
    sug = RR.suggest_many(["/Game/IconTextures/Loadout/icn_12g_bucknew",
                           "/Game/Blueprints/Items/Curve_Damage_Shotgun",
                           "/Game/Totally/Unknown_Thing_XYZ"], off)
    check(sug["/Game/IconTextures/Loadout/icn_12g_bucknew"]
          == ["readyornot/content/icontextures/loadout/icn_12g_bucknew_1024.uasset"],
          f"加了 _1024 后缀 -> 找得到（{sug['/Game/IconTextures/Loadout/icn_12g_bucknew']}）")
    check(sug["/Game/Blueprints/Items/Curve_Damage_Shotgun"]
          == ["readyornot/content/blueprints/items/curve_damage_shotgun_590.uasset"],
          "拆成多把枪 -> 找得到")
    check(sug["/Game/Totally/Unknown_Thing_XYZ"] == [],
          "八竿子打不着的 -> 不给建议")
    check(RR.suggest_many(["/Game/A/T_Drip"], off)["/Game/A/T_Drip"] == []
          or all("drip" in s for s in RR.suggest_many(["/Game/A/T_Drip"], off)["/Game/A/T_Drip"]),
          "短主干不会乱配（不会配到 dripping_dirt 那种）")

    print("\n5) 「它自己那块目录」的判定（挂载点是游戏根时不能全算成自己的）")
    own = RR.build_own_paths("../../../", ["ReadyOrNot/Content/Mods/A/X.uasset"])
    prefs = RR.own_prefixes("", own)
    check(prefs == {"mods/a/"}, f"前缀按内容目录内两层算（{prefs}）")
    check(RR._content_rel("readyornot/content/mods/a/y.uasset").startswith(tuple(prefs)),
          "自己目录里的路径 -> 命中")
    check(not RR._content_rel("readyornot/content/blueprints/x.uasset"
                              ).startswith(tuple(prefs)),
          "官方 blueprints 不会被当成它的目录")

    print("\n6) 端到端：合成模组的引用对账")
    # 官方清单里有：BP_SampleGun（活着）、icn_12g_bucknew_1024（改名后的新名字）
    live_guns = RC.OfficialAssets.full_path_of(
        FX.MOUNT, "Blueprints/Items/WeaponsRevised/BP_SampleGun.uasset")
    live_icon = RC.OfficialAssets.full_path_of(
        FX.MOUNT, "IconTextures/Loadout/icn_12g_bucknew_1024.uasset")
    off2 = FX.make_stub_official([live_guns, live_icon])

    main_asset = pkg_blob(
        "/Game/Mods/Test/BP_Test",                      # 它自己
        "/Game/Blueprints/Items/WeaponsRevised/BP_SampleGun",   # 活着
        "/Game/Mods/Test/BP_Sibling",                   # 模组自己另一个文件
        "/Game/IconTextures/Loadout/icn_12g_bucknew",   # 游戏里改名了
        "/Game/Mods/Test/BP_Missing",                   # 彻底没有（它自己目录里）
    )
    wrong_asset = pkg_blob("/Game/Mods/Test/BP_Wrong")   # 内部包名与 pak 位置不符
    pak = FX.make_pak(
        os.path.join(WORK, "pakchunk9999-Mods_Refs_P.pak"),
        {"Mods/Test/BP_Test.uasset": main_asset,
         "Mods/Test/BP_Sibling.uasset": pkg_blob("/Game/Mods/Test/BP_Sibling"),
         "Other/BP_Wrong.uasset": wrong_asset},
        methods=["", "", "", "", ""])

    r = RR.analyze_pak(pak, off2)
    check(r["ok"], f"分析成功（{r.get('error', '')}）")
    check(r["assets"] == 3 and r["read"] == 3 and r["skipped"] == 0,
          f"3 个资产全读出来（{r['read']}/{r['assets']} 跳过 {r['skipped']}）")
    check(r["refs_alive"] >= 1, f"活着的引用计入（{r['refs_alive']}）")
    check(r["refs_own"] >= 1, f"引用模组自己的文件计入（{r['refs_own']}）")
    ren = {it["ref"] for it in r["broken"]["renamed"]}
    gone = {it["ref"] for it in r["broken"]["gone"]}
    check("/Game/IconTextures/Loadout/icn_12g_bucknew" in ren,
          f"改名过的 -> renamed（{sorted(ren)}）")
    check("/Game/Mods/Test/BP_Missing" in gone,
          f"彻底没有的 -> gone（{sorted(gone)}）")
    check("/Game/Mods/Test/BP_Sibling" not in ren | gone,
          "模组自己打包了的引用不算断链")
    check("/Game/Mods/Test/BP_Test" not in ren | gone, "自己引用自己不算断链")
    item = [i for i in r["broken"]["gone"] if i["ref"].endswith("BP_Missing")][0]
    check(item["in_own_tree"], "落在模组自己目录里 -> 标记出来")
    check(item["assets"] == ["Mods/Test/BP_Test.uasset"],
          f"知道是谁引用的（{item['assets']}）")
    check(len(r["misplaced"]) == 1
          and r["misplaced"][0]["asset"] == "Other/BP_Wrong.uasset",
          f"检测出「内部包名和 pak 位置对不上」（{r['misplaced']}）")

    print("\n7) 报告渲染 + 体检串起来")
    import io
    buf = io.StringIO()
    RR.render_report(r, log=buf.write)
    txt = buf.getvalue()
    check("引用分析" in txt and "改名/搬了目录" in txt, "报告里有两类断链的说明")
    check("最接近" in txt, "报告给出「最接近的是」")
    check("对不上" in txt, "报告给出位置不符的警告")

    a = RH.assess(pak, off2, peers=[], refs=True)
    check(a["refs"] is not None and a["refs"]["ok"], "assess(refs=True) 带出引用分析")
    check(any("引用分析" in x for x in a["reasons"]),
          f"结论里带一句引用分析（{a['reasons']}）")
    buf2 = io.StringIO()
    RH.render_assess(a, log=buf2.write)
    check("引用分析" in buf2.getvalue(), "render_assess 打印引用分析")
    a0 = RH.assess(pak, off2, peers=[], refs=False)
    check(a0["refs"] is None, "默认不做引用分析（不拖慢体检）")

    print("\n8) 降级路径：没有清单 / 模块缺失都不该崩")
    rr = RH.ref_report(pak, None)
    check(not rr["ok"] and "清单" in rr["error"],
          f"没清单 -> 明确说清楚（{rr['error']}）")
    buf3 = io.StringIO()
    RR.render_report({"ok": False, "name": "x.pak", "error": "boom",
                      "missing": [], "broken": {"renamed": [], "gone": []},
                      "misplaced": [], "refs": 0, "refs_alive": 0,
                      "refs_own": 0, "assets": 0, "read": 0, "skipped": 0,
                      "skipped_reason": "", "truncated": False, "seconds": 0},
                     log=buf3.write)
    check("读不了" in buf3.getvalue(), "坏结果也能打印")

    print("\n9) 只读不写 + PakIndex 载荷读取回归")
    before = sorted(os.listdir(os.path.dirname(pak)))
    RR.analyze_pak(pak, off2)
    check(sorted(os.listdir(os.path.dirname(pak))) == before,
          "引用分析不产生任何文件")
    # PakIndex 只加载索引区，payload_of 必须自己去文件里读（不能拿索引切片）
    pidx = P.read_pak_index(pak)
    pfull = P.PakFile(pak)
    e1 = pidx.locate_by_path("Mods/Test/BP_Test.uasset")
    e2 = pfull.locate_by_path("Mods/Test/BP_Test.uasset")
    check(e1 is not None and e2 is not None, "两种读法都能定位条目")
    a1, a2 = pidx.payload_of(e1), pfull.payload_of(e2)
    check(len(a1) == e1.size and a1 == a2,
          f"PakIndex.payload_of 与 PakFile 一致（{len(a1)} vs {len(a2)}）")
    check(a1.startswith(FX.PKG_MAGIC), "读到的确实是载荷（UE 包魔数开头）")

    print("\n10) 崩溃风险预警：引用指向游戏已改名/移除的资产")
    # 实测案例：一个老 HK416 武器模组覆盖了官方武器蓝图，蓝图里引用的附件
    # 被官方改名了，游戏一启动就崩（EXCEPTION_ACCESS_VIOLATION /
    # FAsyncLoadingThread）。所以这里必须报出来，而且要说人话。
    risky = FX.make_pak(
        os.path.join(WORK, "pakchunk999-Mods_Crash_P.pak"),
        {"Blueprints/Items/WeaponsRevised/Primary_HK416.uasset": pkg_blob(
            "/Game/Blueprints/Items/WeaponsRevised/Primary_HK416",   # 它自己
            "/Game/Blueprints/Items/Attachments/Magazines/BP_Magazine_PMAG30",
            "/Game/Crosshair/CH_HK416",                             # 游戏里没有
        )})
    off3 = FX.make_stub_official([
        RC.OfficialAssets.full_path_of(
            FX.MOUNT, "Blueprints/Items/Attachments/BP_Magazine_PMAG.uasset"),
    ])
    a_crash = RH.assess(risky, off3, peers=[], refs=True)
    cr = a_crash.get("crash_risk") or {}
    check(cr.get("renamed", 0) >= 1,
          f"扫出「游戏已改名」的引用（{cr.get('renamed')} 处）")
    check(any("bp_magazine_pmag" in x for x in cr.get("examples") or []),
          f"并且告诉玩家现在叫什么（{cr.get('examples')}）")
    check(any("崩溃风险" in x for x in a_crash["reasons"]),
          "结论里明确写出崩溃风险")
    buf6 = io.StringIO()
    RH.render_assess(a_crash, log=buf6.write)
    txt6 = buf6.getvalue()
    check("崩溃风险" in txt6 and "移出 Paks" in txt6,
          "并给出「移出 Paks 再启动一次」的确认办法")
    check("既然现在能用" not in txt6,
          "会崩的模组不会再说「既然现在能用就别动它」")
    # 干净的模组不该有这个警告
    a_clean = RH.assess(FX.make_pak(os.path.join(WORK, "pakchunk9999-Mods_Ok_P.pak"),
                                    {k: FX._asset_blob(k, b"o" * 30)
                                     for k in FX.UNIQUE_ASSETS}),
                        off3, peers=[], refs=True)
    check(not a_clean.get("crash_risk"), "没断链的模组不报崩溃风险")

    print(f"\n=== 引用分析测试 {'PASS' if not fails else 'FAIL ' + str(fails)} ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
