#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ronhealth.py —— 模组「体检」：为什么这个模组装了没效果？

不转换、不写文件，只回答一个问题：**这个 pak 到底能不能生效？**

检查项直接照着《Unofficial Modding Guide》的调试清单来
（https://unofficial-modding-guide.com/posts/thebasics/ → Debugging）：

  1. 【文件名】必须 `_P.pak` 结尾、`pakchunk<N>-` 开头
     —— 指南："No `_P` at the end of the mod name. This is needed when a
        patch pak is used for the main game files."
  2. 【挂载点】必须是「包住全部内容的最深目录」，而且**要真实存在于游戏里**
     —— 指南："Mount point ... the deepest folder that encapsulates all of your
        content. If it is not, you may have an issue."
  3. 【路径】模组的每条路径必须能对上游戏里的路径（覆盖），或者至少是合理的
     新增内容。一条都对不上 -> 它根本没在改游戏
     —— 指南："Check the file structure. This must perfectly match the files
        within the game, otherwise it will not load."
  4. 【加载顺序】同一个路径被别的模组用更高的 pakchunk 号覆盖了 -> 你输
     —— 指南："Incorrect load order may cause issues between mods, change the
        numerical elements of the pak name to change the load order."
  5. 【资产成组】.uasset / .uexp / .ubulk / .bak 缺一不可，否则引擎读不了
  6. 【包格式】抽样看 .uasset 头部魔数对不对（未压缩条目）
  7. 【官方工具】可选跑 UnrealPak -List / -Test

用法:
    python tools/ronhealth.py "某模组.pak"
    python tools/ronhealth.py "C:\\模组文件夹" --manifest manifest_local.json
    python tools/ronhealth.py "某模组.pak" --game-paks "E:\\...\\ReadyOrNot\\Content\\Paks"
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pakfmt as P
import ronconvert as RC

# UE 包的魔数（FPackageFileSummary 开头 0x9E2A83C1）
PKG_MAGIC = b"\xc1\x83\x2a\x9e"

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"
LEVEL_OK = "ok"
LEVEL_INFO = "info"

_ICON = {LEVEL_ERROR: "✘", LEVEL_WARN: "⚠", LEVEL_OK: "✔", LEVEL_INFO: "·"}

CHUNK_RE = re.compile(r"^pakchunk(\d+)-", re.IGNORECASE)
PATCH_RE = re.compile(r"_P\.pak$", re.IGNORECASE)


def pak_chunk(name: str) -> int | None:
    """从文件名解析 pakchunk 号（= 加载顺序）。解析不出来返回 None。"""
    m = CHUNK_RE.match(os.path.basename(name))
    return int(m.group(1)) if m else None


def engine_paths(mount: str, rels) -> list[str]:
    """(mount, 索引路径) -> 引擎实际使用的完整路径。"""
    return [RC.OfficialAssets.full_path_of(mount, r) for r in rels]


def mount_root(mount: str) -> str:
    """挂载点归一化后的「根目录」，用来判断它在游戏里存不存在。

    '../../../ReadyOrNot/Content/' -> 'readyornot/content'
    '../../../'                    -> ''（挂到游戏根，永远合法）
    """
    m = mount.replace("\\", "/").strip()
    while m.startswith("../"):
        m = m[3:]
    return m.strip("/").lower()


def game_compression_methods(paks_dir: str) -> list[str]:
    """游戏本体 pak 用的压缩方式（看第一个能读的本体 pak 就行）。

    模组必须用游戏支持的压缩方式 —— 用了游戏没编进去的解码器，
    读它的资产会失败，表现就是卡加载。
    """
    if not paks_dir or not os.path.isdir(paks_dir):
        return []
    for name in sorted(os.listdir(paks_dir)):
        if not RC.is_official_pak(name):
            continue
        try:
            pk = P.read_pak_index(os.path.join(paks_dir, name))
            return [m for m in pk.compression_methods if m]
        except Exception:
            continue
    return []


def scan_peer_paks(paks_dir: str, skip: str) -> list[dict]:
    """扫已装的其他【模组】pak，拿到它们的路径集合和 chunk 号。

    只看模组（跳过本体 pak）：本体是模组正常要覆盖的对象，
    真正的加载顺序冲突只发生在模组之间。
    """
    out = []
    if not paks_dir or not os.path.isdir(paks_dir):
        return out
    skip_abs = os.path.abspath(skip) if skip else ""
    skip_name = os.path.basename(skip).lower() if skip else ""
    for name in sorted(os.listdir(paks_dir)):
        if not name.lower().endswith(".pak"):
            continue
        if RC.is_official_pak(name):
            continue
        if name.lower() == skip_name:
            continue                    # 就是它自己的装机副本，不算冲突
        path = os.path.join(paks_dir, name)
        if os.path.abspath(path) == skip_abs:
            continue
        try:
            pk = P.read_pak_index(path)
            rels = pk.all_paths()
            mount = pk.mount_point
        except Exception:
            continue
        out.append({"name": name, "mount": mount, "chunk": pak_chunk(name),
                    "full": set(engine_paths(mount, rels)),
                    "size": os.path.getsize(path)})
    return out


