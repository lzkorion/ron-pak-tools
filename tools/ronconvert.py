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

用法
    # 整个目录批量转换
    python tools/ronconvert.py "C:\\ron模组"

    # 先看诊断，不写文件
    python tools/ronconvert.py "C:\\ron模组" --dry-run

    # 转换并调用官方工具复核
    python tools/ronconvert.py "C:\\ron模组" --verify

    # 单个文件
    python tools/ronconvert.py "某模组.pak"

    # 强制全部剥离（官方已有的路径一律丢弃，包最小但可能改坏贴图类 mod）
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

# 打包/派生文件扩展名（当同名的 .uasset 被保留时，它们也必须保留）
PACKAGE_EXTS = (".uasset", ".umap")
SIDECAR_EXTS = (".uexp", ".ubulk", ".uptnl", ".m.ubulk")


# ===========================================================================
# 官方资产清单
# ===========================================================================
class OfficialAssets:
    """官方已有路径的索引，支持「全路径」与「裸文件名」两级匹配。

    两级匹配的必要性：
      · 从游戏 paks 生成的「全路径清单」里条目带目录，可以精确匹配
        （同名不同目录不会误判）。
      · 旧的 game_manifest.json 只有裸文件名（177241 条里 177233 条没斜杠），
        只能用文件名匹配，会偏保守（宁可少剥）。
    因此：带斜杠的条目进 full 集合，所有条目都进 bare 集合。
    """

    def __init__(self, manifest_path: str | None = None):
        self.full: set[str] = set()
        self.bare: set[str] = set()
        self.source = "(未加载)"
        self.generated_at: float | None = None    # 清单生成时间（若清单里记了）
        if manifest_path and os.path.isfile(manifest_path):
            self._load_manifest(manifest_path)

    @staticmethod
    def _normalize(rel: str) -> str:
        s = rel.replace("\\", "/").strip().lower()
        # 折叠 ../ 与 ./，并去掉挂载点前缀留下的相对段
        while s.startswith("../"):
            s = s[3:]
        s = posixpath.normpath("/" + s.lstrip("/")).lstrip("/")
        return s

    @classmethod
    def full_path_of(cls, mount: str, rel: str) -> str:
        """模组内的 (mount, rel) -> 官方命名空间下的全路径。

        模组: mount='../../../ReadyOrNot/' rel='Content/A.uasset' -> 'readyornot/content/a.uasset'
        官方: mount='../../../'          entry='ReadyOrNot/Content/A.uasset' -> 同上
        """
        m = mount.replace("\\", "/")
        combined = m.rstrip("/") + "/" + rel.lstrip("/") if m.strip("/") else rel
        return cls._normalize(combined)

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
            self.bare.add(s.rsplit("/", 1)[-1])
        self.source = f"{path}（{len(names)} 条）"

    def add_paths(self, paths) -> None:
        for n in paths:
            s = self._normalize(n)
            if not s:
                continue
            self.full.add(s)
            self.bare.add(s.rsplit("/", 1)[-1])

    def has(self, full_path: str) -> tuple[bool, str]:
        """返回 (是否官方已有, 命中方式)。传入路径会先归一化，大小写不敏感。"""
        s = self._normalize(full_path)
        if not s:
            return False, ""
        if s in self.full:
            return True, "full"
        if s.rsplit("/", 1)[-1] in self.bare:
            return True, "bare"
        return False, ""

    def __len__(self) -> int:
        """条目总数。

        注意用 bare 而不是 full：内置清单全是裸文件名，full 会是 0
        （从 v1.1 起 full 只放真正带目录的路径）。
        """
        return len(self.bare)


