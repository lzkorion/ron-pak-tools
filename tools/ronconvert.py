#!/usr/bin/env python3
"""ronconvert.py - 旧模组自动检测 + 转换：把整个文件夹的 .pak 批量转成可用模组。

做什么
    把一个目录（或若干 .pak）丢进来，逐个：
      1. 【检测】pak 是否可读、版本、条目数、路径、哪些内容官方已内置
      2. 【判定】找出会导致崩溃/卡加载的「冲突型资产」（蓝图/逻辑类）
      3. 【转换】把这些冲突资产剥掉，用自研写入器重新打包
      4. 【验证】可选调用官方 UnrealPak -List/-Test 复核
    原文件不动，转换结果放到输出目录。

为什么只剥「冲突型」而不是「官方已有就全剥」
    「官方已有」有两种完全不同的含义：
      · 蓝图/逻辑资产被官方新版取代  -> 旧编译产物 vs 新编译产物，会冲突崩溃 -> 应剥离
      · 贴图/模型/音频等资源替换      -> 覆盖官方是正常 mod 行为（如血腥贴图）-> 必须保留
    所以默认采用【保守白名单】：只剥离
      「扩展名是 .uasset/.umap 且路径像蓝图/逻辑/数据资产」的官方同名条目，
    其余一律保留。宁可少剥，不可把正常 mod 改坏。

★ 「同名」不等于「同路径」
    上面那条还不够：判定「官方已有」必须要求**路径完全一致**（full）。
    真实模组常写成 mount='../../../ReadyOrNot/Content/' + 'ReadyOrNot/Character/...'
    （路径里多带一层 ReadyOrNot），于是和官方永远「同路径匹配不上」，
    但**文件名**和官方某个资产一样。如果按文件名就判「官方已有」，
    就会把模组自己的贴图/网格当成官方内容剥掉 —— 模组直接失效。
    所以默认只信 full；只想同名就剥必须显式加 --match-name。

用法
    # 整个目录批量转换
    python tools/ronconvert.py "C:\\ron模组"

    # 先看诊断，不写文件
    python tools/ronconvert.py "C:\\ron模组" --dry-run

    # 从游戏本体 paks 生成全路径清单（几秒），再做转换
    python tools/ronconvert.py "C:\\ron模组" --game-paks "E:\\...\\ReadyOrNot\\Content\\Paks"

    # 转换并调用官方工具复核
    python tools/ronconvert.py "C:\\ron模组" --verify

    # 单个文件
    python tools/ronconvert.py "某模组.pak"

    # 激进模式：官方已有的路径一律丢弃，包最小但可能改坏贴图类 mod
    python tools/ronconvert.py "C:\\ron模组" --strip-all
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pakfmt as P

# 游戏安装的常见位置（找不到时会自动搜 Steam 库，见 find_game_paks）
DEFAULT_GAME_PAKS = (r"E:\SteamLibrary\steamapps\common\Ready Or Not"
                     r"\ReadyOrNot\Content\Paks")

# 游戏 pak 支持的版本；本作当前为 11（部分新版引擎写 12）
SUPPORTED_VERSIONS = (11, 12)
PAK_VERSION_LATEST = 12

UNREALPAK_CANDIDATES = [
    r"G:\UE_5.8\Engine\Binaries\Win64\UnrealPak.exe",
]

# ---- 冲突型资产的路径特征（保守白名单）----
# 命中这些子串且扩展名是 .uasset/.umap 的条目，若官方已有，则视为冲突并剥离。
CONFLICT_MARKERS = (
    "/blueprints/",
    "/blueprint/",
    "/logicmods/",
    "/logic/",
    "/datatables/",
    "/datatable/",
    "/data/",
    "/gamemodes/",
    "/gameplay/",
    "/characters/",
    "/weapons/",
    "/items/",
    "/ai/",
    "/widgets/",
    "/ui/",
    "/animations/",
    "/maps/",
)

# 明确【不剥离】的路径特征：资源替换类，覆盖官方是正常行为
KEEP_MARKERS = (
    "/textures/",
    "/materials/",
    "/material/",
    "/meshes/",
    "/staticmeshes/",
    "/skeletalmeshes/",
    "/audio/",
    "/sounds/",
    "/fonts/",
    "/particles/",
    "/decals/",
    "/fx/",
    "/vfx/",
    "/internationalization/",
)

# ---------------------------------------------------------------------------
# 资产分组：UE 里「一个资产」= 多个条目
#   <Stem>.uasset / <Stem>.umap   （包头，含导入导出表与 Name Table）
#   <Stem>.uexp                   （导出数据）
#   <Stem>.ubulk                  （大体积二进制载荷）
#   <Stem>.uptnl                  （可选载荷）
#   <Stem>.m.ubulk                （移动端变体）
# 模组里还常带 .bak 备份变体（如 X.uasset.bak），它们同样是该资产的一部分。
#
# ★ 只有把这【一整组】当一个整体处理，才不会留下孤儿。
#   之前的实现只把 .uasset/.umap 当组头，导致 X.uasset.bak 这类
#   变体不会被跟着剥掉，可能让引擎读到新旧混合的组合 → 崩溃/失效。
ASSET_EXT_CHAIN = (
    ".m.ubulk",
    ".uasset", ".umap", ".uexp", ".ubulk", ".uptnl",
    ".uasset.bak", ".umap.bak", ".uexp.bak", ".ubulk.bak", ".uptnl.bak",
)
# 兼容旧名字
PACKAGE_EXTS = (".uasset", ".umap")


def asset_stem(rel: str) -> str:
    """把一个条目路径还原成它所属【资产】的名字。

        X.uasset          -> X
        X.uexp            -> X
        X.uasset.bak      -> X
        X.m.ubulk         -> X
        A/B/X.ubulk.bak   -> A/B/X
    认不出来的（如 .ini/.bin/无扩展名）原样返回，单独成组。
    """
    low = rel.lower()
    for ext in ASSET_EXT_CHAIN:
        if low.endswith(ext):
            return rel[:len(rel) - len(ext)]
    return rel


def asset_members(elist) -> dict:
    """把条目按资产分组：{资产名: [条目, ...]}"""
    groups: dict[str, list] = {}
    for e in elist:
        groups.setdefault(asset_stem(e.rel), []).append(e)
    return groups



# ===========================================================================
# 官方资产清单
# ===========================================================================
class OfficialAssets:
    """官方已有路径的索引。

    ★ 匹配强度分三级（这是本工具最关键的安全机制）：

        full   路径完全一致 —— 可以确定模组覆盖的是同一个资产。可信。
        bare   路径不同但【文件名】相同 —— 只是「同名」。不可信！
        none   都没有。

    为什么必须区分：真实模组常把路径写成
        mount='../../../ReadyOrNot/Content/'  +  'ReadyOrNot/Character/Gore/.../T_X'
    路径里多带一层 ReadyOrNot，于是「和官方同路径」永远匹配不上，
    但文件名和官方某个资产一样。如果按文件名就判定「官方已有」，
    会把**模组自己的贴图/网格**当成官方内容剥掉 —— 模组直接失效。

    因此默认策略：**只信 full；bare 命中判为「存疑」，不剥。**
    需要更激进时用 --match-name 显式开启，并且只在有辅助证据时才剥。
    """

    def __init__(self, manifest_path: str | None = None):
        self.full: set[str] = set()
        self.bare: set[str] = set()
        self.bare_ambiguous: set[str] = set()   # 同名但出现在多个目录 → 更不可信
        self.sizes: dict[str, int] = {}         # 全路径 -> 官方条目的未压缩大小
        self.source = "(未加载)"
        self.generated_at: float | None = None
        self._name_count: dict[str, int] = {}   # 裸名出现次数，用来判歧义
        if manifest_path and os.path.isfile(manifest_path):
            self._load_manifest(manifest_path)

    @staticmethod
    def _normalize(rel: str) -> str:
        s = rel.replace("\\", "/").strip().lower()
        while s.startswith("../"):
            s = s[3:]
        s = posixpath.normpath("/" + s.lstrip("/")).lstrip("/")
        return s

    @classmethod
    def full_path_of(cls, mount: str, rel: str) -> str:
        """模组内的 (mount, rel) -> 官方命名空间下的全路径。"""
        m = mount.replace("\\", "/")
        combined = m.rstrip("/") + "/" + rel.lstrip("/") if m.strip("/") else rel
        return cls._normalize(combined)

    @classmethod
    def stem_variants(cls, mount: str, rel: str) -> list[str]:
        """模组条目可能对应的【官方全路径】候选。

        真实模组的路径常把挂载点里已有的那一两层又写了一遍，例如

            mount = '../../../ReadyOrNot/Content/'
            rel   = 'ReadyOrNot/Content/Blueprints/Items/BP_Gun.uasset'   <- 两层都重复
            rel   = 'ReadyOrNot/Character/Gore/T_X.uasset'                <- 只重复一层

        直接拼会得到 '.../readyornot/content/readyornot/content/...'，永远匹配不上。
        所以额外试「把开头重复的 readyornot/ 、content/ 各去掉一层」的版本，
        让 full 匹配有机会命中真正的官方路径。
        """
        m = mount.replace("\\", "/")
        r = rel.replace("\\", "/").lstrip("/")
        prefix = m.rstrip("/") + "/" if m.strip("/") else ""

        def join(rest: str) -> str:
            return cls._normalize(prefix + rest if prefix else rest)

        cands = [join(r)]
        rest = r
        for _ in range(2):          # 最多去掉两层（readyornot/ 和 content/）
            head, sep, tail = rest.partition("/")
            if not sep or head.lower() not in ("readyornot", "content"):
                break
            rest = tail
            c = join(rest)
            if c not in cands:
                cands.append(c)
        return cands

    def _load_manifest(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        names = data.get("names") or data.get("files") or []
        self.generated_at = data.get("generated_at")
        for n in names:
            s = self._normalize(n)
            if not s:
                continue
            if "/" in s:
                self.full.add(s)      # 只有真正带目录的才算「全路径」
            b = s.rsplit("/", 1)[-1]
            self.bare.add(b)
            c = self._name_count.get(b, 0) + 1
            self._name_count[b] = c
            if c > 1:
                self.bare_ambiguous.add(b)   # 同名出现在多处 → 不敢剥
        # 可选：与 names 一一对应的未压缩大小（老清单没有这一段）
        sizes = data.get("sizes")
        csizes = data.get("csizes")
        if isinstance(sizes, list) and len(sizes) == len(names):
            for i, (n, sz) in enumerate(zip(names, sizes)):
                s = self._normalize(n)
                if not s or not isinstance(sz, int) or sz < 0:
                    continue
                cs = -1
                if isinstance(csizes, list) and i < len(csizes) \
                        and isinstance(csizes[i], int):
                    cs = csizes[i]
                self.sizes[s] = (sz, cs)
        self.source = (f"{path}（{len(names)} 条，全路径 {len(self.full)} 条"
                       + (f"，含大小信息" if self.sizes else "") + "）")

    def add_paths(self, paths) -> None:
        """加入【官方命名空间下的全路径】（= 挂载点 + 挂载内相对路径）。

        ★ 一定要传全路径。只传裸文件名的话 full 索引是空的，
          默认策略下就永远匹配不上 → 工具等于什么都不做。
        """
        for n in paths:
            s = self._normalize(n)
            if not s:
                continue
            if "/" in s:
                self.full.add(s)
            b = s.rsplit("/", 1)[-1]
            self.bare.add(b)
            c = self._name_count.get(b, 0) + 1
            self._name_count[b] = c
            if c > 1:
                self.bare_ambiguous.add(b)

    def size_of(self, full_path: str) -> int | None:
        """官方条目在该路径上的【未压缩大小】；清单里没有就 None。"""
        v = self.sizes.get(self._normalize(full_path))
        return v[0] if v else None

    def entry_size(self, full_path: str) -> tuple[int, int] | None:
        """官方条目在该路径上的 (未压缩大小, 压缩后大小)；清单里没有就 None。

        压缩后大小可能是 -1（老清单没有这一项）—— 那就没法证明「照抄」。
        """
        return self.sizes.get(self._normalize(full_path))

    def has(self, full_path: str) -> tuple[bool, str]:
        """返回 (是否官方已有, 命中方式: full / bare / '')。"""
        s = self._normalize(full_path)
        if not s:
            return False, ""
        if s in self.full:
            return True, "full"
        if s.rsplit("/", 1)[-1] in self.bare:
            return True, "bare"
        return False, ""

    def __len__(self) -> int:
        """官方资产条目总数（用 bare 计，因为它是 full 的「去重后名字」）。"""
        return len(self.bare)

    @property
    def has_full_index(self) -> bool:
        """清单里是否有全路径。

        没有的话（旧的裸名清单）默认策略下永远匹配不上，工具会空转 ——
        必须提示用户重新生成清单，而不是静默「什么都不做」。
        """
        return bool(self.full)


def build_manifest_from_game_paks(paks_dir: str, out_json: str,
                                  verbose: bool = True, progress=None) -> int:
    """用自研解析器从游戏 paks 生成【全路径】清单。

    ★ 写出的必须是「挂载点 + 挂载内路径」的**全路径**（如
      readyornot/content/blueprints/items/bp_gun.uasset）。
      只写裸文件名会让 full 索引为空 —— 默认的保守策略（只信路径一致）
      就永远匹配不上，工具会变成什么都不做的空转。

    ★ 只统计游戏本体 pak（pakchunk<N>-<Platform>.pak），跳过玩家装的模组。
      否则模组内容会被写进「官方清单」，之后判定就会认为「官方已有」→ 全被剥掉。

    ★ 用 read_pak_index 只读索引：本体 pakchunk0 有 24 GB，
      全量读入既慢又可能爆内存，而我们只需要挂载点 + 路径。

    progress: 可选的 callable(stage:str, done:int, total:int, note:str)，
              用于把进度报给 GUI。
    """
    def rep(stage, done, total, note=""):
        if progress:
            try:
                progress(stage, done, total, note)
            except Exception:
                pass

    try:
        all_paks = [f for f in os.listdir(paks_dir) if f.lower().endswith(".pak")]
    except Exception as ex:
        _emit_default(verbose, f"   ✘ 读不了目录 {paks_dir}：{ex}")
        return 0
    files = sorted(f for f in all_paks if is_official_pak(f))
    skipped = [f for f in all_paks if not is_official_pak(f)]
    if not files:
        _emit_default(verbose, f"   ✘ {paks_dir} 里没有游戏本体 pak")
        return 0
    if skipped and verbose:
        _emit_default(verbose,
                      f"   （跳过 {len(skipped)} 个非本体 pak / 模组："
                      f"{', '.join(skipped[:4])}{' ...' if len(skipped) > 4 else ''}）")

    official = OfficialAssets()
    t0 = time.time()
    for i, name in enumerate(files, 1):
        path = os.path.join(paks_dir, name)
        size_mb = 0
        try:
            size_mb = os.path.getsize(path) // (1024 * 1024)
        except Exception:
            pass
        if verbose:
            _emit_default(verbose, f"   [{i}/{len(files)}] {name} ...", end="",
                          flush=True)
        rep("pak", i - 1, len(files), f"{name}（{size_mb} MB）")
        try:
            pk = P.read_pak_index(path)
            mount = pk.mount_point
            pairs = [(OfficialAssets.full_path_of(mount, rel), unc, cmp_)
                     for rel, unc, cmp_ in pk.all_paths_with_sizes()]
            official.add_paths(p for p, _u, _c in pairs)
            for p, unc, cmp_ in pairs:
                if unc >= 0:
                    official.sizes[p] = (unc, cmp_)
            del pk
            if verbose:
                _emit_default(verbose, f" 累计 {len(official)}")
        except Exception as ex:
            if verbose:
                _emit_default(verbose,
                              f" 跳过（{type(ex).__name__}: {ex}）")
        rep("pak", i, len(files), name)
    write_manifest(out_json, official.full, {
        "source_dir": paks_dir,
        "pak_count": len(files),
        "skipped_paks": skipped,
        "generated_by": "ronconvert.build_manifest_from_game_paks",
    }, sizes=official.sizes)
    if verbose:
        _emit_default(
            verbose,
            f"   已写出 {out_json}：全路径 {len(official.full)} 条，"
            f"耗时 {time.time()-t0:.1f}s")
    return len(official)


def _emit_default(verbose: bool, msg: str, **kw) -> None:
    if verbose:
        print(msg, **kw)


def write_manifest(path: str, names, extra: dict | None = None,
                   sizes: dict | None = None) -> None:
    """写清单 JSON，并记下生成时间（用于判断清单是否过期）。

    sizes: 可选的 {全路径: (未压缩大小, 压缩后大小)}。有它才能分辨
           「照抄官方」和「模组自己改过」—— 没有的话为了安全一条都不会剥。
    """
    ordered = sorted(set(names))
    data = {
        "generated_at": time.time(),
        "generated_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(ordered),
        "names": ordered,
    }
    if sizes:
        # 与 names 一一对应的两个数组：比 {路径: 大小} 的 JSON 小得多
        # （47 万条时各约 4 MB vs 15 MB），加载也快。
        #   sizes  = 未压缩大小
        #   csizes = 压缩后大小（交叉验证用；Oodle 对相同输入是确定性的，
        #            两个都相同才敢断定是「照抄官方」）
        data["sizes"] = [int(sizes.get(n, (-1, -1))[0]) for n in ordered]
        data["csizes"] = [int(sizes.get(n, (-1, -1))[1]) for n in ordered]
    if extra:
        data.update(extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ===========================================================================
# 自动找游戏 / 判断清单是否过期
# ===========================================================================
REL_PAKS = os.path.join("ReadyOrNot", "Content", "Paks")


def _steam_roots() -> list[str]:
    """收集可能的 Steam 安装根目录。"""
    roots: list[str] = []
    # 1) 注册表
    try:
        import winreg
        for hive, key, val in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam",
             "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    p, _ = winreg.QueryValueEx(k, val)
                    if p and os.path.isdir(p):
                        roots.append(p)
            except OSError:
                pass
    except Exception:
        pass
    # 2) Steam 的库配置，能发现装在别的盘的情况（比如 E:\SteamLibrary）
    for r in list(roots):
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r'"path"\s*"([^"]+)"', f.read()):
                    p = m.group(1).replace("\\\\", "\\")
                    if os.path.isdir(p):
                        roots.append(p)
        except Exception:
            pass
    # 3) 常见位置兜底
    for drv in ("C:", "D:", "E:", "F:", "G:"):
        for sub in (r"SteamLibrary", r"Steam", r"Games\SteamLibrary",
                    r"Program Files (x86)\Steam"):
            p = os.path.join(drv + os.sep, sub)
            if os.path.isdir(p):
                roots.append(p)
    # 去重且保序
    seen = set()
    out = []
    for r in roots:
        k = os.path.normcase(os.path.abspath(r))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def find_game_paks(extra_hints: list[str] | None = None) -> str | None:
    """自动找 Ready or Not 的 Content\\Paks 目录。

    找不到返回 None。只做「存在且有 .pak」的判断，不做全量扫描（很快）。
    """
    cands: list[str] = []
    if extra_hints:
        cands.extend(extra_hints)
    cands.append(DEFAULT_GAME_PAKS)
    for root in _steam_roots():
        cands.append(os.path.join(root, "steamapps", "common", "Ready Or Not",
                                  REL_PAKS))
    for c in cands:
        try:
            if os.path.isdir(c) and any(
                    f.lower().endswith(".pak") for f in os.listdir(c)):
                return os.path.normpath(c)
        except Exception:
            continue
    return None


# 游戏本体 pak 的命名：pakchunk<N>-<Platform>.pak
# 玩家模组的命名特征（在真实机器上核对过）：
#   pakchunk9999-Mods_<名字>_P.pak     <- 官方本体从来没有 9999 号 chunk
#   <任意名>_P.pak                     <- _P 后缀是 UE 的 patch pak 约定
_OFFICIAL_PAK_RE = re.compile(r"^pakchunk\d+-\w+\.pak$", re.IGNORECASE)


def is_official_pak(name: str) -> bool:
    """是否游戏本体的 pak（不是玩家装的模组）。

    排除规则（任一命中即为模组）：
      · 名字里有 "_P.pak"（UE patch pak 后缀）
      · 名字里有 "-Mods"（N 网/mod.io 模组常见命名）
      · chunk 号 >= 9000（官方本体只用 0..24；模组普遍用 9999）
    判错的后果：模组会被当成官方内容写进清单 → 之后判定认为「官方已有」→ 全被剥掉。
    """
    if not _OFFICIAL_PAK_RE.match(name):
        return False
    low = name.lower()
    if "_p.pak" in low or "-mods" in low:
        return False
    m = re.match(r"^pakchunk(\d+)-", name, re.IGNORECASE)
    if m and int(m.group(1)) >= 9000:
        return False
    return True


def game_paks_newest_mtime(paks_dir: str) -> float:
    """游戏【本体】paks 里最新的修改时间（游戏更新会让它变新）。

    只看 pakchunk<N>-<Platform>.pak，忽略模组与 .sig。
    """
    newest = 0.0
    try:
        with os.scandir(paks_dir) as it:
            for e in it:
                if e.is_file() and is_official_pak(e.name):
                    newest = max(newest, e.stat().st_mtime)
    except Exception:
        pass
    return newest


def check_manifest_freshness(manifest_path: str | None,
                             game_paks: str | None) -> dict:
    """判断清单是否可能过期。

    返回 dict:
        status: "ok" | "stale" | "unknown" | "no_game" | "missing"
        reason: 给用户看的一句话
        age_days / game_days: 便于显示
    """
    if not manifest_path or not os.path.isfile(manifest_path):
        return {"status": "missing", "reason": "没有清单文件"}
    if not game_paks or not os.path.isdir(game_paks):
        return {"status": "no_game", "reason": "没找到游戏安装目录"}

    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as ex:
        return {"status": "unknown", "reason": f"清单读不出来：{ex}"}

    gen = data.get("generated_at")
    if not gen:      # 旧格式清单没有时间戳，只能按文件时间估
        try:
            gen = os.path.getmtime(manifest_path)
        except Exception:
            gen = None
    game = game_paks_newest_mtime(game_paks)

    if not gen or not game:
        return {"status": "unknown", "reason": "缺少时间信息"}

    age_days = (time.time() - gen) / 86400
    game_days = (time.time() - game) / 86400
    if game > gen + 60:            # 游戏 paks 比清单新（留 60 秒余量）
        return {"status": "stale", "age_days": age_days, "game_days": game_days,
                "reason": f"游戏在清单生成之后更新过（清单 {age_days:.0f} 天前，"
                          f"游戏 {game_days:.0f} 天前）"}
    return {"status": "ok", "age_days": age_days, "game_days": game_days,
            "reason": f"清单 {age_days:.0f} 天前生成，比游戏新"}


# ===========================================================================
# 诊断 + 转换
# ===========================================================================
@dataclass
class Entry:
    rel: str                      # 挂载点内相对路径（正斜杠）
    path: P.PakEntry
    full: str = ""                # 官方命名空间全路径
    official: bool = False
    official_by: str = ""
    conflict: bool = False        # 是否「冲突型」资产
    reason: str = ""


@dataclass
class Diagnosis:
    src: str = ""
    ok: bool = False
    error: str = ""
    version: int = 0
    mount: str = ""
    methods: list = field(default_factory=list)
    total_entries: int = 0
    recovered: int = 0
    official_entries: int = 0
    conflict_entries: int = 0
    conflict_paths: list = field(default_factory=list)
    kept: int = 0
    dropped: int = 0
    problems: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    out_path: str = ""
    out_bytes: int = 0
    verify: dict = field(default_factory=dict)
    seconds: float = 0.0
    copied: bool = False          # 无需改动，原样复制
    matched_full: int = 0         # 路径完全一致（可信）
    matched_bare: int = 0         # 仅文件名相同（存疑，默认不剥）
    size_mismatch: list = field(default_factory=list)
    # ↑ [(rel, 模组大小, 官方大小), ...]：会被剥、但内容和官方【不一样】
    #   （只在 --strip-modified 模式下才会有内容）
    kept_modified: list = field(default_factory=list)
    # ↑ [(资产名, rel, 模组大小, 官方大小), ...]：
    #   官方已有但【模组改过】→ 判定为模组功能，已保留没剥
    n_size_evidence: int = 0
    # ↑ 因为「清单里没有大小信息」而无法判断、只好保守保留的资产数
    # 内部工作数据（不参与序列化）
    _elist: list = field(default_factory=list, repr=False)
    _drop: set = field(default_factory=set, repr=False)
    _pak: object = field(default=None, repr=False)

    @property
    def verdict(self) -> str:
        if not self.ok:
            return "无法处理"
        if self.kept == 0:
            return "已被官方完全取代"
        if self.dropped:
            return "已转换（剥离冲突）"
        # 没有可剥离的内容：文件会原样保留（dry-run 时还没复制，也算"本来就可用"）
        return "本来就可用"

    @property
    def action(self) -> str:
        if not self.ok:
            return "跳过（请人工检查）"
        if self.kept == 0:
            return "删除原模组（内容已全部被官方取代）"
        if self.dropped:
            return f"用转换后的文件替换原模组（少了 {self.dropped} 个冲突条目）"
        return "保持使用即可（无需改动）"


def _is_conflict_type(rel: str, ext: str) -> bool:
    """判断某个资产是否属于「会与官方新版冲突」的类型。"""
    if ext not in PACKAGE_EXTS:
        return False
    low = "/" + rel.replace("\\", "/").lower().lstrip("/")
    if any(k in low for k in KEEP_MARKERS):
        return False
    return any(k in low for k in CONFLICT_MARKERS)


def read_mod(path: str) -> tuple[P.PakFile, dict[str, P.PakEntry]]:
    """读出 pak 和它的 {相对路径: 条目}。

    ★ 不要在这里直接要 FDI/PHI：老格式（v1..v9）没有二级索引，
      路径本来就在主索引里。paths_with_entries() 两种格式都认。
    """
    pk = P.read_pak(path)
    entries = pk.paths_with_entries()
    if not entries:
        raise P.PakError("pak 里没有目录索引，无法恢复路径")
    return pk, entries


def diagnose(src: str, official: OfficialAssets, *, strip_all: bool = False,
             match_name: bool = False, strip_modified: bool = False,
             verbose: bool = True, log=None) -> Diagnosis:
    """诊断一个模组。

    match_name=False（默认，安全）：
        只剥「路径也一致」的资产。仅文件名相同的一律视为【存疑】，不剥。
        真实模组常把路径写成 mount + 'ReadyOrNot/Content/...'（多一层），
        按文件名判定会把模组自己的贴图/网格当成官方内容剥掉 → 模组失效。

    strip_modified=False（默认，安全）：
        路径一致、但内容被模组改过的（未压缩大小和官方不同）【不剥】。
        这类资产就是模组的功能本身 —— 剥掉的话游戏能进，但什么都不发生。
        只有在「游戏一进就崩」时才考虑开 True 连它们一起剥。

    strip_all=True（激进）：
        官方已有的一律剥，不看冲突类型。
    """
    emit = log or (print if verbose else (lambda *_a, **_k: None))
    d = Diagnosis(src=src)
    t0 = time.time()
    emit("")
    emit(f"── {os.path.basename(src)}")
    try:
        pk, entries = read_mod(src)
    except Exception as ex:
        d.error = f"{type(ex).__name__}: {ex}"
        d.problems.append("不是合法的 UE pak，或索引结构不支持")
        emit(f"   ✘ 无法读取：{d.error}")
        d.seconds = time.time() - t0
        return d

    d.ok = True
    d.version = pk.version
    d.mount = pk.mount_point
    d.methods = [m for m in pk.compression_methods if m]
    # 老格式没有 encoded 条目表，条目数就是路径数
    d.total_entries = (len(pk.encoded_entries) if pk.encoded_entries
                       else len(entries))
    d.recovered = len(entries)

    if pk.version not in SUPPORTED_VERSIONS:
        # 读了能读（v1 起都认），只是我们【写】的是游戏本体在用的那个版本。
        # 老模组（社区里那些很久没更新的）多半就是这样，别当错误吓人。
        d.actions.append(
            f"pak 是老格式 v{pk.version}（本工具能读能诊断；"
            f"要重新打包会写成 v{SUPPORTED_VERSIONS[0]}，游戏两种都读）")

    # ---- 逐个条目判定 ----
    elist: list[Entry] = []
    for rel, pe in sorted(entries.items()):
        ext = os.path.splitext(rel)[1].lower()
        # 主路径 + 「去掉重复前缀」的候选，让 full 匹配有机会命中
        cands = OfficialAssets.stem_variants(pk.mount_point, rel)
        full = cands[0]
        has, how = False, ""
        for c in cands:
            h, w = official.has(c)
            if h:
                has, how = h, w
                if w == "full":
                    full = c
                    break
        conflict = _is_conflict_type(rel, ext)
        elist.append(Entry(rel=rel, path=pe, full=full, official=has,
                           official_by=how, conflict=conflict))
    by_rel = {e.rel: e for e in elist}

    # 统计匹配强度，便于报告
    d.matched_full = sum(1 for e in elist if e.official_by == "full")
    d.matched_bare = sum(1 for e in elist if e.official_by == "bare")

    # ---- 决定丢弃集合 ----
    # ★ 两条铁律：
    #   1) 按【资产】整组处理（一个资产 = .uasset + .uexp + .ubulk + .bak 变体），
    #      否则会留孤儿，引擎读到新旧混合 → 崩溃。
    #   2) 只信【路径一致】（full）的证据。只有文件名相同（bare）不算数 ——
    #      真实模组路径常多一层 ReadyOrNot/，按文件名判会把模组自己的
    #      贴图/网格当成官方内容剥掉，模组直接失效。
    groups = asset_members(elist)

    def evidence_ok(e) -> bool:
        if e.official_by == "full":
            return True
        if not match_name:
            return False
        # 激进模式下的额外证据要求
        bare = e.rel.rsplit("/", 1)[-1].lower()
        if bare in official.bare_ambiguous:
            return False          # 同名出现在多个目录 → 不敢剥
        return e.conflict         # 只对冲突型（蓝图/逻辑）放宽

    drop: set[str] = set()
    for e in elist:
        if not evidence_ok(e):
            continue
        ext = os.path.splitext(e.rel)[1].lower()
        if strip_all:
            if ext in (".uasset", ".umap", ".uexp", ".ubulk",
                       ".uptnl", ".ini", ".bin"):
                drop.add(e.rel)
        elif e.conflict and ext in PACKAGE_EXTS:
            drop.add(e.rel)

    # ---- 整组决策：内容被模组改过的，【不剥】------------------------------
    # ★ 这是「装了跟没装一样」的根因。
    #
    #   「官方已有 + 冲突型」有两种完全不同的情况，只看路径分不出来：
    #
    #     a) 模组只是【照抄】了官方资产（作者打包时顺手带上的依赖）
    #        -> 剥掉毫无损失，而且能解决旧蓝图导致的崩溃
    #     b) 模组【改过】这个资产（比如血腥 mod 改 Blood_Standard 数据表、
    #        改 BP_RoNBloodPool 让它生成自己的贴花）
    #        -> 剥掉 = 把模组的功能删了。游戏能进，但什么都不发生
    #
    #   区分办法：比对 (未压缩大小, 压缩后大小)。
    #     · 两个都相同 -> 几乎一定是照抄官方（Oodle 对相同输入是确定性的）
    #     · 任一不同   -> 内容不一样 -> 当作「模组改过」-> 【不剥】
    #   实测 BP_RoNBloodPool 未压缩大小和官方一样，但压缩后 2,260 vs 2,198；
    #   只看未压缩大小会误判成「照抄」，把它剥掉 VisceralBlud 就废了。
    #
    #   拿不到大小信息（老清单）时不敢赌，同样不剥，并提示重新生成清单。
    #   真要连改过的一起剥（比如游戏一进就崩），加 --strip-modified。
    def asset_modified(members) -> bool:
        """能不能确认「模组改过这个资产」？确认得了才返回 True。

        只有「路径一致 + 官方大小」才够格下结论：
          · 大小对不上        -> 内容确实不同 -> True（模组改过）
          · 大小完全一致      -> 照抄官方     -> False
          · 清单没带大小信息  -> 不敢赌       -> True（保守：保留）
        一个 full 命中都没有（比如 --match-name 的同名命中）-> 无从判断 ->
        False，交给 evidence_ok 那套规则去管。
        """
        for m in members:
            if m.official_by != "full":
                continue
            sz = official.entry_size(m.full)
            if sz is None or sz[1] < 0:
                return True                  # 清单缺大小 -> 保守
            if (m.path.uncompressed_size, m.path.size) != sz:
                return True                  # 内容不一样
        return False

    d.kept_modified = []
    d.n_size_evidence = 0
    for stem, members in sorted(groups.items()):
        if not any(m.rel in drop for m in members):
            continue
        if not strip_modified and asset_modified(members):
            # 模组自己改过的 -> 整组保留，一条都不剥
            for m in members:
                drop.discard(m.rel)
            if not official.sizes and any(m.official_by == "full"
                                          for m in members):
                d.n_size_evidence += 1
            worst = None
            for m in members:
                if m.official_by != "full":
                    continue
                sz = official.entry_size(m.full)
                if sz is None or \
                        (m.path.uncompressed_size, m.path.size) == sz:
                    continue
                if worst is None or abs(m.path.uncompressed_size - sz[0]) > \
                        abs(worst[1] - worst[2]):
                    worst = (m.rel, m.path.uncompressed_size, sz[0])
            if worst:
                d.kept_modified.append((stem,) + worst)
            continue
        # 整组剥离：某个资产只要有成员被剥，它的全部成员一起剥
        for m in members:
            if m.rel.lower().endswith(ASSET_EXT_CHAIN):
                drop.add(m.rel)

    d.conflict_entries = sum(1 for e in elist if e.conflict)
    d.official_entries = sum(1 for e in elist if e.official)
    d.conflict_paths = [e.rel for e in elist if e.conflict]
    d.dropped = len(drop)
    d.kept = d.total_entries - d.dropped

    # 没被剥、但内容确实和官方不一样的（正常情况下就是上面保下来的那些）
    d.size_mismatch = []
    for rel in sorted(drop):
        e = by_rel.get(rel)
        if e is None or e.official_by != "full":
            continue
        osz = official.size_of(e.full)
        if osz is None:
            continue
        if e.path.uncompressed_size != osz:
            d.size_mismatch.append((rel, e.path.uncompressed_size, osz))

    emit(f"   版本 {d.version}   挂载点 {d.mount!r}   方法 {d.methods}")
    emit(f"   条目 {d.total_entries}（恢复路径 {d.recovered}）")
    emit(f"   官方匹配：路径一致 {d.matched_full} 条（可信）"
         f"  仅同名 {d.matched_bare} 条（存疑，默认不剥）")
    for p in d.problems:
        emit(f"   ⚠ {p}")
    if d.n_size_evidence:
        d.problems.append(
            f"官方清单里没有大小信息，{d.n_size_evidence} 个资产无法确认"
            f"是不是「照抄官方」—— 已按【模组改过】处理，全部保留。"
            f"请重新生成清单（界面勾「先生成官方资产清单」，几秒）")
        emit(f"   ⚠ {d.problems[-1]}")
    if d.kept_modified:
        emit(f"   ⚠ {len(d.kept_modified)} 个资产官方已有，但【内容被模组改过】"
             f"—— 这是模组的功能本身，已【保留】（没剥）：")
        for _stem, rel, msz, osz in d.kept_modified[:5]:
            emit(f"        {rel}   模组 {msz:,}B / 官方 {osz:,}B")
        if len(d.kept_modified) > 5:
            emit(f"        ... 其余 {len(d.kept_modified)-5} 个")
        emit("        （想连这些一起剥：命令行加 --strip-modified，"
             "界面勾「连改过的也剥」）")
    if d.size_mismatch:
        emit(f"   ⚠ 另有 {len(d.size_mismatch)} 个被剥条目内容和官方不同"
             f"（--strip-modified 模式）：")
        for rel, msz, osz in d.size_mismatch[:5]:
            emit(f"        {rel}   模组 {msz:,}B / 官方 {osz:,}B")

    if d.dropped == 0:
        d.actions.append("无可剥离内容（内容都该保留）")
    else:
        d.actions.append(f"剥离 {d.dropped} 个冲突条目，保留 {d.kept} 个")
    if d.kept_modified:
        d.actions.append(
            f"保留了 {len(d.kept_modified)} 个「官方已有但模组改过」的资产"
            f"（那是模组的功能，剥了就会'能进游戏但什么都不发生'）")
    if d.size_mismatch:
        d.actions.append(
            f"提示：{len(d.size_mismatch)} 个被剥条目的内容和官方不一样"
            f"（可能是模组自己改过的），已照剥 —— 进游戏留意一下相关表现")
    if d.kept == 0:
        d.problems.append("剥离后无剩余内容：该模组的冲突部分已被官方完全取代")

    d._elist = elist          # 供 convert 使用
    d._drop = drop
    d._pak = pk
    d.seconds = time.time() - t0
    return d


def convert(d: Diagnosis, outdir: str, *, verify: bool = False,
            target_version: int | None = None, raw: bool = False,
            log=None) -> Diagnosis:
    """把诊断结果落成文件。

    raw=True：把所有能解开的条目改写成【不压缩】。
        用于模组用了游戏没编进去的压缩方式（实测 Hospital 地图模组用 Zlib，
        而本体全是 Oodle）—— 解不开的资产会让游戏卡在加载页面。
        代价是包会变大。解不开的（Oodle）原样搬运。
    """
    emit = log or (lambda *_a, **_k: None)
    if not d.ok:
        return d
    os.makedirs(outdir, exist_ok=True)
    name = os.path.basename(d.src)
    out = os.path.join(outdir, name)
    pk: P.PakFile = d._pak
    elist: list[Entry] = d._elist
    drop: set[str] = d._drop
    # ★ 老格式（v1..v9）的源 pak 不能「沿用源版本号」：我们的写入器只会写
    #   v11/v12 的索引，标成 v3 就成了四不像（实测 UnrealPak -Test 直接 rc=1）。
    #   统一写成游戏本体在用的那个版本（实测本体就是 v11）。
    if target_version:
        tv = target_version
    elif d.version in SUPPORTED_VERSIONS:
        tv = d.version
    else:
        tv = SUPPORTED_VERSIONS[0]
        emit(f"   源 pak 是老格式 v{d.version}：产物按 v{tv}（游戏本体的格式）写。")

    if d.kept == 0:
        # ★ 一条都不剩时【不要写空包】。
        #   空 pak 挂上去只会让游戏加载一个什么都没有的模组（之前的 wound
        #   模组就是这么变成 396 字节废包的）。正确做法是明确告诉用户：
        #   这个模组的内容已经被官方取代，可以直接删掉。
        emit("   剥离后无剩余内容，不生成新 pak。")
        d.actions.append("不生成新 pak（内容已被官方完全取代，原模组可直接删除）")
        d.copied = False
        return d

    # ★ 路径没恢复全就绝不能重新打包。
    #   我们只能写回「看得见」的条目，看不见的那些会被静默丢掉 ——
    #   包看起来正常、自检也过，但模组其实被削掉了一大半。
    #   这种情况原样复制，并明确报告。
    if d.recovered < d.total_entries and d._drop:
        emit(f"   ✘ 只恢复了 {d.recovered}/{d.total_entries} 条路径，"
             f"重新打包会丢掉其余 {(d.total_entries - d.recovered)} 条。")
        emit("      已改为【原样复制】，不做任何修改。请把这个 pak 反馈给作者/工具方。")
        d.problems.append(
            f"目录索引里有 {d.total_entries - d.recovered} 条路径没解析出来，"
            f"为避免丢文件，本次不修改、原样复制")
        d.actions.append("原样复制（路径没解析全，不敢重新打包）")
        shutil.copy2(d.src, out)
        d.out_path = out
        d.out_bytes = os.path.getsize(out)
        d.copied = True
        return d

    if d.dropped == 0 and tv == d.version and not raw:
        # 完全不需要改：直接复制，保证字节一致、零风险
        emit("   正在原样复制（无需改动）...")
        shutil.copy2(d.src, out)
        d.out_path = out
        d.out_bytes = os.path.getsize(out)
        d.copied = True
        d.actions.append("无需修改，已原样复制")
    else:
        if raw:
            emit(f"   正在重新打包并改成【不压缩】（保留 {d.kept} 条 / "
                 f"剥离 {d.dropped} 条）...")
        else:
            emit(f"   正在重新打包（保留 {d.kept} 条 / 剥离 {d.dropped} 条）...")
        methods = list(pk.compression_methods)
        w = P.PakWriter(d.mount, methods=methods, version=tv)
        n_raw = n_kept_compressed = 0
        for e in elist:
            if e.rel in drop:
                continue
            pe = e.path
            if raw and pe.method_index != 0 and pe.size != pe.uncompressed_size:
                mname = (methods[pe.method_index - 1]
                         if 0 < pe.method_index <= len(methods) else "")
                try:
                    blob = P.decompress_payload(
                        mname, pk.payload_of(pe), pe._block_lengths,
                        pe.uncompressed_size)
                except Exception:
                    # 解不开（Oodle）-> 原样搬运
                    n_kept_compressed += 1
                    blob = None
                if blob is not None:
                    n_raw += 1
                    w.add(e.rel, P.PakEntry(
                        size=len(blob), uncompressed_size=len(blob),
                        method_index=0, flags=pe.flags,
                        compression_block_size=0, sha1=pe.sha1,
                    ), blob)
                    continue
            w.add(e.rel, P.PakEntry(
                size=pe.size, uncompressed_size=pe.uncompressed_size,
                method_index=pe.method_index, flags=pe.flags,
                compression_block_size=pe.compression_block_size,
                sha1=pe.sha1, _block_lengths=list(pe._block_lengths),
            ), pk.payload_of(pe))
        info = w.build(out, pak_name_for_seed=name)
        d.out_path = out
        d.out_bytes = info["total_bytes"]
        if tv != d.version:
            d.actions.append(f"版本 {d.version} -> {tv}")
        if raw:
            d.actions.append(
                f"改成不压缩：{n_raw} 条已解开重写"
                + (f"，{n_kept_compressed} 条解不开（Oodle）仍保持原压缩"
                   if n_kept_compressed else ""))
        d.actions.append(f"写出 {os.path.basename(out)}（{info['entries']} 条）")

    # 自研读回自检
    emit("   自检：读回生成的 pak ...")
    self_ok = True
    try:
        chk = P.read_pak(out)
        got = sorted(chk.all_paths())
        want = sorted(e.rel for e in elist if e.rel not in drop)
        if got != want:
            self_ok = False
            d.problems.append(f"自检失败：写回路径 {len(got)} 条，期望 {len(want)} 条")
    except Exception as ex:
        self_ok = False
        d.problems.append(f"自检读取失败：{type(ex).__name__}: {ex}")

    if verify:
        emit("   校验：调用官方 UnrealPak -List / -Test（大模组可能要等一会）...")
        d.verify = unrealpak_check(out)

    # ★ 自检/官方复核没过 -> 把产物删掉，绝不把「看着像模组、其实坏的」pak
    #   留在输出目录里让人装进游戏。宁可什么都不给，也不能给一个坏的。
    bad_verify = isinstance(d.verify, dict) and d.verify.get("ok") is False
    if not self_ok or bad_verify:
        why = ("官方 UnrealPak -Test 没过" if bad_verify else "读回自检没过")
        try:
            os.remove(out)
            emit(f"   ✘ {why}：产物已删除，不给你一个可能装坏的 pak。")
        except OSError:
            emit(f"   ✘ {why}：产物删不掉，请手动删除 {out}")
        d.out_path = ""
        d.out_bytes = 0
        d.copied = False
        d.actions.append(f"产物未通过{why}，已丢弃（原模组保持不变，可继续用）")
        d.problems.append(f"产物未通过{why} —— 已丢弃，没有生成任何文件")
    return d


# ===========================================================================
# 官方工具复核
# ===========================================================================
def find_unrealpak() -> str | None:
    for c in UNREALPAK_CANDIDATES:
        if os.path.isfile(c):
            return c
    return None


def unrealpak_check(pak: str) -> dict:
    """调用官方 UnrealPak -List / -Test。非 ASCII 路径先搬到 ASCII 临时目录。"""
    exe = find_unrealpak()
    if not exe:
        return {"ok": None, "reason": "未找到 UnrealPak.exe"}

    tmp = None
    target = os.path.abspath(pak)
    if not all(ord(c) < 128 for c in target):
        base = r"C:\ronwork" if os.path.isdir(r"C:\ronwork") else None
        tmp = os.path.join(base or os.environ.get("TEMP", "."), "ronconvert_check")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        target = os.path.join(tmp, "check.pak")
        shutil.copy2(pak, target)

    def run(*a):
        p = subprocess.run([exe, target] + list(a), capture_output=True, timeout=1800)
        return p.returncode, ((p.stdout or b"").decode("utf-8", "replace") +
                              (p.stderr or b"").decode("utf-8", "replace"))

    rc_l, out_l = run("-List")
    rc_t, _ = run("-Test")
    import re
    listed = len(re.findall(r'Display: "(.+?)" offset: \d+, size: \d+ bytes', out_l))
    mm = re.search(r'mount point "(.+?)"', out_l)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": rc_l == 0 and rc_t == 0, "list_rc": rc_l, "test_rc": rc_t,
            "listed": listed, "mount": mm.group(1) if mm else None}


# ===========================================================================
# 扫描 / 主流程
# ===========================================================================
def find_paks(target: str) -> list[str]:
    if os.path.isfile(target):
        return [target] if target.lower().endswith(".pak") else []
    out = []
    for base, _dirs, files in os.walk(target):
        # 跳过我们自己的输出目录，避免重复处理
        if os.path.basename(base).lower() in ("converted", "转换后"):
            continue
        for f in files:
            if f.lower().endswith(".pak"):
                out.append(os.path.join(base, f))
    return sorted(out)


def main() -> int:
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(
        description="旧模组自动检测 + 转换为可用模组",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法")[-1])
    ap.add_argument("target", help="模组目录，或单个 .pak")
    ap.add_argument("-o", "--outdir", default=None,
                    help="输出目录（默认 <目标>/converted）")
    ap.add_argument("--manifest", default="game_manifest.json",
                    help="官方资产清单（默认 game_manifest.json）")
    ap.add_argument("--game-paks", default=None,
                    help="直接从游戏 Paks 目录读官方清单（最准，几秒即可）")
    ap.add_argument("--strip-all", action="store_true",
                    help="激进模式：官方已有的路径一律剥离（可能改坏贴图类 mod）")
    ap.add_argument("--match-name", action="store_true",
                    help="激进模式：文件名相同也算「官方已有」（默认关闭，"
                         "因为同名不等于同路径，会把模组自己的贴图剥掉）")
    ap.add_argument("--strip-modified", action="store_true",
                    help="连「模组自己改过」的资产也剥掉（默认保留）。"
                         "只在游戏一进就崩、需要清掉旧蓝图时才用 —— "
                         "开了之后模组很可能变成'能进游戏但什么都不发生'")
    ap.add_argument("--repack-raw", action="store_true",
                    help="把条目改成【不压缩】重新打包。用于模组用了游戏没编进去的"
                         "压缩方式（比如 Zlib 而本体是 Oodle），会导致卡加载。"
                         "代价是包变大")
    ap.add_argument("--target-version", type=int, default=None,
                    help=f"输出 pak 版本（默认跟随源；当前游戏为 {PAK_VERSION_LATEST}）")
    ap.add_argument("--verify", action="store_true",
                    help="用官方 UnrealPak -List/-Test 复核转换结果")
    ap.add_argument("--dry-run", action="store_true", help="只诊断，不写文件")
    ap.add_argument("--json", default=None, help="把报告写入 JSON")
    args = ap.parse_args()

    target = args.target
    if not os.path.exists(target):
        print(f"路径不存在：{target}")
        return 1

    # ---- 官方清单 ----
    official = OfficialAssets(args.manifest)
    if args.game_paks:
        print(f"从游戏 paks 生成全路径清单：{args.game_paks}")
        cache = "game_manifest_full.json"
        build_manifest_from_game_paks(args.game_paks, cache)
        official = OfficialAssets(cache)
    print(f"官方资产清单：{official.source}")
    if len(official) == 0:
        print("⚠ 清单为空，无法判断「官方已有」。请先运行：")
        print("   加 --game-paks <游戏Paks目录> 自动生成全路径清单")
    elif not official.has_full_index:
        print("⚠ 这份清单【只有裸文件名】，没有全路径 ——")
        print("   默认策略只信「路径完全一致」，此时会一条都不剥（工具空转）。")
        print("   请用 --game-paks 重新生成，或改加 --match-name 走激进模式。")

    paks = find_paks(target)
    if not paks:
        print(f"在 {target} 下没有找到 .pak")
        return 1

    outdir = args.outdir or os.path.join(
        target if os.path.isdir(target) else os.path.dirname(target) or ".",
        "converted")
    print(f"待处理 {len(paks)} 个 pak")
    print(f"输出目录：{outdir}")
    print("=" * 78)

    results: list[Diagnosis] = []
    for i, src in enumerate(paks, 1):
        print(f"\n[{i}/{len(paks)}]", end=" ")
        d = diagnose(src, official, strip_all=args.strip_all,
                     match_name=args.match_name,
                     strip_modified=args.strip_modified)
        if not args.dry_run and d.ok:
            convert(d, outdir, verify=args.verify,
                    target_version=args.target_version,
                    raw=args.repack_raw)
        results.append(d)
        # 打印结论
        print(f"   ── 结论：{d.verdict}")
        for a in d.actions:
            print(f"      · {a}")
        for p in d.problems:
            print(f"      ⚠ {p}")
        if d.dropped and d.conflict_paths:
            show = [p for p in d.conflict_paths if p in getattr(d, "_drop", set())]
            for p in show[:5]:
                print(f"      剥离 {p}")
            if len(show) > 5:
                print(f"      ... 其余 {len(show)-5} 个冲突条目")
        if d.verify:
            v = d.verify
            if v.get("ok"):
                print(f"      ✔ 官方复核：-List {v['listed']} 条 / -Test 通过")
            elif v.get("ok") is False:
                print(f"      ✘ 官方复核失败：{v}")
        if d.out_path:
            print(f"      输出 {d.out_path}（{d.out_bytes:,} 字节）")

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print("汇总")
    buckets: dict[str, list[Diagnosis]] = {}
    for d in results:
        buckets.setdefault(d.verdict, []).append(d)
    for k in ("已转换（剥离冲突）", "已转换", "本来就可用", "已被官方完全取代", "无法处理"):
        if k in buckets:
            print(f"   {k:<14} {len(buckets[k])}")
    for k, v in buckets.items():
        if k not in ("已转换（剥离冲突）", "已转换", "本来就可用",
                     "已被官方完全取代", "无法处理"):
            print(f"   {k:<14} {len(v)}")

    print("\n行动建议")
    for d in results:
        print(f"   {os.path.basename(d.src)}")
        print(f"      {d.action}")
        if d.out_path:
            print(f"      转换结果：{d.out_path}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([{
                "src": d.src, "verdict": d.verdict, "ok": d.ok, "error": d.error,
                "version": d.version, "mount": d.mount, "methods": d.methods,
                "total_entries": d.total_entries, "recovered": d.recovered,
                "official_entries": d.official_entries,
                "conflict_entries": d.conflict_entries, "kept": d.kept,
                "dropped": d.dropped, "out_path": d.out_path,
                "out_bytes": d.out_bytes, "actions": d.actions,
                "problems": d.problems, "verify": d.verify,
                "dropped_paths": sorted(getattr(d, "_drop", [])),
                "matched_full": d.matched_full, "matched_bare": d.matched_bare,
                "size_mismatch": [{"path": r, "mod_bytes": m, "official_bytes": o}
                                  for r, m, o in d.size_mismatch],
                "kept_modified": [{"asset": s, "path": r, "mod_bytes": m,
                                   "official_bytes": o}
                                  for s, r, m, o in d.kept_modified],
            } for d in results], f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {args.json}")

    bad = [d for d in results if not d.ok]
    return 1 if bad and len(bad) == len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