def check(src: str, official: RC.OfficialAssets | None = None, *,
          paks_dir: str | None = None, peers: list[dict] | None = None,
          verify: bool = False, log=None) -> dict:
    """给一个 pak 做体检。返回 dict：{ok, error, findings[], ...}"""
    emit = log or (lambda *_a, **_k: None)
    name = os.path.basename(src)
    r: dict = {"src": src, "name": name, "ok": False, "error": "",
               "findings": [], "mount": "", "entries": 0, "chunk": pak_chunk(name),
               "overrides": 0, "new": 0, "unmatched": 0}

    def add(level, title, detail="", hint=""):
        r["findings"].append({"level": level, "title": title,
                              "detail": detail, "hint": hint})

    # ---------- 0. 能不能读 ----------
    try:
        pk = P.read_pak_index(src)
        rels = pk.all_paths()
        mount = pk.mount_point
    except Exception as ex:
        r["error"] = f"{type(ex).__name__}: {ex}"
        add(LEVEL_ERROR, "读不了这个 pak",
            r["error"], "文件损坏，或者不是标准 UE pak")
        return r

    r["ok"] = True
    r["mount"] = mount
    r["entries"] = len(rels)
    fulls = engine_paths(mount, rels)
    add(LEVEL_INFO, f"挂载点 {mount!r}", f"条目 {len(rels)} 条")

    # ---------- 1. 文件名 ----------
    if not PATCH_RE.search(name):
        add(LEVEL_ERROR, "文件名不是 `_P.pak` 结尾",
            f"实际：{name}",
            "指南：主线 pak 存在时，补丁 pak 必须以 _P 结尾，否则很可能不加载")
    else:
        add(LEVEL_OK, "文件名以 `_P.pak` 结尾")
    if r["chunk"] is None:
        add(LEVEL_WARN, "文件名解析不出 pakchunk 号",
            f"实际：{name}",
            "格式应为 pakchunk<N>-<名字>_P.pak；数字决定加载顺序")
    else:
        add(LEVEL_INFO, f"加载顺序 pakchunk{r['chunk']}",
            "数字越大越优先（后挂载的覆盖先挂载的）")

    # ---------- 2. 挂载点是否存在 ----------
    # ★ 注意：纯新增内容的模组（新地图、新武器）可以用一套【全新的命名空间】，
    #   官方清单里当然找不到 —— 那是正常的，不是错误。
    #   只有「想覆盖官方、却挂到了游戏不访问的目录」才是真的错。
    root = mount_root(mount)
    top = root.split("/")[0] if root else ""
    if not mount.strip("/"):
        add(LEVEL_WARN, "挂载点是空的", "条目没有挂到任何目录下")
    elif not root:
        add(LEVEL_OK, "挂载点 = 游戏根 `../../../`")
    elif top not in ("readyornot", "engine"):
        add(LEVEL_ERROR, f"挂载点不在游戏命名空间里：{root}",
            f"第一层是 {top!r}，游戏只从 ReadyOrNot/ 和 Engine/ 下读内容",
            "挂载点要形如 ../../../ReadyOrNot/Content/")
    elif official is not None and official.has_full_index:
        prefixed = sum(1 for f in official.full if f.startswith(root + "/"))
        if prefixed:
            add(LEVEL_OK, f"挂载点在游戏里存在（{prefixed:,} 条官方路径在它下面）")
        else:
            r["mount_unknown"] = True     # 先记下，等算完覆盖数再定性
    else:
        add(LEVEL_INFO, f"挂载点 {root or '(游戏根)'}",
            "（没有官方清单，无法确认它是否存在）")

    # ---------- 3. 路径对不对得上官方 ----------
    if official is not None and official.has_full_index:
        over, new = [], []
        variant = 0
        for rel, full in zip(rels, fulls):
            if full in official.full:
                over.append(rel)
                continue
            # 模组路径多写/少写了一层 ReadyOrNot 之类，靠候选路径救回来
            cands = RC.OfficialAssets.stem_variants(mount, rel)
            if any(c in official.full for c in cands):
                over.append(rel)
                variant += 1
            else:
                new.append(rel)
        r["overrides"] = len(over)
        r["new"] = len(new)
        r["unmatched"] = len(new)
        add(LEVEL_OK, f"覆盖官方资产 {len(over)} 条"
                      + (f"（其中 {variant} 条靠路径变体才对上）" if variant else ""),
            "" if not over else "例：" + "、".join(
                sorted(over)[:3]))
        add(LEVEL_INFO, f"游戏里没有的新路径 {len(new)} 条",
            "" if not new else "例：" + "、".join(sorted(new)[:3]))
        if not over:
            if r.get("mount_unknown"):
                add(LEVEL_WARN,
                    f"挂载点在官方清单里找不到，而且这个模组【不覆盖任何官方资产】",
                    "整套内容都在一个官方没有的命名空间下",
                    "如果它本来就是新地图/新武器之类的纯新增模组，这是正常的；"
                    "如果它本该替换官方内容，那就是挂错地方了")
            add(LEVEL_WARN, "一条都没对上官方路径（纯新增内容的模组可忽略）",
                f"{len(new)} 条全是游戏里不存在的新路径",
                "如果它本该替换贴图/模型/蓝图/数据表，说明打包时的目录结构没对上游戏；"
                "如果本来就是新地图/新武器之类的纯新增模组，这条是正常的")
        elif r.get("mount_unknown"):
            add(LEVEL_ERROR, f"挂载点不在任何官方路径下，但模组又在覆盖官方：{root}",
                f"有 {len(over)} 条能对上官方路径（靠路径变体救回来的）",
                "挂载点选错了。它应该是「包住全部内容的最深目录」，"
                "通常形如 ../../../ReadyOrNot/Content/")
        elif len(over) < max(1, len(rels) // 100):
            add(LEVEL_WARN, f"覆盖的资产很少（{len(over)}/{len(rels)}）",
                "大部分内容游戏里没有对应项",
                "如果效果不明显，可能是这个原因")
    else:
        add(LEVEL_WARN, "没有官方清单，跳过路径比对",
            "无法判断模组到底改了什么",
            "先生成清单（界面勾「先生成官方资产清单」，几秒）")

    # ---------- 4. 加载顺序冲突（模组之间） ----------
    if peers is None and paks_dir:
        peers = scan_peer_paks(paks_dir, src)
    if peers:
        my_set = set(fulls)
        my_chunk = r["chunk"] if r["chunk"] is not None else 0
        lose, win, tie = [], [], []
        for pr in peers:
            common = my_set & pr["full"]
            if not common:
                continue
            pc = pr["chunk"] if pr["chunk"] is not None else 0
            if pc > my_chunk:
                lose.append((pr["name"], pc, len(common)))
            elif pc < my_chunk:
                win.append((pr["name"], pc, len(common)))
            else:
                tie.append((pr["name"], pc, len(common)))
        for nm, pc, n in lose:
            add(LEVEL_ERROR, f"被 {nm} 覆盖（它 pakchunk{pc} > 你的 pakchunk{my_chunk}）",
                f"有 {n} 条路径重复",
                "同一个路径数字大的赢。要让它生效，把你的 pakchunk 号改大"
                "（改名即可，指南：改数字就能改加载顺序）")
        for nm, pc, n in tie:
            add(LEVEL_WARN, f"和 {nm} 抢同一个 pakchunk{pc}，谁生效不确定",
                f"有 {n} 条路径重复",
                "两个 pak 加载顺序相同时谁赢没有保证。把其中一个的 pakchunk 号改掉")
        for nm, pc, n in win:
            add(LEVEL_INFO, f"你覆盖了 {nm}（你 pakchunk{my_chunk} > 它 pakchunk{pc}）",
                f"有 {n} 条路径重复")
        if not lose and not win and not tie:
            add(LEVEL_OK, "和已装的其他模组没有路径冲突")
    elif paks_dir:
        add(LEVEL_INFO, "没找到其他已装模组，跳过加载顺序检查")

    # ---------- 5. 资产成组完整性 ----------
    groups: dict[str, list[str]] = {}
    for rel in rels:
        groups.setdefault(RC.asset_stem(rel), []).append(rel)
    orphans, missing = [], []
    for stem, members in groups.items():
        low = [m.lower() for m in members]
        if stem == members[0] and not any(
                m.endswith((".uasset", ".umap")) for m in low):
            continue                    # 不是可识别资产（.ini 之类）
        has_pkg = any(m.endswith((".uasset", ".umap")) for m in low)
        if not has_pkg and any(m.endswith((".uexp", ".ubulk", ".uptnl"))
                               for m in low):
            orphans.append(stem)
            continue
        if has_pkg and not any(m.endswith(".uexp") for m in low):
            missing.append(stem)
    if orphans:
        add(LEVEL_ERROR, f"孤儿条目 {len(orphans)} 个（只有 .uexp/.ubulk，没有 .uasset）",
            "、".join(orphans[:3]))
    if missing:
        add(LEVEL_WARN, f"{len(missing)} 个资产只有 .uasset 没有 .uexp",
            "、".join(missing[:3]),
            "UE 里两者通常成对出现，缺一半可能读不出来")
    if not orphans and not missing:
        add(LEVEL_OK, f"资产成组完整（{len(groups)} 个资产，无孤儿/缺件）")

    # ---------- 5a. 地图模组：转换救不了 ----------
    r["is_map"] = any(x.lower().endswith(".umap") for x in rels)
    if r["is_map"]:
        umaps = [x for x in rels if x.lower().endswith(".umap")]
        add(LEVEL_WARN, "这是地图模组 —— 转换（剥资产）救不了它",
            "、".join(umaps[:3]),
            "自定义地图每次游戏大版本更新都必须由作者【重新烤】。"
            "官方 Boiling Point 更新日志点名过："
            "BP_Reportable_Actor_V3 里的语音节点被删掉了，"
            "未更新的关卡会崩溃。卡加载/闪退基本都是这个原因，"
            "重新打包改变不了任何东西 —— 只能等作者更新或换图。")

    # ---------- 5b. 压缩方式必须和游戏一致 ----------
    mine_methods = [m for m in pk.compression_methods if m]
    r["methods"] = mine_methods
    game_methods = game_compression_methods(paks_dir) if paks_dir else []
    if mine_methods and game_methods:
        if set(mine_methods) - set(game_methods):
            add(LEVEL_ERROR,
                f"压缩方式 {mine_methods} 和游戏本体 {game_methods} 不一致",
                f"游戏本体用的是 {game_methods}，这个包用的是 {mine_methods}",
                "游戏如果没把对应的解码器编进去，读这个包的资产就会失败 —— "
                "表现就是卡在加载页面。用能输出 "
                f"{'/'.join(game_methods)} 的工具重新打包"
                "（社区打包脚本用的是 -compressionformats=Oodle）")
        else:
            add(LEVEL_OK, f"压缩方式 {mine_methods} 和游戏本体一致")

    # ---------- 6. 抽样看包格式（.uasset 头部魔数） ----------
    # 只读索引的话拿不到数据区，所以直接按偏移 seek 读 4 个字节 —— 不用把
    # 整个 pak（可能上 GB）读进内存。压缩条目没法直接看，跳过。
    idx = pk.read_directory_index("fdi") or pk.read_directory_index("phi")
    loc2rel = {}
    for dname, files in idx.items():
        for fname, loc in files.items():
            loc2rel[loc] = (dname + fname).lstrip("/")
    loc2e = pk.location_map()

    sampled = bad_magic = 0
    bad_samples: list[str] = []
    with open(src, "rb") as fh:
        for loc, pe in loc2e.items():
            if sampled >= 20:
                break
            rel = loc2rel.get(loc, "")
            if not rel.lower().endswith((".uasset", ".umap")):
                continue
            if pe.size != pe.uncompressed_size:
                continue                     # 压缩的，直接看不了
            fh.seek(pe.offset + pe.header_size(pk.version))
            head = fh.read(4)
            if len(head) < 4:
                break
            sampled += 1
            if head != PKG_MAGIC:
                bad_magic += 1
                if len(bad_samples) < 3:
                    bad_samples.append(f"{rel}（头部 {head.hex(' ')}）")
    if sampled and not bad_magic:
        add(LEVEL_OK, f"抽样 {sampled} 个未压缩 .uasset，UE 包魔数都正确")
    elif bad_magic:
        add(LEVEL_ERROR, f"{bad_magic}/{sampled} 个 .uasset 头部魔数不对",
            "、".join(bad_samples),
            "不是合法的 UE 包，游戏会拒绝加载")
    elif not any(r.lower().endswith((".uasset", ".umap")) for r in rels):
        add(LEVEL_INFO, "包里没有 .uasset/.umap，跳过包格式抽样")
    else:
        add(LEVEL_INFO, "所有 .uasset 都是压缩的，跳过了包格式抽样")

    # ---------- 7. 官方工具复核 ----------
    if verify:
        v = RC.unrealpak_check(src)
        if v.get("ok"):
            add(LEVEL_OK, f"官方 UnrealPak 复核通过（-List {v.get('listed')} 条 / -Test rc=0）")
        elif v.get("ok") is False:
            add(LEVEL_ERROR, "官方 UnrealPak 复核失败", str(v),
                "pak 结构有问题，游戏多半也读不了")
        else:
            add(LEVEL_INFO, f"未做官方复核：{v.get('reason', v)}")

    # ---------- 结论 ----------
    n_err = sum(1 for f in r["findings"] if f["level"] == LEVEL_ERROR)
    n_warn = sum(1 for f in r["findings"] if f["level"] == LEVEL_WARN)
    r["errors"] = n_err
    r["warnings"] = n_warn
    if n_err:
        r["verdict"] = "有问题：按上面的 ✘ 逐条修"
    elif n_warn:
        r["verdict"] = "基本可用，但有需要留意的地方"
    else:
        r["verdict"] = "没发现结构性问题"
    return r


def render(r: dict, log=None) -> None:
    """把体检结果打出来。"""
    emit = log or print
    emit("")
    emit(f"── {r['name']}")
    if not r["ok"]:
        for f in r["findings"]:
            emit(f"   {_ICON[f['level']]} {f['title']}")
            if f.get("hint"):
                emit(f"      → {f['hint']}")
        emit(f"   ── 结论：无法检查")
        return
    for f in r["findings"]:
        emit(f"   {_ICON[f['level']]} {f['title']}")
        if f.get("detail"):
            emit(f"      {f['detail']}")
        if f.get("hint"):
            emit(f"      → {f['hint']}")
    emit(f"   ── 结论：{r['verdict']}")


# ---------------------------------------------------------------------------
# 先诊断再转换：这个模组到底能不能转、值不值得转？
# ---------------------------------------------------------------------------
VERDICTS = {
    "convert":  ("可以转换", "有该剥的照抄官方资产，转换有用"),
    "no_change": ("不用转换", "没有可剥的内容，原样用就行"),
    "do_not":   ("不建议转换", "官方同路径的资产全是模组自己改过的，转换剥不到东西，"
                              "强行剥反而会毁掉模组"),
    "blocked":  ("转换也修不好", "结构上有硬伤，转换解决不了，得先修那些问题"),
    "delete":   ("该删掉", "内容已全部被官方取代"),
    "unreadable": ("读不了", "不是合法 pak"),
}


def assess(src: str, official: RC.OfficialAssets | None = None, *,
           paks_dir: str | None = None, peers: list[dict] | None = None,
           verify: bool = False, refs: bool = False, log=None) -> dict:
    """先诊断：给出「该不该转换」的结论，然后再决定要不要转。

    refs=True 时额外做【引用分析】（慢：要把每个 .uasset 解开读引用表）。

    返回 dict：{verdict, label, why, reasons[], should_convert, health, diag,
                kinds[], kind_note, rename, refs}
    """
    emit = log or (lambda *_a, **_k: None)
    # check() 内部会自己扫 peers，但 assess 自己算冲突也要用 —— 先补上，
    # 否则「没前缀 + 被别家压着」会漏掉，改完名刚好和人家同号。
    if peers is None and paks_dir:
        peers = scan_peer_paks(paks_dir, src)
    h = check(src, official, paks_dir=paks_dir, peers=peers, verify=verify)
    out = {"src": src, "name": h["name"], "ok": h["ok"], "health": h,
           "reasons": [], "diag": None, "kinds": [], "kind_note": "",
           "refs": None,
           "rename": {"needed": False, "new_name": h["name"], "reasons": []}}

    def finish(code):
        label, why = VERDICTS[code]
        # 引用分析（如果做了）的结论：放在最后说，它是「转换救不了」那一类问题
        rr = out.get("refs") or {}
        br = rr.get("broken") or {}
        n_ren, n_gone = len(br.get("renamed") or []), len(br.get("gone") or [])
        if n_ren:
            out["reasons"].append(
                f"引用分析：{n_ren} 个引用的包名游戏里已经没有，但存在同名主干的新"
                f"资产 —— 多半是这次更新改名/搬了目录（转换修不了，得等作者更新）")
        if n_gone:
            out["reasons"].append(
                f"引用分析：{n_gone} 个引用的包名游戏里彻底没有 —— "
                f"作者没打包进来，或者官方把它删了")
        out["verdict"] = code
        out["label"] = label
        out["why"] = why
        out["should_convert"] = (code == "convert")
        return out

    # ---- 0. 类型识别 + 要不要改名（这两项跟后面的结论无关，先算） ----
    over_rels: list[str] = []
    if h["ok"]:
        try:
            pk = P.read_pak_index(src)
            rels = pk.all_paths()
            if official is not None and official.has_full_index:
                for r in rels:
                    cands = RC.OfficialAssets.stem_variants(pk.mount_point, r)
                    if any(c in official.full for c in cands):
                        over_rels.append(r)
            out["kinds"] = classify_mod(rels, over_rels)
            out["kind_note"] = "；".join(KIND_NOTE[k] for k in out["kinds"]
                                        if k in KIND_NOTE)
        except Exception:
            pass

    # 引用分析（可选，慢）：它引用的资产游戏里还在不在
    if refs and h["ok"]:
        out["refs"] = ref_report(src, official, log=emit)

    # 加载顺序冲突：谁用更大的 chunk 号压着我 -> 该怎么改名
    out["rename"] = rename_plan_for(src, paks_dir=paks_dir, peers=peers)

    # ---- 1. 读不了 ----
    if not h["ok"]:
        out["reasons"].append(f"读不了：{h.get('error', '')}")
        return finish("unreadable")

    # ---- 2. 转换修不了的结构硬伤 ----
    blockers = []
    for f in h["findings"]:
        t = f["title"]
        if f["level"] != LEVEL_ERROR:
            continue
        if "挂载点不在游戏命名空间里" in t or "挂载点不在任何官方路径下" in t:
            blockers.append(t.split("：")[0] + " —— 模组的资产挂在游戏从不访问的目录上")
        elif "不是 `_P.pak` 结尾" in t:
            blockers.append("文件名不是 _P.pak 结尾 —— 很可能根本不加载"
                            "（可以用「自动改名修复」补上）")
        elif "孤儿条目" in t:
            blockers.append("有孤儿条目（只有 .uexp/.ubulk 没有 .uasset）—— "
                            "转换只会剥不会补，修不了")
        elif "头部魔数不对" in t:
            blockers.append("包内容不是合法 UE 包 —— 转换修不了")
        elif "覆盖" in t and "被 " in t:
            blockers.append(t + " —— 换个 pakchunk 号比转换更管用"
                            "（可以用「自动改名修复」调）")
    if blockers:
        out["reasons"].extend(blockers)
        return finish("blocked")

    # ---- 3. 转换到底会做什么 ----
    if official is None or not official.has_full_index:
        out["reasons"].append("没有官方清单，判断不了转换会剥掉什么")
        return finish("blocked")

    d = RC.diagnose(src, official, verbose=False)
    if not d.ok:
        out["reasons"].append(f"诊断失败：{d.error}")
        return finish("unreadable")
    out["diag"] = {
        "entries": d.total_entries, "recovered": d.recovered,
        "dropped": d.dropped, "kept": d.kept,
        "matched_full": d.matched_full, "matched_bare": d.matched_bare,
        "kept_modified": len(d.kept_modified),
        "kept_modified_paths": [r for _s, r, _m, _o in d.kept_modified][:8],
    }

    if d.recovered < d.total_entries:
        out["reasons"].append(
            f"只解析出 {d.recovered}/{d.total_entries} 条路径 —— 转换会丢文件，"
            f"工具已拒绝重新打包")
        return finish("blocked")

    if d.kept == 0 and d.dropped > 0:
        out["reasons"].append(f"剥离 {d.dropped} 条后一条不剩")
        return finish("delete")

    if d.dropped > 0:
        out["reasons"].append(
            f"会剥掉 {d.dropped} 条「和官方一模一样（照抄）」的资产 —— "
            f"这正是转换要干的事，剥掉零损失")
        if d.kept_modified:
            out["reasons"].append(
                f"同时保留 {len(d.kept_modified)} 个模组自己改过的资产（不会误伤）")
        return finish("convert")

    if d.kept_modified:
        out["reasons"].append(
            f"官方已有同路径的资产有 {len(d.kept_modified)} 个，但它们"
            f"【全都是模组自己改过的】—— 那是模组的功能本身，不能剥")
        out["reasons"].append("没有任何「照抄官方」的资产需要清理")
        return finish("do_not")

    out["reasons"].append("没有可剥的内容（没有和官方同路径的照抄资产）")
    return finish("no_change")


# ---------------------------------------------------------------------------
# 模组类型识别：这个 mod 到底是干嘛的？本工具能不能改它？
# ---------------------------------------------------------------------------
def _base(rel: str) -> str:
    return rel.rsplit("/", 1)[-1].lower()


def classify_mod(rels, over) -> list[str]:
    """按「它真正在改什么」判断类型。

    rels: 全部条目
    over: 能对上官方路径的条目（= 它真的在覆盖官方的东西）
    """
    low = [r.lower() for r in rels]
    ov = [r.lower() for r in over]
    kinds = []

    def hit(seq, pats):
        return any(any(p in r for p in pats) for r in seq)

    def name_hit(seq, prefixes):
        return any(_base(r).startswith(prefixes) for r in seq)

    if any(r.endswith(".umap") for r in low):
        kinds.append("地图")
    if hit(ov, ("/textures/", "/texture/")) or name_hit(ov, ("t_",)):
        kinds.append("贴图替换")
    if hit(ov, ("/materials/", "/material/")) or name_hit(
            ov, ("m_", "mi_", "mpc_")):
        kinds.append("材质替换")
    if hit(ov, ("/meshes/", "/staticmeshes/", "/skeletalmeshes/")) or name_hit(
            ov, ("sm_", "sk_")):
        kinds.append("网格替换")
    if hit(ov, ("/blueprints/", "/blueprint/", "/logicmods/")) or name_hit(
            ov, ("bp_", "wbp_")):
        kinds.append("蓝图/逻辑")
    if hit(ov, ("/datatables/", "/datatable/")) or name_hit(
            ov, ("dt_",)) or any("datatable" in _base(r) for r in ov):
        kinds.append("数据表")
    if hit(ov, ("/audio/", "/sounds/", "/fmod/")):
        kinds.append("音频替换")
    if hit(ov, ("/animations/", "/anim/")):
        kinds.append("动画")
    if not over:
        kinds.append("纯新增内容")
    if not kinds:
        kinds.append("其它")
    return kinds


# 每类模组「转换（剥资产）有没有用」的说明
KIND_NOTE = {
    "地图": "自定义地图必须作者重新烤，重打包改变不了任何东西",
    "贴图替换": "覆盖官方贴图是正常 mod 行为，没有可剥的东西",
    "材质替换": "覆盖官方材质是正常 mod 行为，没有可剥的东西",
    "网格替换": "覆盖官方网格是正常 mod 行为，没有可剥的东西",
    "蓝图/逻辑": "蓝图最容易因为游戏更新而崩溃 —— 但也最可能是模组的功能本身",
    "数据表": "数据表是模组改数值的主要手段，剥了就等于删功能",
    "音频替换": "覆盖官方音频是正常 mod 行为",
    "动画": "覆盖官方动画是正常 mod 行为",
    "纯新增内容": "全是游戏里没有的新路径，没有可剥的东西",
    "其它": "无法归类",
}


# ---------------------------------------------------------------------------
# 自动改名修复：补 _P 后缀、解决加载顺序冲突
# ---------------------------------------------------------------------------
def plan_rename(src: str, *, peers: list[dict] | None = None,
                conflict_chunks: list[int] | None = None) -> dict:
    """算出这个 pak 该怎么改名才能生效。只做「确定性」的改动：

      1. 文件名不是 `_P.pak` 结尾 -> 补上（指南点名的头号错误）
      2. 文件名解析不出 pakchunk 号 -> 补 `pakchunk9999-` 前缀
      3. 同名路径被 pakchunk 号更大的模组压着 -> 把号提到比它大

    返回 {needed, new_name, reasons[]}；不改任何文件。
    """
    name = os.path.basename(src)
    stem = name[:-4] if name.lower().endswith(".pak") else name
    new_stem = stem
    reasons = []

    if not PATCH_RE.search(name):
        new_stem += "_P"
        reasons.append("补上 `_P.pak` 后缀（主线 pak 存在时补丁 pak 必须有它，"
                       "否则很可能不加载）")

    if pak_chunk(name) is None:
        new_stem = "pakchunk9999-" + new_stem
        reasons.append("补上 `pakchunk9999-` 前缀（没这个前缀解析不出加载顺序）")

    if conflict_chunks:
        my = pak_chunk(new_stem) or 0
        top = max(conflict_chunks)
        if top >= my:
            new_stem = re.sub(r"^pakchunk\d+-", f"pakchunk{top + 1}-",
                              new_stem, count=1, flags=re.IGNORECASE)
            reasons.append(f"pakchunk 号从 {my} 提到 {top + 1}"
                           f"（现在被 pakchunk{top} 压着，同一个路径数字大的赢）")

    out = {"needed": bool(reasons), "new_name": new_stem + ".pak",
           "reasons": reasons}
    return out


def rename_plan_for(src: str, *, paks_dir: str | None = None,
                    peers: list[dict] | None = None) -> dict:
    """只算「这个 pak 该怎么改名」，不做完整诊断（转换流程里顺带用）。

    assess() 也走这条路径 —— 保证「诊断里看到的改名建议」和
    「转换时实际改的名」永远是同一个结果，不会两处逻辑各说各话。
    返回 plan_rename() 的结果，外加 readable（这个 pak 本身读得动吗）。
    """
    name = os.path.basename(src)
    if peers is None and paks_dir:
        peers = scan_peer_paks(paks_dir, src)
    conflicts: list[int] = []
    readable = False
    if peers:
        try:
            pk = P.read_pak_index(src)
            readable = True
            mine = set(engine_paths(pk.mount_point, pk.all_paths()))
            # ★ 解析不出 chunk 号时按 0 算 —— 不然「没前缀 + 被别家压着」
            #   会被漏掉，改完名刚好和人家同号，谁生效又变成不确定。
            my = pak_chunk(name)
            my_rank = my if my is not None else 0
            for pr in peers:
                if mine & pr["full"]:
                    pc = pr["chunk"]
                    if pc is not None and pc >= my_rank:
                        conflicts.append(pc)
        except Exception:
            pass
    else:
        # 没有同目录模组可比较，但文件名本身还是能读的
        try:
            P.read_pak_index(src)
            readable = True
        except Exception:
            pass
    rn = plan_rename(src, conflict_chunks=conflicts)
    rn["readable"] = readable
    return rn


def should_fix_name(a: dict) -> bool:
    """该不该给这个诊断结果做「自动改名修复」。

    只做确定性的事（补 `_P` / 补前缀 / 调 chunk 号），而且**不碰读不动的文件**——
    给一个损坏的 pak 生成改名副本，只会让人以为它被修好了。
    """
    rn = a.get("rename") or {}
    if not rn.get("needed"):
        return False
    if not rn.get("readable", True):
        return False
    return a.get("verdict") != "unreadable"


# ---------------------------------------------------------------------------
# 引用分析（v1.7.0）：模组引用的资产，游戏更新后还在不在？
# ---------------------------------------------------------------------------
def ref_report(src: str, official, *, limit: int = 0, log=None) -> dict:
    """调 ronrefs 做引用分析。缺模块 / 缺 oo2core 都明确降级，绝不抛异常。"""
    name = os.path.basename(src)
    blank = {"src": src, "name": name, "ok": False, "error": "", "entries": 0,
             "assets": 0, "read": 0, "skipped": 0, "skipped_reason": "",
             "refs": 0, "refs_alive": 0, "refs_own": 0, "missing": [],
             "misplaced": [], "truncated": False, "seconds": 0.0}
    if official is None or not getattr(official, "has_full_index", False):
        blank["error"] = "没有官方清单，引用分析没法对账"
        return blank
    try:
        import ronrefs as RR
    except Exception as ex:
        blank["error"] = f"引用分析模块加载失败：{type(ex).__name__}: {ex}"
        return blank
    try:
        return RR.analyze_pak(src, official, limit=limit, log=log)
    except Exception as ex:
        blank["error"] = f"{type(ex).__name__}: {ex}"
        return blank


def render_refs(r: dict, log=None) -> None:
    """打印引用分析结果（模块不在就跳过）。"""
    emit = log or print
    try:
        import ronrefs as RR
    except Exception:
        return
    try:
        RR.render_report(r, log=emit)
    except Exception as ex:
        emit(f"   （引用分析结果打印失败：{type(ex).__name__}: {ex}）")


def apply_rename(src: str, new_name: str, outdir: str) -> str:
    """复制一份改成新名字放进 outdir。**不动原文件**，也绝不覆盖已有文件。"""
    import shutil
    os.makedirs(outdir, exist_ok=True)
    dst = os.path.join(outdir, new_name)
    if os.path.abspath(dst) == os.path.abspath(src):
        return src
    n = 1
    base, ext = os.path.splitext(dst)
    while os.path.exists(dst):
        dst = f"{base}({n}){ext}"
        n += 1
    shutil.copy2(src, dst)
    return dst


def render_assess(a: dict, log=None) -> None:
    """打印「先诊断再转换」的结论：类型 + 能不能改 + 要不要改名。"""
    emit = log or print
    emit("")
    emit(f"── {a['name']}")
    kinds = a.get("kinds") or []
    if kinds:
        emit(f"   类型：{' + '.join(kinds)}")
        for k in kinds:
            if k in KIND_NOTE:
                emit(f"        · {k}：{KIND_NOTE[k]}")
    if a.get("health", {}).get("ok"):
        h = a["health"]
        for f in h["findings"]:
            if f["level"] in (LEVEL_ERROR, LEVEL_WARN):
                emit(f"   {_ICON[f['level']]} {f['title']}")
                if f.get("hint"):
                    emit(f"      → {f['hint']}")
    for r in a["reasons"]:
        emit(f"   · {r}")
    if a.get("refs") is not None:
        render_refs(a["refs"], log=emit)
    rn = a.get("rename") or {}
    if rn.get("needed"):
        if rn.get("readable", True):
            emit(f"   ✎ 改名就能修：{a['name']}  ->  {rn['new_name']}")
            for r in rn["reasons"]:
                emit(f"        · {r}")
        else:
            emit(f"   ✎ 名字也不对（该叫 {rn['new_name']}），"
                 f"但这个 pak 本身读不动 —— 改名救不了坏文件")
    emit(f"   ── 诊断结论：【{a['label']}】{a['why']}")


def print_assess_table(results, log=None) -> None:
    """汇总表：哪些该转、哪些不用转、哪些转了也没用。"""
    emit = log or print
    emit("")
    emit("=" * 66)
    emit("诊断汇总")
    buckets: dict[str, list[str]] = {}
    for a in results:
        buckets.setdefault(a["label"], []).append(a["name"])
    for label in ("可以转换", "不用转换", "不建议转换", "转换也修不好",
                  "该删掉", "读不了"):
        if label in buckets:
            emit(f"   {label:<10} {len(buckets[label])} 个")
            for n in buckets[label]:
                emit(f"        {n}")


def main() -> int:
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(
        description="模组体检：为什么这个 pak 装了没效果？（只诊断，不改文件）")
    ap.add_argument("target", help="单个 .pak，或装着模组的目录")
    ap.add_argument("--manifest", default=None,
                    help="官方资产清单（默认自动找程序旁边的 manifest_local.json）")
    ap.add_argument("--game-paks", default=None,
                    help="游戏 Paks 目录（用来查加载顺序冲突；默认自动找）")
    ap.add_argument("--verify", action="store_true",
                    help="额外调用官方 UnrealPak -List/-Test")
    ap.add_argument("--assess", action="store_true",
                    help="先诊断再转换：只回答「这个模组是什么类型、能不能改、"
                         "值不值得改」，不改任何文件")
    ap.add_argument("--refs", action="store_true",
                    help="引用分析：读模组资产里记的包路径，查它引用的资产"
                         "游戏更新后还在不在（较慢；压缩资产需要本机有 oo2core）")
    ap.add_argument("--fix-names", metavar="输出目录", default=None,
                    help="自动改名修复：给缺 _P 后缀的补上、给加载顺序被压的"
                         "调大 pakchunk 号，结果复制到指定目录（原文件不动）")
    ap.add_argument("--json", default=None, help="把结果写入 JSON")
    args = ap.parse_args()

    if not os.path.exists(args.target):
        print(f"路径不存在：{args.target}")
        return 1

    manifest = args.manifest
    if not manifest:
        for n in ("manifest_local.json", "game_manifest_full.json",
                  "game_manifest.json"):
            for d in (os.getcwd(), os.path.dirname(os.path.abspath(__file__)),
                      os.path.dirname(os.path.abspath(__file__)) + os.sep + ".."):
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    manifest = p
                    break
            if manifest:
                break
    official = RC.OfficialAssets(manifest) if manifest else None
    if official is not None:
        print(f"官方清单：{official.source}")

    paks_dir = args.game_paks or RC.find_game_paks()
    if paks_dir:
        print(f"游戏 Paks：{paks_dir}")
    print("=" * 78)

    all_paks = RC.find_paks(args.target)
    if not all_paks:
        print(f"没找到 .pak：{args.target}")
        return 1
    # 体检/诊断只针对【模组】。游戏本体 pak（pakchunkN-Windows.pak）不是模组，
    # 拿「必须有 _P 后缀」之类的规则去套它们全是误报。
    paks = [p for p in all_paks if not RC.is_official_pak(os.path.basename(p))]
    skipped = len(all_paks) - len(paks)
    if skipped:
        print(f"（跳过 {skipped} 个游戏本体 pak —— 体检只针对模组）")
    if not paks:
        print("这个目录里没有模组 pak（只有游戏本体）")
        return 0

    results = []
    if args.assess or args.fix_names or args.refs:
        # 先诊断再转换（可选顺带改名修复 / 引用分析）：只给结论 + 可选产物
        ares = []
        for src in paks:
            a = assess(src, official, paks_dir=paks_dir, verify=args.verify,
                       refs=args.refs)
            if args.fix_names and should_fix_name(a):
                a["fixed_path"] = apply_rename(src, a["rename"]["new_name"],
                                               args.fix_names)
            elif args.fix_names and a["rename"]["needed"]:
                a["fix_skipped"] = ("这个 pak 本身读不动 —— 改名救不了坏文件，"
                                    "没有生成副本")
            render_assess(a)
            if a.get("fixed_path"):
                print(f"   ✔ 已生成改名后的副本：{a['fixed_path']}")
            elif a.get("fix_skipped"):
                print(f"   · {a['fix_skipped']}")
            ares.append(a)
        print_assess_table(ares)
        n_conv = sum(1 for a in ares if a["should_convert"])
        n_fix = sum(1 for a in ares if a.get("fixed_path"))
        print(f"\n其中 {n_conv} 个建议转换；其余的转了也没用，甚至会更糟。")
        if n_fix:
            print(f"      {n_fix} 个已生成改名后的副本（原文件没动）。")
        if args.json:
            import json
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump([{k: v for k, v in a.items() if k != "health"}
                           for a in ares], f, ensure_ascii=False, indent=2)
            print(f"\n报告已写入 {args.json}")
        return 0

    for src in paks:
        r = check(src, official, paks_dir=paks_dir, verify=args.verify)
        render(r)
        if args.refs and r["ok"]:
            render_refs(ref_report(src, official), log=print)
        results.append(r)

    n_bad = sum(1 for r in results if not r["ok"] or r.get("errors"))
    print("\n" + "=" * 78)
    print(f"体检 {len(results)} 个 pak：{len(results)-n_bad} 个没问题，{n_bad} 个有问题")
    for r in results:
        print(f"   {r['name']}  ->  {r.get('verdict', r.get('error', ''))}")

    if args.json:
        import json
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
