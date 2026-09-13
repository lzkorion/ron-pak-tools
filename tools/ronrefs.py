#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ronrefs.py —— 引用分析：模组引用的那些资产，游戏更新后还在不在？

为什么需要这个：
    官方指南说 RoN 模组是拿 UAssetGUI 直接改「游戏烤好的资产」做的，所以
    模组资产【内部记着它引用的包路径】。游戏一更新，官方经常把资产改名、
    搬目录、拆成好几份 —— 实测：

        /Game/ReadyOrNot/Character/Shared/LACRIMAL_INST_V2
            -> 游戏现在叫 .../shared/instance/lacrimal_inst_v2
        /Game/Blueprints/Items/DamageCurves/Curve_Damage_Shotgun
            -> 拆成了 curve_damage_shotgun_590 / _bsg / _sawnoff
        /Game/IconTextures/Loadout/icn_12g_bucknew
            -> 加了后缀：icn_12g_bucknew_1024

    模组还在引用老路径：轻则那个资产加载不出来（「装了没效果」），
    重则引用为空直接崩。转换（剥资产）修不了这个 —— 只能指出问题在哪。

本模块【只读不写】：
    1. 逐条读 .uasset/.umap 的载荷（按需 seek，不把大 pak 读进内存）
    2. 抽出它自己记录的包路径 + 它引用的包路径
    3. 把 UE 包路径映射成引擎资产路径，和官方清单对账
    4. 对不上的引用，扫一遍清单给出「游戏现在最可能叫什么」

★ oo2core 不随本程序分发（Epic 的二进制，UE EULA 禁止分发）。
  这里只找【用户自己机器上】已经有的副本（游戏目录 / UE 安装目录 /
  System32），找不到就明确说「压缩资产读不了」，绝不静默给错结论。
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pakfmt as P
import ronconvert as RC

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"
LEVEL_OK = "ok"
LEVEL_INFO = "info"

# 单个资产最大扫描字节数：正常 .uasset 只有几 MB，超过这个量级的多半是
# 被错当成包的大文件，扫了也只是浪费时间和内存。
MAX_ASSET_BYTES = 48 * 1024 * 1024

# 引用分析的默认时间预算（秒）。到点就停，并如实报告「只分析了前 N 个」。
DEFAULT_BUDGET = 180.0

# UE 包路径：/Game/... 、/Plugin/... 、/Script/...（全是标识符字符）
PKG_RE = re.compile(rb"/(?:[A-Za-z0-9_]+/)+[A-Za-z0-9_]+")

# 设备目录：这些不是资产包，别当引用报出来
SKIP_ROOTS = ("/script", "/engine", "/temp", "/game/__externalactors")

# ---------------------------------------------------------------------------
# oo2core：只加载「用户本机已有」的副本
# ---------------------------------------------------------------------------
_ODLE = None
_ODLE_PATH = ""
_ODLE_TRIED = False

_ODLE_SIG = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_int64,
             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
             ctypes.c_int64, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
             ctypes.c_int64, ctypes.c_int]


