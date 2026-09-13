#!/usr/bin/env python3
"""
roncheck.py - Ready or Not 模组批量体检器

用途：把从 N 网下载的模组丢进来，直接得到该留还是该删的结论。

对每个模组 pak 依次判定：
  1. 容器层     —— 是否合法 pak / 版本是否匹配当前游戏 / 索引是否完整
  2. 内容层     —— 导出资产，恢复它覆盖了哪些资产路径
  3. 冗余判定   —— 覆盖的资产在当前游戏里是否已存在（= 官方已内置）
  4. 冲突判定   —— 多个模组是否改同一个资产（互相冲突）

结论分类：
  [冗余]  覆盖的资产大部分官方已有  -> 建议删除（官方已内置，留着会冲突）
  [冲突]  与其它模组改同一资产      -> 需取舍
  [可用]  覆盖资产官方没有          -> 保留
  [损坏]  pak 无法解析/版本不匹配   -> 无法使用

用法:
    python tools/roncheck.py "D:\\mods\\*.pak"
    python tools/roncheck.py --dir "D:\\RoN模组" --work .roncheck
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import zlib
from dataclasses import dataclass, field, asdict

# 允许作为脚本直接运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MAGIC = struct.pack("<I", 0x5A6F12E1)
UE_PKG = 0x9E2A83C1
RON_TARGET_PAK_VERSION = 11

GAME_PAKS_GLOB = (r"E:\SteamLibrary\steamapps\common\Ready Or Not"
                  r"\ReadyOrNot\Content\Paks\*.pak")


class CheckError(Exception):
    pass


@dataclass
class ModReport:
    path: str
    name: str = ""
    size: int = 0
    ok: bool = False
    pak_version: int = 0
    version_match: bool = False
    index_valid: bool = False
    encrypted: bool = False
    entry_count: int = 0
    exported: int = 0
    export_failed: int = 0
    asset_paths: list = field(default_factory=list)
    game_has: list = field(default_factory=list)
    game_lacks: list = field(default_factory=list)
    verdict: str = ""
    verdict_detail: str = ""
    conflicts_with: list = field(default_factory=list)
    export_errors: list = field(default_factory=list)
    raw_paths: int = 0
    raw_fnames: int = 0
    source: str = ""
    upak_entries: int = 0
    upak_by_ext: dict = field(default_factory=dict)
    error: str = ""


# --------------------------------------------------------------------------
# 依赖延迟导入（避免无游戏时 import 失败）
# --------------------------------------------------------------------------
def _import_tools():
    """RonPak 实际定义在 ronextract.py（支持导出/解压）。"""
    from ronextract import RonPak
    return RonPak


# --------------------------------------------------------------------------
def _load_manifest(path: str) -> set:
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    stems = set()
    for n in m.get("names", []):
        for e in (".uasset", ".uexp", ".umap", ".ubulk"):
            if n.endswith(e):
                stems.add(n[:-len(e)])
                break
        else:
            stems.add(n)
    return stems


def _try_unrealpak(pak_path: str, game_stems: set):
    """首选方法：用官方 UnrealPak 列条目（权威，含多块条目与精确 SHA1）。

    返回 (assets, detail) 或 None（UnrealPak 不可用时）。
    """
    try:
        from ronunreal import find_unrealpak, list_pak
    except Exception:
        return None
    if not find_unrealpak():
        return None
    try:
        listing = list_pak(pak_path)
    except Exception as ex:
        return None
    if not listing.entries:
        return None

    # 只统计 .uasset —— 一个资产对应一个 uasset
    mod_assets = set()
    for e in listing.entries:
        if e.ext == ".uasset":
            mod_assets.add(re.sub(r"\.uasset$", "",
                                  os.path.basename(e.path)))
    detail = {
        "entries": len(listing.entries),
        "mount": listing.mount_point,
        "by_ext": listing.by_ext(),
        "assets": len(mod_assets),
    }
    return mod_assets, detail


def _scan_paths_raw(pak_path: str) -> tuple[set, set]:
    """备用解析：直接从 pak 原始字节提取文件名与资产路径（无需解压）。

    为什么需要：
        某些 pak 里存在多块(multi-block)压缩条目，逐块解码依赖块表格式，
        若块表解析不对就会整条解压失败。但【文件的路径/名字在 pak 里是明文】，
        所以即使解压不出来，也能恢复"这个模组包含哪些文件"。

    返回 (完整 /Game/ 路径集合, 文件名集合)。
    """
    with open(pak_path, "rb") as f:
        d = f.read()
    paths: set[str] = set()
    for m in re.finditer(rb"/(?:Game|Script)/[ -~]{3,200}", d):
        s = re.split(rb"[\x00-\x1f]", m.group())[0].decode("ascii", "replace")
        s = s.rstrip("\x00")
        if len(s) > 8:
            paths.add(s)
    fnames: set[str] = set()
    for m in re.finditer(
            rb"[A-Za-z0-9_\-\.]{3,120}\.(?:uasset|uexp|umap|ubulk)", d):
        fnames.add(m.group().decode("ascii", "replace"))
    return paths, fnames


def _extract_asset_paths(pak, workdir: str, limit_export: int = 0) -> tuple:
    """导出模组内容并恢复它【覆盖的资产路径】。

    ★ 关键区分：UE 包的名称表里既有【该包自身的路径】，也有它引用的
      其它对象路径。只有前者是"这个模组覆盖了什么"；
      后者是依赖，不是覆盖目标。混在一起会严重高估冗余度。
      uasset 里第一个 /Game/ 串即包自身路径。

    limit_export=0 表示不限制（默认全量，避免漏掉资产导致误判）。
    """
    ok = fail = 0
    paths: set[str] = set()
    errors: list[str] = []

    if not os.path.isdir(workdir):
        os.makedirs(workdir, exist_ok=True)

    for i, e in enumerate(pak.entries):
        try:
            data = pak.extract(e)
        except Exception as ex:
            fail += 1
            if len(errors) < 5:
                errors.append(f"#{i} loc={e.location} method={e.method} "
                              f"blocks={e.blocks} layout={e.layout}: "
                              f"{type(ex).__name__}: {ex}")
            continue
        ok += 1
        if len(data) < 4 or struct.unpack_from("<I", data, 0)[0] != UE_PKG:
            continue
        # 只取第一个 /Game/ 或 /Script/ 串 —— 即包自身的路径
        m = re.search(rb"/(?:Game|Script)/[ -~]{3,180}", data)
        if not m:
            continue
        s = m.group().decode("ascii", "replace")
        s = re.split(r"[\x00-\x1f]", s)[0].rstrip("\x00")
        if len(s) > 8:
            paths.add(s)
        if limit_export and ok >= limit_export:
            break
    return paths, ok, fail, errors


def check_mod(pak_path: str, game_stems: set, workdir: str) -> ModReport:
    rep = ModReport(path=pak_path, name=os.path.basename(pak_path))
    rep.size = os.path.getsize(pak_path)
    try:
        RonPak = _import_tools()
        pk = RonPak(pak_path)
    except Exception as ex:
        rep.error = f"{type(ex).__name__}: {ex}"
        rep.verdict = "损坏"
        rep.verdict_detail = "无法解析为 UE pak（不是 pak / 文件不完整 / 格式不支持）"
        return rep

    rep.ok = True
    rep.pak_version = pk.version
    rep.version_match = (pk.version == RON_TARGET_PAK_VERSION)
    rep.index_valid = True
    rep.encrypted = pk.encrypted_index
    rep.entry_count = len(pk.entries)

    if not rep.version_match:
        rep.verdict = "版本不匹配"
        rep.verdict_detail = (f"模组 pak v{pk.version}，当前游戏需要 "
                              f"v{RON_TARGET_PAK_VERSION}")
        return rep

    # 导出并恢复资产路径
    # ★ 首选：官方 UnrealPak 的权威条目清单
    # 若可用则跳过内部解析器导出（省时，且避免多块条目的无效报错）
    up = _try_unrealpak(pak_path, game_stems)
    if up:
        up_assets, up_detail = up
        rep.source = "UnrealPak"
        rep.upak_entries = up_detail["entries"]
        rep.upak_by_ext = up_detail["by_ext"]
        rep.asset_paths = sorted(up_assets)
        rep.exported = up_detail["entries"]
        rep.export_failed = 0
        tot = len(rep.asset_paths)
        for t in rep.asset_paths:
            if t in game_stems:
                rep.game_has.append(t)
            else:
                rep.game_lacks.append(t)
        if not game_stems:
            rep.verdict = "无法判定"
            rep.verdict_detail = "缺少游戏清单 game_manifest.json"
            return rep
        ratio = (len(rep.game_has) / tot) if tot else 0.0
        # ★ 重要区分：资产同名有两种完全不同的含义
        #   (a) 被官方取代 —— 官方新版覆盖了同一批【蓝图/逻辑资产】，会冲突崩溃
        #   (b) 资源替换   —— 正常 mod 覆盖【贴图/模型/音频】，是有意为之，不冲突
        # 仅凭资产名无法区分，必须如实告知用户，不能一概判"冗余"。
        if tot == 0:
            rep.verdict = "无法判定"
            rep.verdict_detail = "UnrealPak 清单里没有 .uasset"
        elif len(rep.game_has) == 0:
            rep.verdict = "新增内容"
            rep.verdict_detail = (
                f"{tot} 个资产官方都没有 —— 模组新增内容，不覆盖官方资产，"
                f"不会冲突")
        elif len(rep.game_lacks) == 0:
            rep.verdict = "完全覆盖"
            rep.verdict_detail = (
                f"{tot} 个资产官方全部已有（100% 覆盖）\n"
                f"需人工判断属于哪种：\n"
                f"  · 蓝图/逻辑资产被官方新版取代 → 会冲突崩溃，应删除\n"
                f"  · 贴图/模型等资源替换 → 正常 mod，保留")
        elif ratio >= 0.7:
            rep.verdict = "高覆盖风险"
            rep.verdict_detail = (
                f"{len(rep.game_has)}/{tot} 个资产官方已有（{ratio:.0%}）—— "
                f"若这些是蓝图/逻辑资产，大概率已被官方取代，建议删除；"
                f"若是资源替换则属正常。")
        else:
            rep.verdict = "少部分覆盖"
            rep.verdict_detail = (
                f"{len(rep.game_has)}/{tot} 个资产官方已有（{ratio:.0%}）—— "
                f"大部分是新增内容，少量覆盖需留意是否与官方冲突。")
        return rep

    # ---- 回退：内置解析器 ----
    sub = os.path.join(workdir, re.sub(r"[^\w]+", "_", rep.name)[:60])
    try:
        paths, okc, failc, errors = _extract_asset_paths(pk, sub)
    except Exception as ex:
        rep.error = f"导出失败 {type(ex).__name__}: {ex}"
        rep.verdict = "损坏"
        rep.verdict_detail = "pak 可解析但内容无法导出"
        return rep

    rep.exported, rep.export_failed = okc, failc
    rep.export_errors = errors

    # ---- 备用解析：直接扫原始字节提取路径（解压失败也能用）----
    raw_paths, raw_fnames = _scan_paths_raw(pak_path)
    rep.raw_paths = len(raw_paths)
    rep.raw_fnames = len(raw_fnames)

    # 资产路径归一化（取末段文件名比对游戏清单）
    targets: set[str] = set()
    for p in paths:
        rel = p.lstrip("/")
        if rel.startswith("Game/"):
            rel = rel[len("Game/"):]
        targets.add(rel)
    # 并入备用解析结果（原始字节里的文件名，去掉扩展名）
    for f in raw_fnames:
        targets.add(re.sub(r"\.(uasset|uexp|umap|ubulk|uptnl)$", "", f))
    for p in raw_paths:
        rel = p.lstrip("/")
        if rel.startswith("Game/"):
            rel = rel[len("Game/"):]
        targets.add(rel)

    # ★ 首选：官方 UnrealPak 的权威条目清单（含多块条目，精确到 uasset）
    rep.source = "内置解析器"
    rep.asset_paths = sorted(targets)

    if not game_stems:
        rep.verdict = "无法判定"
        rep.verdict_detail = ("缺少游戏清单，请先运行：\n"
                              "  python tools\\ronadapt.py --game-manifest "
                              "\"<游戏>\\Paks\\pakchunk0-Windows.pak\"")
        return rep

    for t in targets:
        base = t.split("/")[-1]
        if base in game_stems:
            rep.game_has.append(t)
        else:
            rep.game_lacks.append(t)

    tot = len(rep.game_has) + len(rep.game_lacks)
    if tot == 0:
        rep.verdict = "无法判定"
        rep.verdict_detail = "未能从模组内容中恢复出资产路径"
    elif len(rep.game_has) == 0:
        rep.verdict = "可用"
        rep.verdict_detail = f"覆盖的 {tot} 个资产在官方游戏里都不存在（模组独有内容）"
    elif len(rep.game_lacks) == 0:
        rep.verdict = "冗余"
        rep.verdict_detail = (f"覆盖的 {tot} 个资产官方游戏全部已有 —— "
                              f"官方已内置同内容，建议删除以免冲突")
    else:
        ratio = len(rep.game_has) / tot
        if ratio >= 0.7:
            rep.verdict = "高危冗余"
            rep.verdict_detail = (
                f"{len(rep.game_has)}/{tot} 个资产官方已有（{ratio:.0%}）—— "
                f"大部分内容被官方取代，冲突风险高。"
                f"官方缺失的 {len(rep.game_lacks)} 个可用 ronstrip 保留")
        else:
            rep.verdict = "部分冲突"
            rep.verdict_detail = (
                f"{len(rep.game_has)}/{tot} 个资产官方已有 —— "
                f"部分内容与官方重叠，可能冲突")
    return rep


def detect_conflicts(reports: list) -> None:
    """检测模组之间是否改同一资产。"""
    owner: dict[str, list] = {}
    for r in reports:
        for p in r.asset_paths:
            owner.setdefault(p, []).append(r.name)
    for p, names in owner.items():
        if len(names) > 1:
            for n in names:
                for r in reports:
                    if r.name == n:
                        others = [x for x in names if x != n]
                        r.conflicts_with.extend(
                            f"{p} <- {o}" for o in others)


# --------------------------------------------------------------------------
def main() -> int:
    import argparse
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(description="RoN 模组批量体检")
    ap.add_argument("patterns", nargs="*", help="pak 路径或通配符")
    ap.add_argument("--dir", help="扫描该目录下所有 .pak")
    ap.add_argument("--work", default=".roncheck", help="临时导出目录")
    ap.add_argument("--manifest", default="game_manifest.json",
                    help="游戏清单 JSON")
    ap.add_argument("--json", metavar="FILE", help="结果写入 JSON")
    ap.add_argument("--keep", action="store_true", help="保留临时导出文件")
    args = ap.parse_args()

    paks: list[str] = []
    for pat in args.patterns:
        # glob 不处理含方括号/通配符的字面路径；先当作真实文件试一次
        if os.path.isfile(pat):
            paks.append(pat)
        else:
            paks += glob.glob(pat, recursive=True)
    if args.dir:
        paks += glob.glob(os.path.join(args.dir, "**", "*.pak"), recursive=True)
    if not paks:
        # 默认扫描游戏 ~mods 与 Mods 目录
        base = os.path.dirname(GAME_PAKS_GLOB)
        for d in ("~mods", "Mods", "LogicMods"):
            paks += glob.glob(os.path.join(base, d, "*.pak"))
    paks = sorted(set(os.path.abspath(p) for p in paks))

    if not paks:
        print("没有找到要检查的 pak。")
        print("用法示例：")
        print(r'  python tools\roncheck.py "D:\下载\*.pak"')
        print(r'  python tools\roncheck.py --dir "D:\RoN模组"')
        return 1

    print(f"游戏清单      : {args.manifest} "
          f"({'已加载' if os.path.isfile(args.manifest) else '缺失'})")
    game_stems = _load_manifest(args.manifest)
    print(f"官方资产条目  : {len(game_stems):,}")
    print(f"待检模组      : {len(paks)}")
    print("=" * 78)

    reports = []
    for p in paks:
        r = check_mod(p, game_stems, args.work)
        reports.append(r)
        tag = {"新增内容": "[新增内容]  ", "完全覆盖": "[完全覆盖]  ",
               "高覆盖风险": "[高覆盖风险]", "少部分覆盖": "[少部分覆盖]",
               "可用": "[可用]    ", "冗余": "[冗余]    ",
               "高危冗余": "[高危冗余]", "部分冲突": "[部分冲突]",
               "损坏": "[损坏]    ", "版本不匹配": "[版本不符]",
               "无法判定": "[无法判定]"}.get(r.verdict, "[?]       ")
        print(f"\n{tag} {r.name}")
        print(f"      大小 {r.size/1024:.0f} KB | pak v{r.pak_version} | "
              f"条目 {r.entry_count} | 导出 {r.exported}"
              + (f" (失败 {r.export_failed})" if r.export_failed else ""))
        if r.raw_fnames:
            print(f"      原始字节扫描: {r.raw_paths} 条 /Game/ 路径, "
                  f"{r.raw_fnames} 个文件名条目")
        if r.source == "UnrealPak":
            print(f"      清单来源    : 官方 UnrealPak "
                  f"({r.upak_entries} 条目 {r.upak_by_ext})")
        if r.asset_paths:
            print(f"      恢复资产路径 {len(r.asset_paths)} 个"
                  f" | 官方已有 {len(r.game_has)} | 官方缺失 {len(r.game_lacks)}")
        if r.verdict_detail:
            for line in r.verdict_detail.split("\n"):
                print(f"      {line}")
        if r.export_errors:
            print(f"      导出失败样例（前 {len(r.export_errors)} 条）：")
            for line in r.export_errors:
                print(f"        {line}")
        if r.error:
            print(f"      错误: {r.error}")

    detect_conflicts(reports)

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    from collections import Counter
    c = Counter(r.verdict for r in reports)
    for k in ("新增内容", "少部分覆盖", "完全覆盖", "高覆盖风险",
              "版本不匹配", "损坏", "无法判定",
              "可用", "冗余", "高危冗余", "部分冲突"):
        if c.get(k):
            print(f"  {k:<10} {c[k]}")

    conflicted = [r for r in reports if r.conflicts_with]
    if conflicted:
        print()
        print("模组间冲突：")
        for r in conflicted:
            print(f"  {r.name}")
            for c2 in sorted(set(r.conflicts_with))[:8]:
                print(f"      {c2}")

    # 行动建议
    print()
    print("=" * 78)
    print("行动建议")
    print("=" * 78)
    for r in reports:
        if r.verdict == "完全覆盖":
            print(f"  需判断  {r.name}   （100% 覆盖官方资产：蓝图被取代→删；"
                  f"资源替换→留）")
        elif r.verdict == "高覆盖风险":
            print(f"  建议核查  {r.name}   （大部分资产官方已有）")
        elif r.verdict == "版本不匹配":
            print(f"  无法使用  {r.name}   （pak v{r.pak_version} != 11）")
        elif r.verdict == "损坏":
            print(f"  无法使用  {r.name}   （{r.error}）")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in reports], f,
                      ensure_ascii=False, indent=2)
        print(f"\n结果已写入 {args.json}")

    if not args.keep and os.path.isdir(args.work):
        shutil.rmtree(args.work, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
