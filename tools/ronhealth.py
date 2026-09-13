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
    root = mount_root(mount)
    if not mount.strip("/"):
        add(LEVEL_WARN, "挂载点是空的", "条目没有挂到任何目录下")
    elif not root:
        add(LEVEL_OK, "挂载点 = 游戏根 `../../../`")
    elif official is not None and official.has_full_index:
        prefixed = sum(1 for f in official.full if f.startswith(root + "/"))
        if prefixed:
            add(LEVEL_OK, f"挂载点在游戏里存在（{prefixed:,} 条官方路径在它下面）")
        else:
            add(LEVEL_ERROR, f"挂载点在游戏里【不存在】：{root}",
                "没有任何官方资产落在这个目录下",
                "模组的所有条目都挂在一个游戏从不访问的目录上 → 装了必然无效。"
                "挂载点应该是「包住全部内容的最深目录」的下拉框，"
                "通常形如 ../../../ReadyOrNot/Content/")
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
            add(LEVEL_WARN, "一条都没对上官方路径（纯新增内容的模组可忽略）",
                f"{len(new)} 条全是游戏里不存在的新路径",
                "如果它本该替换贴图/模型/蓝图/数据表，说明打包时的目录结构没对上游戏；"
                "如果本来就是新地图/新武器之类的纯新增模组，这条是正常的")
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

    # ---------- 6. 抽样看包格式（.uasset 头部魔数） ----------
    # 只读索引的话拿不到数据区，所以直接按偏移 seek 读 4 个字节 —— 不用把
    # 整个 pak（可能上 GB）读进内存。压缩条目没法直接看，跳过。
    idx = pk.read_directory_index("fdi") or pk.read_directory_index("phi")
    loc2rel = {}
    for dname, files in idx.items():
        for fname, loc in files.items():
            loc2rel[loc] = (dname + fname).lstrip("/")
    loc2e: dict[int, object] = {}
    q = 0
    for pe in pk.encoded_entries:
        loc2e[q] = pe
        q += len(P.encode_entry_index(pe))
    for i, pe in enumerate(pk.non_encodable):
        loc2e[-i - 1] = pe

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

    paks = RC.find_paks(args.target)
    if not paks:
        print(f"没找到 .pak：{args.target}")
        return 1

    results = []
    for src in paks:
        r = check(src, official, paks_dir=paks_dir, verify=args.verify)
        render(r)
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