def build_manifest_from_game_paks(paks_dir: str, out_json: str,
                                  verbose: bool = True, progress=None) -> int:
    """用自研解析器从游戏 paks 生成【全路径】清单（比裸文件名匹配准确得多）。

    ★ 只统计游戏本体 pak（pakchunk<N>-<Platform>.pak），跳过玩家装的模组。
      否则模组内容会被写进「官方清单」，之后判定就会认为「官方已有」→ 全被剥掉。

    progress: 可选的 callable(stage:str, done:int, total:int, note:str)，
              用于把进度报给 GUI（扫 44GB 要几分钟）。
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
            pk = P.read_pak(path)
            official.add_paths(pk.all_paths())
            del pk
            if verbose:
                _emit_default(verbose, f" 累计 {len(official)}")
        except Exception as ex:
            if verbose:
                _emit_default(verbose,
                              f" 跳过（{type(ex).__name__}: {ex}）")
        rep("pak", i, len(files), name)
    write_manifest(out_json, official.bare, {
        "source_dir": paks_dir,
        "pak_count": len(files),
        "skipped_paks": skipped,
        "generated_by": "ronconvert.build_manifest_from_game_paks",
    })
    if verbose:
        _emit_default(
            verbose,
            f"   已写出 {out_json}：{len(official)} 条，"
            f"耗时 {time.time()-t0:.1f}s")
    return len(official)


def _emit_default(verbose: bool, msg: str, **kw) -> None:
    if verbose:
        print(msg, **kw)


def write_manifest(path: str, names, extra: dict | None = None) -> None:
    """写清单 JSON，并记下生成时间（用于判断清单是否过期）。"""
    data = {
        "generated_at": time.time(),
        "generated_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(names),
        "names": sorted(names),
    }
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
    pk = P.read_pak(path)
    idx = pk.read_directory_index("fdi") or pk.read_directory_index("phi")
    if not idx:
        raise P.PakError("pak 里没有目录索引，无法恢复路径")
    loc_to_path: dict[int, str] = {}
    for dname, files in idx.items():
        for fname, loc in files.items():
            loc_to_path[loc] = (dname + fname).lstrip("/")
    entries: dict[str, P.PakEntry] = {}
    q = 0
    for e in pk.encoded_entries:
        rel = loc_to_path.get(q)
        q += len(P.encode_entry_index(e))
        if rel:
            entries[rel] = e
    return pk, entries


def diagnose(src: str, official: OfficialAssets, *, strip_all: bool = False,
             verbose: bool = True, log=None) -> Diagnosis:
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
    d.total_entries = len(pk.encoded_entries)
    d.recovered = len(entries)

    if pk.version not in SUPPORTED_VERSIONS:
        d.problems.append(f"pak 版本 {pk.version} 不在支持范围 {SUPPORTED_VERSIONS}")

    # ---- 逐个条目判定 ----
    elist: list[Entry] = []
    for rel, pe in sorted(entries.items()):
        ext = os.path.splitext(rel)[1].lower()
        full = OfficialAssets.full_path_of(pk.mount_point, rel)
        has, how = official.has(full)
        conflict = _is_conflict_type(rel, ext)
        elist.append(Entry(rel=rel, path=pe, full=full, official=has,
                           official_by=how, conflict=conflict))
    by_rel = {e.rel: e for e in elist}

    # ---- 决定丢弃集合 ----
    # UE 里一个资产 = 多个条目（.uasset/.umap + .uexp/.ubulk/.uptnl）。
    # 只剥其中一部分会留下"孤儿"（例如 asset 没了但 uexp 还在），
    # 可能让引擎读到不匹配的组合。所以按【资产 stem】成组处理：
    #   某 stem 下只要有【任何一个】可剥的 .uasset/.umap，整个 stem 一起剥。
    drop: set[str] = set()
    for e in elist:
        ext = os.path.splitext(e.rel)[1].lower()
        if strip_all:
            if e.official and ext in (".uasset", ".umap", ".uexp", ".ubulk",
                                      ".uptnl", ".ini", ".bin"):
                drop.add(e.rel)
            continue
        if e.official and e.conflict:
            drop.add(e.rel)

    all_stems = {os.path.splitext(e.rel)[0] for e in elist}
    dropped_stems: set[str] = set()
    for rel in drop:
        stem, ext = os.path.splitext(rel)
        if ext.lower() in PACKAGE_EXTS:
            dropped_stems.add(stem)
    for e in elist:
        stem, ext = os.path.splitext(e.rel)
        if ext.lower() in SIDECAR_EXTS and stem in dropped_stems:
            drop.add(e.rel)

    d.conflict_entries = sum(1 for e in elist if e.conflict)
    d.official_entries = sum(1 for e in elist if e.official)
    d.conflict_paths = [e.rel for e in elist if e.conflict]
    d.dropped = len(drop)
    d.kept = d.total_entries - d.dropped

    emit(f"   版本 {d.version}   挂载点 {d.mount!r}   方法 {d.methods}")
    emit(f"   条目 {d.total_entries}（恢复路径 {d.recovered}）"
         f"   官方已有 {d.official_entries}   冲突型 {d.conflict_entries}")
    for p in d.problems:
        emit(f"   ⚠ {p}")

    if d.dropped == 0:
        d.actions.append("无可剥离内容（内容都该保留）")
    else:
        d.actions.append(f"剥离 {d.dropped} 个冲突条目，保留 {d.kept} 个")
    if d.kept == 0:
        d.problems.append("剥离后无剩余内容：该模组的冲突部分已被官方完全取代")

    d._elist = elist          # 供 convert 使用
    d._drop = drop
    d._pak = pk
    d.seconds = time.time() - t0
    return d


def convert(d: Diagnosis, outdir: str, *, verify: bool = False,
            target_version: int | None = None, log=None) -> Diagnosis:
    """把诊断结果落成文件。"""
    emit = log or (lambda *_a, **_k: None)
    if not d.ok:
        return d
    os.makedirs(outdir, exist_ok=True)
    name = os.path.basename(d.src)
    out = os.path.join(outdir, name)
    pk: P.PakFile = d._pak
    elist: list[Entry] = d._elist
    drop: set[str] = d._drop
    tv = target_version or d.version

    if d.dropped == 0 and tv == d.version:
        # 完全不需要改：直接复制，保证字节一致、零风险
        emit("   正在原样复制（无需改动）...")
        shutil.copy2(d.src, out)
        d.out_path = out
        d.out_bytes = os.path.getsize(out)
        d.copied = True
        d.actions.append("无需修改，已原样复制")
    else:
        emit(f"   正在重新打包（保留 {d.kept} 条 / 剥离 {d.dropped} 条）...")
        w = P.PakWriter(d.mount, methods=list(pk.compression_methods), version=tv)
        for e in elist:
            if e.rel in drop:
                continue
            pe = e.path
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
        d.actions.append(f"写出 {os.path.basename(out)}（{info['entries']} 条）")

    # 自研读回自检
    emit("   自检：读回生成的 pak ...")
    try:
        chk = P.read_pak(out)
        got = sorted(chk.all_paths())
        want = sorted(e.rel for e in elist if e.rel not in drop)
        if got != want:
            d.problems.append(f"自检失败：写回路径 {len(got)} 条，期望 {len(want)} 条")
    except Exception as ex:
        d.problems.append(f"自检读取失败：{type(ex).__name__}: {ex}")

    if verify:
        emit("   校验：调用官方 UnrealPak -List / -Test（大模组可能要等一会）...")
        d.verify = unrealpak_check(out)
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
                    help="直接从游戏 Paks 目录读官方清单（最准，需数分钟）")
    ap.add_argument("--strip-all", action="store_true",
                    help="激进模式：官方已有的路径一律剥离（可能改坏贴图类 mod）")
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
    print(f"官方资产清单：{official.source}   全路径 {len(official.full)} 条")
    if len(official) == 0:
        print("⚠ 清单为空，无法判断「官方已有」。请先运行：")
        print('   python tools\\ronadapt.py --game-manifest "...\\pakchunk0-Windows.pak"')
        print("   或加 --game-paks <游戏Paks目录> 自动生成全路径清单")
    elif not args.manifest or not os.path.isfile(args.manifest):
        print("  （裸文件名匹配：可能出现同名误判，建议用 --game-paks 生成全路径清单）")

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
        d = diagnose(src, official, strip_all=args.strip_all)
        if not args.dry_run and d.ok:
            convert(d, outdir, verify=args.verify,
                    target_version=args.target_version)
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
            } for d in results], f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {args.json}")

    bad = [d for d in results if not d.ok]
    return 1 if bad and len(bad) == len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