def _oodle_roots() -> list[str]:
    """oo2core 可能在哪：游戏目录 -> UE 安装目录（浅层优先）。"""
    roots: list[str] = []
    try:
        paks = RC.find_game_paks() or ""
    except Exception:
        paks = ""
    if paks:
        # <游戏根>\ReadyOrNot\Content\Paks -> <游戏根>
        game = os.path.abspath(os.path.join(paks, "..", "..", ".."))
        roots += [os.path.join(game, "ReadyOrNot", "Binaries", "Win64"),
                  os.path.join(game, "Engine", "Binaries", "Win64"), game]
    for pat in (r"C:\Program Files\Epic Games\UE_*",
                r"C:\Program Files (x86)\Epic Games\UE_*"):
        for ue in glob.glob(pat):
            roots += [os.path.join(ue, "Engine", "Binaries", "Win64"),
                      os.path.join(ue, "Engine", "Binaries"), ue]
    # ★ glob 的 ?: 匹配不到盘符（"?:\\UE_*" 是空列表），得自己枚举盘符
    import string
    ue_dirs: list[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        ue_dirs += glob.glob(os.path.join(drive, "UE_*"))
        ue_dirs += glob.glob(os.path.join(drive, "Epic Games", "UE_*"))
    for ue in ue_dirs:
        roots += [os.path.join(ue, "Engine", "Binaries", "Win64"),
                  os.path.join(ue, "Engine", "Binaries"), ue]
    roots.append(r"C:\Windows\System32")
    out, seen = [], set()
    for r in roots:
        k = r.lower()
        if k not in seen and os.path.isdir(r):
            seen.add(k)
            out.append(r)
    return out


def _walk_dlls(root: str, max_depth: int, limit: int = 60) -> list[str]:
    """有深度上限地找 oo2core*.dll（不做全盘递归）。"""
    out: list[str] = []
    base = root.rstrip("\\/").count(os.sep)
    for cur, dirs, files in os.walk(root):
        if cur.count(os.sep) - base >= max_depth:
            dirs[:] = []
        for f in files:
            fl = f.lower()
            if fl.startswith("oo2core") and fl.endswith(".dll"):
                out.append(os.path.join(cur, f))
                if len(out) >= limit:
                    return out
    return out


def find_oodle(refresh: bool = False):
    """找并加载 oo2core.dll。返回 (OodleLZ_Decompress 函数或 None, dll 路径)。

    ★ 只搜索本机已有的副本，绝不打包、绝不下发。没有它只是「压缩资产读不了」，
      功能会明确降级，不会给出错误结论。
    """
    global _ODLE, _ODLE_PATH, _ODLE_TRIED
    if _ODLE_TRIED and not refresh:
        return _ODLE, _ODLE_PATH
    _ODLE_TRIED = True
    _ODLE, _ODLE_PATH = None, ""

    cands: list[str] = []
    for root in _oodle_roots():
        cands += _walk_dlls(root, max_depth=4 if root.endswith("Win64") else 8)
        if cands:
            break                       # 找到一层就不再往下找别的根
    # 游戏/引擎自己的 Binaries 优先（编码器版本最接近），AutomationTool 兜底
    cands.sort(key=lambda p: (p.lower().count("automationtool"),
                              p.lower().count("win-x86"), len(p)))
    for c in cands:
        try:
            lib = ctypes.WinDLL(c)
            fn = lib.OodleLZ_Decompress
            fn.restype = ctypes.c_int64
            fn.argtypes = _ODLE_SIG
        except Exception:
            continue
        _ODLE, _ODLE_PATH = fn, c
        return _ODLE, _ODLE_PATH
    return None, ""


def oodle_state() -> str:
    fn, path = find_oodle()
    return path if fn else ""


def _oodle_one(fn, blob: bytes, out_size: int) -> bytes:
    out = ctypes.create_string_buffer(max(out_size, 1))
    got = fn(blob, len(blob), out, out_size, 0, 0, 0, None, 0,
             None, None, None, 0, 0)
    if got <= 0:
        raise P.PakError(f"Oodle 解压失败（返回 {got}）")
    return out.raw[:got]


def decompress_any(method: str, payload: bytes, block_lengths=(),
                   uncompressed_size: int = 0,
                   block_size: int = 0) -> bytes:
    """把条目载荷解开。Zlib / 不压缩 / Oodle 三种都支持（Oodle 需本机有 dll）。"""
    m = (method or "").strip().lower()
    if not m or m in ("none", "raw"):
        return payload
    if m in ("zlib", "gzip"):
        return P.decompress_payload(m, payload, block_lengths,
                                    uncompressed_size)
    if m != "oodle":
        raise P.PakError(f"不认识的压缩方式：{method}")
    fn, _path = find_oodle()
    if fn is None:
        raise P.PakError("这台机器上没有 oo2core.dll，Oodle 压缩的资产读不了")
    lens = list(block_lengths)
    if len(lens) <= 1:
        return _oodle_one(fn, payload, uncompressed_size or len(payload) * 4)
    # 多块：逐块解压后拼接（块表给的是每块【压缩后】长度）
    out = bytearray()
    off, left = 0, uncompressed_size
    for n in lens:
        want = min(block_size or left, left) if left else block_size or 0
        want = want or max(n * 8, 1)
        out += _oodle_one(fn, payload[off:off + n], want)
        off += n
        left = max(0, left - want)
    return bytes(out)


# ---------------------------------------------------------------------------
# UE 包路径 <-> 引擎资产路径
# ---------------------------------------------------------------------------
def ue_pkg_to_engine(pkg: str) -> list[str]:
    """UE 包路径 -> 引擎资产路径候选（小写、不含扩展名）。

    /Game/X            -> readyornot/content/X        （工程内容目录）
    /Plugin/X          -> readyornot/plugins/<p>/content/X
                          readyornot/plugins/gamefeatures/<p>/content/X
                          engine/plugins/<p>/content/X
    /Engine/X          -> engine/content/X

    ★ GameFeatures 那一层是实测补上的：RoN 的 DLC（/ReadyOrNotDLC4/...）实际装在
      readyornot/plugins/gamefeatures/readyornotdlc4/content/ 下面，只试前两条会
      把 AK74M 那种大量引用 DLC 的模组误报成「引用全断了」。
    """
    p = (pkg or "").strip().rstrip("/")
    if not p.startswith("/"):
        return []
    parts = p.lstrip("/").split("/")
    if len(parts) < 2:
        return []
    root, rest = parts[0], "/".join(parts[1:])
    rl, rootl = rest.lower(), root.lower()
    if rootl == "game":
        return [f"readyornot/content/{rl}"]
    if rootl == "engine":
        return [f"engine/content/{rl}", f"engine/plugins/{rl}"]
    # 插件名做根：游戏插件 / GameFeatures 插件 / 引擎插件都可能
    return [f"readyornot/plugins/{rootl}/content/{rl}",
            f"readyornot/plugins/gamefeatures/{rootl}/content/{rl}",
            f"engine/plugins/{rootl}/content/{rl}"]


def engine_candidates(pkg: str) -> list[str]:
    """引用的全部候选路径（带扩展名、含「重复前缀」的变体）。

    真实模组的路径常把 readyornot/ 、content/ 又写了一遍（比如
    /Game/ReadyOrNot/Character/... ），所以每个候选都要再试剥掉一层/两层的版本。
    ★ 剥的是【内容目录之内】的开头，不是把 readyornot/content/ 这两层剥掉 ——
      否则「多写一层 ReadyOrNot」这种真实情况反而漏掉。
    """
    out: list[str] = []
    for base in ue_pkg_to_engine(pkg):
        pre = ""
        rest = base
        for p in ("readyornot/content/", "engine/content/"):
            if rest.startswith(p):
                pre, rest = p, rest[len(p):]
                break
        variants = [pre + rest]
        for _ in range(2):
            head, sep, tail = rest.partition("/")
            if not sep or head not in ("readyornot", "content"):
                break
            rest = tail
            v = pre + rest
            if rest and v not in variants:
                variants.append(v)
        for v in variants:
            for ext in (".uasset", ".umap"):
                c = v + ext
                if c not in out:
                    out.append(c)
    return out


def scan_pkg_strings(blob: bytes) -> list[str]:
    """扫描载荷里所有像「UE 包路径」的字符串（去重、保序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in PKG_RE.finditer(blob):
        s = m.group(0).decode("ascii", "replace")
        # 只接受「包」形态：至少 /Root/Name，且根不是设备目录
        low = s.lower()
        if low.startswith(SKIP_ROOTS):
            continue
        if low.count("/") < 2:
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def asset_refs(blob: bytes, rel_name: str = "") -> dict:
    """从一个资产的载荷里抽出：它自己的包路径 + 它引用的包路径。

    self_pkg 只在【能对上文件名】时才给（内部名 = 文件名），否则留空 —— 宁可
    不报，也不拿「第一个路径串」硬当成它自己。
    """
    strs = scan_pkg_strings(blob)
    base = os.path.splitext(os.path.basename(rel_name))[0].lower() if rel_name else ""
    self_pkg = ""
    if base:
        for s in strs:
            last = s.rsplit("/", 1)[-1]
            # 去掉对象后缀：/Pkg/Object -> Object
            last = last.split(".", 1)[0]
            if last.lower() == base:
                self_pkg = s
                break
    refs = [s for s in strs if s != self_pkg]
    return {"self_pkg": self_pkg, "first_pkg": strs[0] if strs else "",
            "refs": refs, "all": strs}


# ---------------------------------------------------------------------------
# 和官方清单对账
# ---------------------------------------------------------------------------
def build_own_paths(mount: str, rels) -> set[str]:
    """这个模组自己的引擎路径（小写，带扩展名）—— 模组内部互相引用不算「断了」。"""
    out: set[str] = set()
    for rel in rels:
        for c in RC.OfficialAssets.stem_variants(mount, rel):
            out.add(c.lower())
    return out


def _stem_norm(name: str) -> str:
    """取文件名主干并去掉尾巴上的版本号：T_X_v2 / icn_1024 / foo_2 -> t_x / icn / foo。

    ★ 官方更新时最常见的改动就是加后缀（_v2、_1024、_2），归一化后才好找现名。
    """
    s = os.path.splitext(os.path.basename(name))[0].lower()
    s = re.sub(r"_(?:v|ver|version)?\d+$", "", s)
    return s


def suggest_many(missing: list[str], official, limit: int = 3) -> dict:
    """给一批「已断引用」找游戏现在的名字：扫一遍清单，不做全量索引。

    一个引用最多给 limit 个建议；挑法：主干完全一样的最优先，其次是
    前缀包含关系（长度差小的优先）。主干一样时再比【原始名字】的长度差 ——
    否则 lacrimal_inst_v2 会被归一化后的 lacrimal_inst 抢走。
    """
    want: dict[str, list[str]] = {}
    for r in missing:
        want.setdefault(_stem_norm(r), []).append(r)
    out: dict[str, list[str]] = {r: [] for r in missing}
    if not missing or official is None or not getattr(official, "full", None):
        return out

    scored: dict[str, list[tuple]] = {r: [] for r in missing}
    for name in official.full:
        if not name.endswith((".uasset", ".umap")):
            continue
        stem = _stem_norm(name)
        if len(stem) < 6:
            continue
        raw_len = len(os.path.splitext(os.path.basename(name))[0])
        for wstem, refs in want.items():
            if len(wstem) < 6:
                continue
            if stem == wstem:
                rank = 0
            elif ((stem.startswith(wstem) or wstem.startswith(stem))
                  and min(len(stem), len(wstem)) >= 10
                  and abs(len(stem) - len(wstem)) <= 8):
                # ★ 前缀包含要卡住长度：不然 T_Drip 会匹配到
                #   t_dripping_dirt_m 这种八竿子打不着的贴图。
                rank = 1
            else:
                continue
            for r in refs:
                raw_ref_len = len(os.path.splitext(
                    os.path.basename(r))[0].split(".", 1)[0])
                scored[r].append((rank, abs(raw_len - raw_ref_len), name))

    for r, items in scored.items():
        items.sort()
        seen: list[str] = []
        for _rank, _d, name in items:
            if name not in seen:
                seen.append(name)
            if len(seen) >= limit:
                break
        out[r] = seen
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _content_rel(p: str) -> str:
    """归一化成「内容目录内」的相对形式（便于比较是不是同一块地方）。

    ★ 循环剥：同一个资产在真实数据里可能写成 readyornot/content/readyornot/...、
      content/...、readyornot/... 几种样子，比较之前都得归一。
    """
    p = p.lower()
    for _ in range(3):
        for pre in ("readyornot/content/", "engine/content/",
                    "readyornot/", "content/"):
            if p.startswith(pre):
                p = p[len(pre):]
                break
        else:
            break
    return p


def own_prefixes(mount_engine: str, owns: set[str], depth: int = 2) -> set[str]:
    """模组自己用到的目录前缀（内容目录内前 depth 层）。

    用来判断一条断链是不是落在「它自己那块目录」里 —— 落在自己目录里的，
    多半是作者改了名没更新引用 / 忘了打包，而不是游戏更新造成的。

    ★ 必须先把引擎前缀（readyornot/content/）剥掉再取前缀：
      否则「挂载点是游戏根 ../../../」的模组会算出前缀 'readyornot/'，
      于是【所有】官方路径都被当成「它自己的目录」。
    """
    out: set[str] = set()
    for p in owns:
        if mount_engine and not _content_rel(p).startswith(
                _content_rel(mount_engine)):
            if not p.startswith(mount_engine):
                continue
        segs = _content_rel(p).split("/")[:depth]
        if segs and segs[0]:
            out.add("/".join(segs) + "/")
    return out


def analyze_pak(src: str, official, *, limit: int = 0,
                budget: float = DEFAULT_BUDGET, log=None) -> dict:
    """分析一个模组 pak：它引用的资产，游戏里还剩几个？

    返回 dict：
        {ok, error, entries, assets, read, skipped, skipped_reason,
         refs, refs_alive, refs_own,
         missing[{asset, ref, in_own_tree, suggest[]}],
         broken{"renamed": [...], "gone": [...]},   # 按引用路径去重
         misplaced[{asset, internal, actual}], truncated, seconds}
    """
    emit = log or (lambda *_a, **_k: None)
    t0 = time.time()
    res: dict = {"src": src, "name": os.path.basename(src), "ok": False,
                 "error": "", "entries": 0, "assets": 0, "read": 0,
                 "skipped": 0, "skipped_reason": "", "refs": 0,
                 "refs_alive": 0, "refs_own": 0, "missing": [],
                 "broken": {"renamed": [], "gone": []},
                 "misplaced": [], "truncated": False, "seconds": 0.0}
    try:
        pk = P.read_pak_index(src)
        pairs = pk.paths_with_entries()
    except Exception as ex:
        res["error"] = f"{type(ex).__name__}: {ex}"
        res["seconds"] = time.time() - t0
        return res

    res["ok"] = True
    res["entries"] = len(pairs)
    res["mount"] = pk.mount_point
    method_names = list(pk.compression_methods)
    own = build_own_paths(pk.mount_point, pairs.keys())
    mount_engine = RC.OfficialAssets._normalize(pk.mount_point).strip("/")
    mount_engine = (mount_engine + "/") if mount_engine else ""
    mine = own_prefixes(mount_engine, own)
    asset_items = [(rel, e) for rel, e in sorted(pairs.items())
                   if rel.lower().endswith((".uasset", ".umap"))]
    res["assets"] = len(asset_items)

    for rel, e in asset_items:
        if budget and time.time() - t0 > budget:
            res["truncated"] = True
            break
        if limit and res["read"] >= limit:
            res["truncated"] = True
            break
        mi = e.method_index
        mname = method_names[mi - 1] if 0 < mi <= len(method_names) else ""
        if e.uncompressed_size > MAX_ASSET_BYTES:
            res["skipped"] += 1
            continue
        try:
            raw = pk.payload_of(e)
            if e.size != e.uncompressed_size:
                blob = decompress_any(mname, raw, e._block_lengths,
                                      e.uncompressed_size,
                                      e.compression_block_size)
            else:
                blob = raw
        except Exception as ex:
            res["skipped"] += 1
            if not res["skipped_reason"]:
                res["skipped_reason"] = f"{type(ex).__name__}: {ex}"
            continue
        res["read"] += 1

        info = asset_refs(blob, rel)
        actual = RC.OfficialAssets.full_path_of(pk.mount_point, rel)
        # 1) 内部包路径 vs pak 里的位置
        self_pkg = info["self_pkg"]
        if self_pkg:
            cands = engine_candidates(self_pkg)
            ext = os.path.splitext(rel)[1].lower()
            want = [c for c in cands if c.endswith(ext)]
            if want and actual.lower() not in want:
                res["misplaced"].append({"asset": rel, "internal": self_pkg,
                                         "actual": actual, "expect": want[0]})
        # 2) 引用对账
        for ref in info["refs"]:
            res["refs"] += 1
            cands = engine_candidates(ref)
            if not cands:
                continue
            if any(c in official.full for c in cands):
                res["refs_alive"] += 1
                continue
            if (own & set(cands)) or actual.lower() in cands:
                res["refs_own"] += 1          # 引用模组自己的东西
                continue
            mapped = cands[0]
            res["missing"].append({
                "asset": rel, "ref": ref, "mapped": mapped,
                "in_own_tree": _content_rel(mapped).startswith(tuple(mine)),
                "suggest": []})

    if res["missing"]:
        sug = suggest_many([m["ref"] for m in res["missing"]], official)
        for m in res["missing"]:
            m["suggest"] = sug.get(m["ref"], [])
    # 按引用路径去重（同一个路径被几十个资产引用是常态），
    # 并分成「游戏里还有近似名（多半是改了名/搬了目录）」和「彻底没有」。
    for m in res["missing"]:
        key = "renamed" if m["suggest"] else "gone"
        got = res["broken"][key]
        for item in got:
            if item["ref"] == m["ref"]:
                item["assets"].append(m["asset"])
                break
        else:
            got.append({"ref": m["ref"], "mapped": m["mapped"],
                        "in_own_tree": m["in_own_tree"],
                        "assets": [m["asset"]], "suggest": m["suggest"]})
    res["missing_assets"] = len(res["missing"])
    res["seconds"] = time.time() - t0
    n_uniq = sum(len(v) for v in res["broken"].values())
    emit(f"   引用分析：{res['name']} —— 读 {res['read']}/{res['assets']} 个资产，"
         f"引用 {res['refs']} 个，其中 {n_uniq} 个路径游戏里已经没有"
         f"（{res['seconds']:.1f} 秒）")
    return res


def render_report(r: dict, log=None) -> None:
    """打印一个 pak 的引用分析结果（分组 + 去重，不刷屏）。"""
    emit = log or print
    emit("")
    emit(f"── 引用分析：{r['name']}")
    if not r["ok"]:
        emit(f"   ✘ 读不了：{r['error']}")
        return
    if r["skipped"] and not r["read"]:
        emit(f"   ⚠ {r['skipped']} 个资产都读不了：{r['skipped_reason']}")
        emit("      要分析压缩资产，本机需要有 oo2core.dll ——"
             "装了 Unreal Engine 的话会自动找到（本程序不附带它）。")
        return
    emit(f"   资产 {r['assets']} 个，读出 {r['read']} 个"
         + (f"，{r['skipped']} 个读不了（{r['skipped_reason']}）"
            if r["skipped"] else ""))
    n_uniq = sum(len(v) for v in r["broken"].values())
    emit(f"   引用 {r['refs']} 个：游戏里还在 {r['refs_alive']} 个，"
         f"模组自己的 {r['refs_own']} 个，【已断 {n_uniq} 个路径】")
    if r["truncated"]:
        emit(f"   （时间/数量到上限，只分析了前 {r['read']} 个资产）")

    if not n_uniq:
        emit("   ✔ 它引用的官方资产游戏里都还在 —— 引用这一层没问题")

    titles = {
        "renamed": "游戏里还有近似的名字（多半是这次更新改名/搬了目录）",
        "gone": "游戏里彻底没有这个包名（作者没打包进来，或官方删掉了）",
    }
    for key in ("renamed", "gone"):
        items = r["broken"][key]
        if not items:
            continue
        emit("")
        emit(f"   ✘ {titles[key]}：{len(items)} 个路径")
        for it in items[:8]:
            tag = "（在模组自己的目录里）" if it["in_own_tree"] else ""
            emit(f"      {it['ref']}{tag}")
            if it["suggest"]:
                emit(f"         游戏里最接近的是 {it['suggest'][0]}")
            n = len(it["assets"])
            emit(f"         引用它的资产：{it['assets'][0]}"
                 + (f" 等 {n} 个" if n > 1 else ""))
        if len(items) > 8:
            emit(f"      ... 其余 {len(items) - 8} 个路径")

    for mp in r["misplaced"][:5]:
        emit(f"   ⚠ {mp['asset']} 内部记的包路径是 {mp['internal']}，"
             f"但它在 pak 里放在 {mp['actual']}")
        emit(f"      （按包名找文件时是对不上的：应该放在 {mp['expect']}）")


def main() -> int:
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(
        description="引用分析：模组引用的资产，游戏更新后还在不在？（只读，不改文件）")
    ap.add_argument("target", help="单个 .pak，或装着模组的目录")
    ap.add_argument("--manifest", default=None, help="官方资产清单")
    ap.add_argument("--limit", type=int, default=0, help="每个 pak 最多分析几个资产")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                    help=f"每个 pak 的时间预算（秒，默认 {DEFAULT_BUDGET:.0f}）")
    ap.add_argument("--json", default=None, help="把结果写入 JSON")
    args = ap.parse_args()

    if not os.path.exists(args.target):
        print(f"路径不存在：{args.target}")
        return 1
    man = args.manifest
    if not man:
        for n in ("manifest_local.json", "game_manifest_full.json",
                  "game_manifest.json"):
            for d in (os.getcwd(), os.path.dirname(os.path.abspath(__file__)),
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..")):
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    man = p
                    break
            if man:
                break
    if not man or not os.path.isfile(man):
        print("没有官方清单，没法对账（用 --manifest 指定，或用主程序先生成一份）")
        return 1
    official = RC.OfficialAssets(man)
    print(f"官方清单：{official.source}")
    oo = oodle_state()
    print(f"oo2core  ：{oo or '（没找到 —— 只能分析未压缩的资产）'}")
    print("=" * 78)

    paks = RC.find_paks(args.target)
    paks = [p for p in paks if not RC.is_official_pak(os.path.basename(p))]
    if not paks:
        print(f"没找到模组 pak：{args.target}")
        return 1
    results = []
    for src in paks:
        r = analyze_pak(src, official, limit=args.limit, budget=args.budget)
        render_report(r)
        results.append(r)

    n_bad = sum(1 for r in results if r["missing"])
    print("\n" + "=" * 78)
    print(f"分析 {len(results)} 个 pak：{n_bad} 个有断掉的引用")
    for r in results:
        uniq = sum(len(v) for v in r["broken"].values())
        if not uniq:
            continue
        print(f"   {r['name']}")
        print(f"      断链 {uniq} 个路径：疑似改名/搬走 "
              f"{len(r['broken']['renamed'])} 个，彻底没有 "
              f"{len(r['broken']['gone'])} 个（{r['seconds']:.1f} 秒）")
    if args.json:
        import json
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
