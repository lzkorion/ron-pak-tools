#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RoN 模组转换器 —— 图形界面版

双击运行（或 python ronconvert_gui.py）。选一个模组文件夹，点「开始转换」，
工具会自动检测并用 ronconvert.py 的算法把旧模组转成可用模组。

设计要点
    · 转换在【独立子进程】里跑（multiprocessing 'spawn'），
      所以界面不会卡死，子进程崩溃也不会把主界面带走。
    · 子进程通过 Queue 回传日志与每个 pak 的结果 -> 实时进度 + 汇总统计。
    · 所有异常都在子进程内被捕获并回报，主界面永不闪退。
    · 原文件只读，输出固定写到 <所选目录>\\converted。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import queue as queue_mod
import subprocess
import sys
import time
import traceback

# ---------------------------------------------------------------------------
# 让 ronconvert / pakfmt 能被导入（源码运行 与 PyInstaller 打包 都能用）
# ---------------------------------------------------------------------------
def _base_dir() -> str:
    """可执行文件所在目录（打包后）或本脚本所在目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


_BUNDLE = getattr(sys, "_MEIPASS", None)      # PyInstaller 解包目录
_BASE = _base_dir()
for _p in (_BUNDLE, _BASE, os.path.join(_BASE, "tools")):
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
# 说明：spawn 出来的子进程会重新 import 本模块，所以这段路径设置必须在
# 模块顶层执行，子进程才能找到 ronconvert / pakfmt。

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "RoN Pak Tools"
NOTICE = ("非官方第三方工具，与 VOID Interactive / Epic Games 无关联。"
          "本程序不含任何游戏资产或 Epic 工具。")
DEFAULT_GAME_PAKS = (r"E:\SteamLibrary\steamapps\common\Ready Or Not"
                     r"\ReadyOrNot\Content\Paks")


# ---------------------------------------------------------------------------
# 控制台输出：--windowed 打包后 sys.stdout 可能是 None（无控制台），
# 而且终端编码可能不支持 ✔ ✘ ⚠ 等字符。这里统一处理，避免自检时崩掉。
# ---------------------------------------------------------------------------
def _setup_stdout() -> None:
    if os.name == "nt":
        try:
            import ctypes
            # 若父进程有控制台（从 cmd/pwsh 运行），附加上去
            ctypes.windll.kernel32.AttachConsole(-1)
        except Exception:
            pass
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            try:                       # 无控制台时用无操作流兜底
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass
            continue
        for enc in ("utf-8", "gbk"):
            try:
                s.reconfigure(encoding=enc, errors="replace")
                break
            except Exception:
                continue


_setup_stdout()


def _out(msg: str = "") -> None:
    """安全打印：任何情况下都不抛异常。"""
    try:
        print(msg)
    except Exception:
        for ch in ("✔", "✘", "⚠", "·", "─", "→", "…"):
            msg = msg.replace(ch, "?")
        try:
            print(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 清单定位
#
# ★ 本程序【不附带】任何官方资产清单。清单里含游戏资产名，属于游戏派生数据，
#   不能随开源项目分发。改为：首次运行时用你自己的游戏安装在本机生成一份，
#   存在程序旁边（manifest_local.json），之后自动复用。
# ---------------------------------------------------------------------------
MANIFEST_NAME = "manifest_local.json"
LEGACY_MANIFEST_NAMES = ("game_manifest_full.json", "game_manifest.json")


def manifest_path() -> str:
    """本机清单应放的位置（程序旁边）。"""
    return os.path.join(_base_dir(), MANIFEST_NAME)


def find_manifest() -> tuple[str | None, str]:
    """找到可用的清单。返回 (路径, 说明)。

    查找顺序：程序旁边的 manifest_local.json → 兼容旧文件名 → 打包目录里的副本。
    """
    p = manifest_path()
    if os.path.isfile(p):
        return p, f"本机清单 {MANIFEST_NAME}"
    for n in LEGACY_MANIFEST_NAMES:
        q = os.path.join(_base_dir(), n)
        if os.path.isfile(q):
            return q, f"本机清单 {n}"
    if _BUNDLE:
        for n in (MANIFEST_NAME,) + LEGACY_MANIFEST_NAMES:
            q = os.path.join(_BUNDLE, n)
            if os.path.isfile(q):
                return q, f"内置清单 {n}"
    return None, "尚未生成官方资产清单"


def generate_manifest(game_paks: str, log=None) -> tuple[bool, str]:
    """从本机游戏 paks 生成清单，写到程序旁边。返回 (成功?, 说明)。

    log: 可选的 callable(str)，用于把进度显示到界面。
    """
    def say(s):
        if log:
            try:
                log(s)
            except Exception:
                pass

    try:
        import ronconvert as RC
    except Exception as ex:
        return False, f"无法加载转换模块：{type(ex).__name__}: {ex}"
    if not game_paks or not os.path.isdir(game_paks):
        return False, "没有找到游戏安装目录"
    out = manifest_path()
    try:
        def prog(stage, done, total, note):
            if total and (done == 0 or done == total or done % 5 == 0):
                say(f"正在生成清单 {done}/{total}：{note}")
        n = RC.build_manifest_from_game_paks(game_paks, out, verbose=False,
                                             progress=prog)
    except Exception as ex:
        return False, f"生成失败：{type(ex).__name__}: {ex}"
    if n <= 0:
        return False, "生成结果为空（游戏目录里没有可读的本体 pak？）"
    return True, f"已生成清单：{out}（{n} 条）"


def find_unrealpak() -> str | None:
    for c in (r"G:\UE_5.8\Engine\Binaries\Win64\UnrealPak.exe",):
        if os.path.isfile(c):
            return c
    return None


# ---------------------------------------------------------------------------
# 配置持久化：记住上次选的目录 / 选项（存在 exe 旁边）
# ---------------------------------------------------------------------------
CONFIG_NAME = "ronconvert_gui.json"


def _config_path() -> str:
    return os.path.join(_base_dir(), CONFIG_NAME)


def load_config() -> dict:
    import json
    try:
        with open(_config_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    # 测试环境下不写配置，避免污染工作目录、干扰其它测试
    if os.environ.get("RONPAK_NO_CONFIG"):
        return
    import json
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass          # 配置写不进去也不该影响主流程


# ---------------------------------------------------------------------------
# 子进程 worker —— 必须是模块级函数，且不能是 lambda/闭包（spawn 需要可 pickle）
# ---------------------------------------------------------------------------
def _rename_output(path: str, new_name: str) -> str:
    """把刚生成的产物改成新名字（原地改名，绝不留两份让人不知道用哪个）。

    目标名已存在时**不覆盖**：原样保留，由调用方报告。
    """
    dst = os.path.join(os.path.dirname(os.path.abspath(path)), new_name)
    if os.path.abspath(dst) == os.path.abspath(path):
        return path
    if os.path.exists(dst):
        return path
    os.replace(path, dst)
    return dst


def _worker(mod_dir: str, outdir: str, manifest: str | None,
            verify: bool, strip_all: bool, match_name: bool,
            strip_modified: bool, health_only: bool, repack_raw: bool,
            assess_first: bool, fix_names: bool, game_paks: str | None,
            q) -> None:
    """在子进程中执行转换，通过 q 回传消息。

    消息格式: (kind, payload)
        ("log",  str)                  一行日志
        ("prog", (done, total, name))  进度
        ("result", dict)               单个 pak 的结果
        ("done", dict)                 全部完成
        ("fatal", str)                 致命错误
    """
    import os as _os

    def log(msg: str) -> None:
        q.put(("log", str(msg)))

    try:
        import ronconvert as RC
    except Exception as ex:
        q.put(("fatal", f"无法加载转换模块 ronconvert：{type(ex).__name__}: {ex}"))
        return

    try:
        log("=" * 70)
        log(f"{APP_TITLE}  开始")
        log(f"模组目录 : {mod_dir}")
        log(f"输出目录 : {outdir}")
        match_name = bool(match_name)
        strip_modified = bool(strip_modified)
        repack_raw = bool(repack_raw)
        log(f"官方校验 : {'开启' if verify else '关闭'}")
        log(f"剥离模式 : {'激进（官方已有即剥离）' if strip_all else '智能（只剥冲突型）'}")
        log(f"同名判定 : {'开启（文件名相同也算官方已有，有误剥风险）' if match_name else '关闭（只信路径完全一致）'}")
        log(f"改过的资产: {'剥掉（可能变成能进游戏但什么都不发生）' if strip_modified else '保留（推荐：那是模组的功能本身）'}")
        if repack_raw:
            log("压缩方式 : 改成【不压缩】重新打包（用于模组用了游戏没编进去的压缩方式）")
        if fix_names:
            log("自动改名修复: 开启（文件名缺 _P 后缀 / 加载顺序被压着的，"
                "顺带出一份改好名的）")
        log("=" * 70)

        # ---- 官方清单（由本机游戏生成，不随程序分发）----
        # 清单里含游戏资产名，属于游戏派生数据，不能随开源项目分发。
        # 所以：首次运行 / 勾选重新生成时，用用户自己的游戏安装在本机生成一份。
        if game_paks and _os.path.isdir(game_paks):
            cache = manifest_path()
            log("")
            log(f"正在从本机游戏生成官方资产清单：{game_paks}")
            log("（只统计游戏本体 pakchunkN-Windows.pak，跳过装在同一个文件夹里的模组）")
            log("（清单只写在你自己的电脑上，不含任何游戏资产文件）")
            try:
                def _mprog(stage, done, total, note):
                    q.put(("mprog", (done, total, note)))

                n = RC.build_manifest_from_game_paks(
                    game_paks, cache, verbose=False, progress=_mprog)
                if n > 0:
                    manifest = cache
                    log(f"已生成 {cache}（{n} 条）")
                    log("以后会自动复用这份清单，不必再生成。")
                else:
                    log("⚠ 生成结果为空，继续用现有清单。")
            except Exception as ex:
                log(f"⚠ 生成失败（继续用现有清单）：{type(ex).__name__}: {ex}")

        official = RC.OfficialAssets(manifest)
        log("")
        log(f"官方资产清单：{official.source}")
        log(f"   全路径条目 {len(official.full)} 条，文件名条目 {len(official.bare)} 条")
        if len(official) == 0:
            if health_only:
                log("   ⚠ 没有官方清单：体检照做（挂载点/文件名/成组/加载顺序/"
                    "包格式），但会跳过「路径对不对得上官方」那一项。")
            else:
                q.put(("fatal",
                       "还没有官方资产清单，无法判断哪些内容官方已有。\n\n"
                       "请勾选「先生成官方资产清单」后重试（需要游戏已安装）。\n"
                       "清单在本机生成、只存在你自己电脑上，不含任何游戏资产文件。"))
                return
        if not official.has_full_index and not match_name and not health_only:
            # ★ 旧的裸名清单 + 默认策略 = 一条都不会剥（工具空转）。
            #   必须说清楚，否则用户会以为「转换过了」却什么都没发生。
            q.put(("fatal",
                   "这份官方清单里【只有文件名，没有全路径】，而默认策略只信"
                   "「路径完全一致」，结果会一条都不剥。\n\n"
                   "同名不等于同路径 —— 按文件名剥会把模组自己的贴图/网格剥掉，"
                   "模组直接失效（这正是之前那个 bug）。\n\n"
                   "请勾选「先生成官方资产清单」重新生成一份（几秒即可）；\n"
                   "或勾选「同名也剥」走激进模式自行承担风险。"))
            return
        # 清单是否可能过期（游戏更新会让本体 pak 变新）
        try:
            st = RC.check_manifest_freshness(manifest, game_paks)
            if st.get("status") == "stale":
                log(f"   ⚠ 清单可能过期：{st['reason']}")
                log("      建议勾选「先生成官方资产清单」重新生成一次。")
        except Exception:
            pass

        # ---- 找 pak ----
        paks = RC.find_paks(mod_dir)
        if not paks:
            q.put(("fatal", f"在所选目录里没有找到任何 .pak 文件：\n{mod_dir}\n\n"
                            f"请确认选的是模组目录。"))
            return
        log(f"待处理 {len(paks)} 个 pak")
        log("")

        total = len(paks)
        done = 0
        summary: list[dict] = []
        t_all = time.time()

        if health_only:
            # ---- 体检模式：只诊断「为什么没效果」，不写任何文件 ----
            try:
                import ronhealth as RH
            except Exception as ex:
                q.put(("fatal", f"无法加载体检模块 ronhealth：{type(ex).__name__}: {ex}"))
                return
            log("体检模式：只诊断，不生成任何文件")
            if fix_names:
                log("（体检模式不产出任何文件，所以「自动改名修复」在这里不生效；"
                    "想拿改名副本请取消勾选体检模式）")
            log("")
            peers = RH.scan_peer_paks(game_paks, "") if game_paks else []
            if peers:
                log(f"已读入 {len(peers)} 个同目录模组，用来查加载顺序冲突")
                log("")
            for i, src in enumerate(paks, 1):
                q.put(("prog", (i - 1, total, _os.path.basename(src))))
                try:
                    hr = RH.check(src, official, peers=peers, verify=verify)
                except Exception as ex:
                    log(f"   ✘ 体检出错：{type(ex).__name__}: {ex}")
                    hr = {"name": _os.path.basename(src), "ok": False,
                          "findings": [], "verdict": f"体检出错：{ex}",
                          "errors": 1, "warnings": 0}
                RH.render(hr, log=log)
                done += 1
                summary.append({
                    "name": hr["name"], "verdict": hr.get("verdict", ""),
                    "action": "", "out": "", "dropped": 0, "kept": 0,
                    "ok": hr.get("ok", False),
                    "problems": [f["title"] for f in hr.get("findings", [])
                                 if f["level"] == RH.LEVEL_ERROR],
                })
                q.put(("result", summary[-1]))
                q.put(("prog", (done, total, _os.path.basename(src))))
        else:
            # ---- 先诊断再转换：不推荐转的直接跳过，不产出文件 ----
            RH = None
            if assess_first or fix_names:
                try:
                    import ronhealth as RH          # noqa: F811
                except Exception as ex:
                    RH = None
                    if assess_first:
                        log(f"⚠ 无法加载诊断模块，改为全部转换：{ex}")
                    if fix_names:
                        log(f"⚠ 自动改名修复跳过（模块加载失败）：{ex}")

            fix_cache: dict[str, dict] = {}       # src -> 改名方案

            def rename_of(src: str) -> dict:
                """算出该改成什么名（诊断模式下直接复用诊断结果，不重算）。"""
                if src in fix_cache:
                    return fix_cache[src]
                rn: dict = {}
                if RH is not None:
                    try:
                        rn = RH.rename_plan_for(src, paks_dir=game_paks)
                    except Exception as ex:
                        log(f"   ⚠ 改名分析失败：{type(ex).__name__}: {ex}")
                        rn = {}
                fix_cache[src] = rn
                return rn

            def rename_copy(src: str, tag: str) -> tuple[str, str]:
                """生成一份改好名的副本（原文件不动）。返回 (新路径, 新文件名)。"""
                rn = rename_of(src)
                if RH is None or not rn.get("needed"):
                    return "", ""
                if not rn.get("readable", True):
                    log("   （这个 pak 读不动，不生成改名副本 —— 改名救不了坏文件）")
                    return "", ""
                try:
                    newp = RH.apply_rename(src, rn["new_name"], outdir)
                except Exception as ex:
                    log(f"   ⚠ 改名失败：{type(ex).__name__}: {ex}")
                    return "", ""
                log(f"   ✎ {tag}{_os.path.basename(src)}  ->  "
                    f"{_os.path.basename(newp)}")
                for r in rn.get("reasons", []):
                    log(f"        · {r}")
                return newp, _os.path.basename(newp)

            skip: set[str] = set()
            if assess_first and RH is not None:
                log("先诊断：逐个判断「能不能改、值不值得改」，只转该转的")
                log("=" * 70)
                ares = []
                for i, src in enumerate(paks, 1):
                    q.put(("prog", (i - 1, total, _os.path.basename(src))))
                    try:
                        a = RH.assess(src, official, paks_dir=game_paks,
                                      verify=False)
                    except Exception as ex:
                        a = {"name": _os.path.basename(src), "label": "读不了",
                             "why": f"{type(ex).__name__}: {ex}",
                             "reasons": [], "should_convert": False,
                             "health": {"ok": False, "findings": []}}
                    fix_cache[src] = a.get("rename") or {}   # 改名建议复用诊断结果
                    RH.render_assess(a, log=log)
                    ares.append(a)
                    if not a["should_convert"]:
                        skip.add(src)
                RH.print_assess_table(ares, log=log)
                log("")
                log(f"诊断完毕：{total - len(skip)} 个建议转换，"
                    f"{len(skip)} 个跳过（转了也没用，甚至更糟）")
                log("=" * 70)
                if not skip:
                    log("（没有需要跳过的）")

            for i, src in enumerate(paks, 1):
                q.put(("prog", (i - 1, total, _os.path.basename(src))))
                if src in skip:
                    log("")
                    log(f"── {_os.path.basename(src)}")
                    log("   ⏭ 诊断判定不需要转换（也没生成文件）")
                    renamed = fixed = ""
                    if fix_names:
                        fixed, renamed = rename_copy(src, "改名修复：")
                        if fixed:
                            log("      （原文件没动，改好名的副本已放进输出目录）")
                    done += 1
                    summary.append({
                        "name": _os.path.basename(src), "verdict": "诊断后跳过",
                        "action": ("保持原样（诊断认为转换没有意义）；"
                                   "另出了一份改好名的副本" if fixed else
                                   "保持原样（诊断认为转换没有意义）"),
                        "out": fixed, "dropped": 0, "kept": 0, "ok": True,
                        "problems": [], "renamed": renamed,
                    })
                    q.put(("result", summary[-1]))
                    q.put(("prog", (done, total, _os.path.basename(src))))
                    continue
                try:
                    d = RC.diagnose(src, official, strip_all=strip_all,
                                    match_name=match_name,
                                    strip_modified=strip_modified, log=log)
                    if d.ok:
                        RC.convert(d, outdir, verify=verify, log=log,
                                   raw=repack_raw)
                except Exception as ex:
                    # 单个 pak 出错不应中断整批
                    log(f"   ✘ 处理出错：{type(ex).__name__}: {ex}")
                    log(traceback.format_exc(limit=3))
                    d = RC.Diagnosis(src=src)
                    d.error = f"{type(ex).__name__}: {ex}"
                    d.problems.append("处理时发生异常")

                # 打印结论
                log(f"   ── 结论：{d.verdict}")
                for a in d.actions:
                    log(f"      · {a}")
                for p in d.problems:
                    log(f"      ⚠ {p}")
                if d.dropped:
                    show = sorted(d._drop)
                    for p in show[:5]:
                        log(f"      剥离 {p}")
                    if len(show) > 5:
                        log(f"      ... 其余 {len(show)-5} 个冲突条目")
                if d.verify:
                    v = d.verify
                    if v.get("ok"):
                        log(f"      ✔ 官方复核：-List {v['listed']} 条 / -Test 通过")
                    elif v.get("ok") is False:
                        log(f"      ✘ 官方复核失败：{v}")
                    else:
                        log(f"      ⚠ 未能复核：{v.get('reason', v)}")
                if d.out_path:
                    log(f"      输出 {d.out_path}（{d.out_bytes:,} 字节）")

                # 自动改名修复：产物直接改成该叫的名字（原地改名，不留两份）
                renamed = ""
                if fix_names and d.out_path and RH is not None:
                    rn = rename_of(src)
                    if rn.get("needed"):
                        tgt = rn["new_name"]
                        newp = _rename_output(d.out_path, tgt)
                        if newp != d.out_path:
                            renamed = _os.path.basename(newp)
                            log(f"   ✎ 自动改名修复：{_os.path.basename(src)}  ->  "
                                f"{renamed}")
                            for r in rn.get("reasons", []):
                                log(f"        · {r}")
                            d.out_path = newp
                        else:
                            log(f"   ⚠ 想改名成 {tgt}，但输出目录里已有同名文件，"
                                f"保留原名 {_os.path.basename(d.out_path)}")

                done += 1
                summary.append({
                    "name": _os.path.basename(src), "verdict": d.verdict,
                    "action": d.action, "out": d.out_path,
                    "dropped": d.dropped, "kept": d.kept,
                    "ok": d.ok, "problems": list(d.problems),
                    "renamed": renamed,
                })
                q.put(("result", summary[-1]))
                q.put(("prog", (done, total, _os.path.basename(src))))

        # ---- 汇总 ----
        buckets: dict[str, int] = {}
        for s in summary:
            buckets[s["verdict"]] = buckets.get(s["verdict"], 0) + 1
        n_renamed = sum(1 for s in summary if s.get("renamed"))
        log("")
        log("=" * 70)
        log("汇总")
        for k, v in buckets.items():
            log(f"   {k:<16} {v}")
        if n_renamed:
            log(f"   {'自动改名修复':<16} {n_renamed}")
        log("")
        log("行动建议")
        for s in summary:
            log(f"   {s['name']}")
            log(f"      {s['action']}")
            if s.get("renamed"):
                log(f"      改好名的文件：{s['renamed']}")
        log("")
        log(f"总耗时 {time.time()-t_all:.1f} 秒")
        log(f"输出目录：{outdir}")

        q.put(("done", {"outdir": outdir, "total": total,
                        "buckets": buckets, "summary": summary,
                        "renamed": n_renamed,
                        "seconds": time.time() - t_all}))
    except Exception as ex:
        try:
            log("发生未预期的错误：")
            log(traceback.format_exc())
        finally:
            q.put(("fatal", f"{type(ex).__name__}: {ex}"))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk, autostart: bool = False):
        self.root = root
        self.proc: mp.Process | None = None
        self.q: mp.Queue | None = None
        self.outdir: str | None = None
        self.running = False
        self.total = 0
        self._pump_id = None
        self.cfg = load_config()

        root.title(APP_TITLE)
        # 窗口尺寸：默认 980x760；可用 --size 宽x高 覆盖
        # （小屏笔记本 / 截图时有用）。自动限制在屏幕内。
        # ★ 建完控件后还会按「内容实际需要的高度」再调一次 ——
        #   高 DPI（比如 133% 缩放）下同样的界面要高出三分之一，
        #   固定高度会把「开始转换」按钮和日志挤出屏幕外。
        size = "980x760"
        for i, a in enumerate(sys.argv):
            if a == "--size" and i + 1 < len(sys.argv):
                size = sys.argv[i + 1]
            elif a.startswith("--size="):
                size = a.split("=", 1)[1]
        try:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            w, h = (int(x) for x in size.lower().split("x"))
            w = max(780, min(w, sw - 40))
            h = max(480, min(h, sh - 80))
        except Exception:
            sw, sh, w, h = 1280, 800, 980, 760
        root.geometry(f"{w}x{h}")
        root.minsize(780, 520)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        # ---- 标题 ----
        head = ttk.Frame(root, padding=(14, 12, 14, 6))
        head.pack(fill="x")
        ttk.Label(head, text=APP_TITLE,
                  font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(head,
                  text="选一个模组文件夹 → 点「开始转换」。"
                       "原文件不会被修改，结果输出到该文件夹的 converted 子目录。",
                  foreground="#555").pack(anchor="w", pady=(3, 0))
        ttk.Label(head, text=NOTICE, foreground="#888",
                  font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(2, 0))

        # ---- 路径选择 ----
        box = ttk.LabelFrame(root, text=" 1. 选择模组文件夹 ", padding=10)
        box.pack(fill="x", padx=14, pady=(8, 4))

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.path_var = tk.StringVar(value="（尚未选择）")
        ent = ttk.Entry(row, textvariable=self.path_var, state="readonly")
        ent.pack(side="left", fill="x", expand=True, ipady=3)
        self.btn_pick = ttk.Button(row, text="选择文件夹…", command=self.pick_dir)
        self.btn_pick.pack(side="left", padx=(8, 0))

        self.outdir_var = tk.StringVar(value="输出目录：—")
        ttk.Label(box, textvariable=self.outdir_var,
                  foreground="#0a5").pack(anchor="w", pady=(6, 0))

        # ---- 选项 ----
        opt = ttk.LabelFrame(root, text=" 2. 选项 ", padding=10)
        opt.pack(fill="x", padx=14, pady=4)

        self.verify_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, variable=self.verify_var,
                        text="用官方 UnrealPak 校验结果（推荐，较慢耗时但更放心）"
                        ).grid(row=0, column=0, sticky="w")

        self.strip_all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, variable=self.strip_all_var,
                        text="激进模式：只要官方已有就剥离（包更小，但可能改坏贴图类 mod）"
                        ).grid(row=1, column=0, sticky="w")

        self.match_name_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, variable=self.match_name_var,
            text="同名也剥：文件名相同就当官方已有\n"
                 "（默认关闭；同名≠同路径，开了有把模组贴图误剥的风险）"
        ).grid(row=2, column=0, sticky="w")

        self.strip_modified_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, variable=self.strip_modified_var,
            text="连改过的也剥：把模组自己改过的蓝图/数据表也删掉\n"
                 "（默认关闭；开了多半会变成「能进游戏但什么都不发生」，"
                 "只在游戏一进就崩时才勾）"
        ).grid(row=3, column=0, sticky="w")

        self.health_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, variable=self.health_var,
            text="体检模式：只诊断「为什么这个模组装了没效果」，不生成任何文件"
        ).grid(row=4, column=0, sticky="w")

        self.assess_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt, variable=self.assess_var,
            text="先诊断再转换（推荐）：先判断这个模组能不能改、值不值得改，"
                 "只转该转的"
        ).grid(row=5, column=0, sticky="w")

        self.raw_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, variable=self.raw_var,
            text="改压缩方式为不压缩：用于模组压缩方式和游戏不一致导致卡加载"
                 "（包会变大）"
        ).grid(row=7, column=0, sticky="w")

        self.fixnames_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, variable=self.fixnames_var,
            text="自动改名修复：补 _P 后缀 / 调 pakchunk 加载顺序，"
                 "另出一份改好名的（原文件不动）"
        ).grid(row=6, column=0, sticky="w")

        self.genman_var = tk.BooleanVar(value=False)
        chk = ttk.Checkbutton(
            opt, variable=self.genman_var,
            text="先生成官方资产清单（首次使用必做；游戏更新后重新生成）")
        chk.grid(row=8, column=0, sticky="w")
        self.game_var = tk.StringVar(value="游戏目录识别中…")
        ttk.Label(opt, textvariable=self.game_var,
                  foreground="#666").grid(row=9, column=0, sticky="w", pady=(4, 0))

        # ---- 按钮 ----
        bar = ttk.Frame(root, padding=(14, 6))
        bar.pack(fill="x")
        self.btn_start = ttk.Button(bar, text="开始转换", command=self.start,
                                    state="disabled")
        self.btn_start.pack(side="left")
        self.btn_open = ttk.Button(bar, text="打开输出文件夹",
                                   command=self.open_outdir, state="disabled")
        self.btn_open.pack(side="left", padx=(8, 0))
        self.btn_cancel = ttk.Button(bar, text="中止", command=self.cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var,
                  foreground="#036").pack(side="left", padx=(16, 0))

        # ---- 进度条 ----
        pf = ttk.Frame(root, padding=(14, 0))
        pf.pack(fill="x")
        self.pb = ttk.Progressbar(pf, mode="determinate", maximum=100)
        self.pb.pack(fill="x")

        # ---- 日志 ----
        lf = ttk.LabelFrame(root, text=" 3. 日志 ", padding=(8, 6))
        lf.pack(fill="both", expand=True, padx=14, pady=(8, 12))
        self.log = tk.Text(lf, wrap="none", height=10,
                           font=("Consolas", 9), background="#fbfbfb")
        ys = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        xs = ttk.Scrollbar(lf, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)

        # 让日志有颜色
        self.log.tag_configure("err", foreground="#b00020")
        self.log.tag_configure("ok", foreground="#0a7a30")
        self.log.tag_configure("warn", foreground="#b36b00")
        self.log.tag_configure("head", foreground="#003a8c",
                               font=("Consolas", 9, "bold"))

        self.say("欢迎使用。请先点「选择文件夹」选择你的模组目录。")

        # ---- 尺寸再定一次：内容需要多高就给多高（屏幕装不下才压缩） ----
        #   高度按内容算（高 DPI 下同样的界面要高三分之一），
        #   宽度不跟着算 —— 日志框的「默认宽度」会把窗口撑宽，而不是内容需要。
        try:
            root.update_idletasks()
            h2 = max(h, root.winfo_reqheight())
            h2 = max(480, min(h2, sh - 80))
            if h2 != h:
                root.geometry(f"{w}x{h2}")
        except Exception:
            pass          # 尺寸算不出来也不该影响使用

        # ---- 自动找游戏 + 判断清单新旧（关键：游戏一更新，清单就过期）----
        m, how = find_manifest()
        self.game_paks = self._detect_game()
        self.game_var.set(f"游戏目录：{self.game_paks or '未自动找到（可手动生成清单）'}")
        self._report_manifest_state(m, how)
        up = find_unrealpak()
        if up:
            self.say(f"官方 UnrealPak：{up}")
        else:
            self.say("⚠ 没找到 UnrealPak.exe，校验功能会跳过"
                     "（转换本身仍可进行）。", "warn")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # ---- 恢复上次选择 ----
        last = self.cfg.get("last_dir")
        if last and os.path.isdir(last):
            self._apply_dir(last)
            self.say(f"（已记住上次的目录：{last}）")
        if "verify" in self.cfg:
            self.verify_var.set(bool(self.cfg["verify"]))
        if "strip_all" in self.cfg:
            self.strip_all_var.set(bool(self.cfg["strip_all"]))
        if "match_name" in self.cfg:
            self.match_name_var.set(bool(self.cfg["match_name"]))
        if "strip_modified" in self.cfg:
            self.strip_modified_var.set(bool(self.cfg["strip_modified"]))
        if "health_only" in self.cfg:
            self.health_var.set(bool(self.cfg["health_only"]))
        if "repack_raw" in self.cfg:
            self.raw_var.set(bool(self.cfg["repack_raw"]))
        if "assess_first" in self.cfg:
            self.assess_var.set(bool(self.cfg["assess_first"]))
        if "fix_names" in self.cfg:
            self.fixnames_var.set(bool(self.cfg["fix_names"]))
        if autostart and last and os.path.isdir(last):
            self.root.after(400, self.start)

    # ---- 统一的"选中目录"处理 ----
    def _apply_dir(self, d: str) -> None:
        d = os.path.normpath(d)
        self.path_var.set(d)
        self.outdir = os.path.join(d, "converted")
        self.outdir_var.set(f"输出目录：{self.outdir}")
        self.btn_start.configure(state="normal")
        self.btn_open.configure(state="normal")

    # ---- 自动找游戏 ----
    def _detect_game(self) -> str | None:
        """自动定位游戏 Paks 目录，并显示信息来源。"""
        hints = []
        saved = self.cfg.get("game_paks")
        if saved:
            hints.append(saved)
        try:
            import ronconvert as RC
            found = RC.find_game_paks(hints)
        except Exception as ex:
            self.say(f"⚠ 自动查找游戏目录失败：{ex}", "warn")
            return None
        if found:
            self.say(f"游戏安装    ：{found}", "ok")
            if saved != found:
                self.cfg["game_paks"] = found
                save_config(self.cfg)
        else:
            self.say("⚠ 没自动找到游戏安装目录（Ready Or Not\\Content\\Paks）。", "warn")
            self.say("   如果游戏装在别处，可以手动生成清单：")
            self.say("     python tools\\ronconvert.py \"任意目录\" "
                     "--game-paks \"<你的游戏Paks目录>\" --dry-run")
            self.say(f"   然后把生成的 json 改名成 {MANIFEST_NAME} 放到本程序旁边。")
        return found

    # ---- 报告清单状态 ----
    def _report_manifest_state(self, manifest: str | None, how: str) -> None:
        """告诉用户：有没有清单、是否过期、怎么生成。"""
        if not manifest:
            self.say("")
            self.say("还没有官方资产清单（首次使用需要生成一次）。", "warn")
            self.say("   本程序【不附带】任何游戏数据。清单要用你自己安装的游戏在本机生成，")
            self.say("   只写在你电脑上，不会上传到任何地方。", "warn")
            if self.game_paks:
                self.say("   已找到游戏，勾选「先生成官方资产清单」再点开始即可。", "ok")
            else:
                self.say("   没找到游戏安装目录，请手动生成（见上面的命令）。", "warn")
            return

        self.say(f"官方资产清单：{how}  ({manifest})")
        try:
            import ronconvert as RC
            st = RC.check_manifest_freshness(manifest, self.game_paks)
        except Exception:
            return
        status = st.get("status")
        if status == "stale":
            self.say(f"   ⚠ 清单可能过期：{st['reason']}", "warn")
            self.say("      游戏更新会新增内容。勾选「先生成官方资产清单」"
                     "重新生成一次即可。", "warn")
        elif status == "ok":
            self.say(f"   {st['reason']}")

    # ---- 日志 ----
    def say(self, text: str, tag: str | None = None) -> None:
        self.log.insert("end", str(text) + "\n", tag or ())
        self.log.see("end")

    def say_block(self, text: str, tag: str | None = None) -> None:
        for line in str(text).splitlines():
            self.say(line, tag)

    # ---- 选择目录 ----
    def pick_dir(self) -> None:
        d = filedialog.askdirectory(title="选择 Ready or Not 模组文件夹")
        if not d:
            return
        self._apply_dir(d)
        # 预先检查
        n = 0
        try:
            for base, dirs, files in os.walk(d):
                if os.path.basename(base).lower() in ("converted", "转换后"):
                    continue
                n += sum(1 for f in files if f.lower().endswith(".pak"))
        except Exception as ex:
            self.say(f"⚠ 无法读取该目录：{ex}", "warn")
        self.say(f"已选择：{d}")
        if n:
            self.say(f"   发现 {n} 个 .pak", "ok")
        else:
            self.say("   ⚠ 这个目录（含子目录）里没有 .pak 文件", "warn")

    # ---- 开始 ----
    def start(self) -> None:
        if self.running:
            return
        d = self.path_var.get()
        if not d or d.startswith("（"):
            messagebox.showwarning(APP_TITLE, "请先选择模组文件夹。")
            return
        if not os.path.isdir(d):
            messagebox.showerror(APP_TITLE, f"目录不存在：\n{d}")
            return

        manifest, how = find_manifest()
        game_paks = getattr(self, "game_paks", None) if self.genman_var.get() else None

        # 首次使用：还没有清单 -> 引导生成（不假装能转换）
        if not manifest:
            if not game_paks:
                messagebox.showerror(
                    APP_TITLE,
                    "还没有官方资产清单，无法判断哪些内容官方已有。\n\n"
                    "本程序不附带任何游戏数据，清单需要用你自己安装的游戏生成。\n\n"
                    "没找到游戏安装目录。请手动生成一次：\n"
                    "  python tools\\ronconvert.py \"任意目录\" "
                    "--game-paks \"<游戏Paks目录>\" --dry-run\n"
                    f"然后把生成的 json 改名成 {MANIFEST_NAME} 放到本程序旁边。")
                return
            if not messagebox.askyesno(
                    APP_TITLE,
                    "首次使用需要先生成官方资产清单。\n\n"
                    f"会用你本机的游戏生成：\n{game_paks}\n\n"
                    "清单只写在你自己的电脑上（程序同目录的 "
                    f"{MANIFEST_NAME}），\n"
                    "不会上传、不包含任何游戏资产文件。\n\n"
                    "游戏本体 pak 约 44 GB，但只读索引，几秒即可。现在开始吗？"):
                return
            self.genman_var.set(True)      # 触发 worker 里的生成
            self.say("首次使用：先生成官方资产清单。", "head")

        if self.genman_var.get() and not game_paks:
            messagebox.showwarning(
                APP_TITLE,
                "勾选了「先生成官方资产清单」，但没找到游戏安装目录。\n\n"
                "会改用现有清单继续转换（判定精度略低）。")
        if not manifest and not self.genman_var.get():
            messagebox.showerror(
                APP_TITLE,
                "找不到官方资产清单，无法判断哪些内容官方已有。\n\n"
                "请勾选「先生成官方资产清单」后重试。")
            return

        self.outdir = os.path.join(d, "converted")
        self.outdir_var.set(f"输出目录：{self.outdir}")
        os.makedirs(self.outdir, exist_ok=True)

        # 记住本次选择
        self.cfg.update({"last_dir": d, "verify": bool(self.verify_var.get()),
                         "strip_all": bool(self.strip_all_var.get()),
                         "match_name": bool(self.match_name_var.get()),
                         "strip_modified": bool(self.strip_modified_var.get()),
                         "health_only": bool(self.health_var.get()),
                         "repack_raw": bool(self.raw_var.get()),
                         "assess_first": bool(self.assess_var.get()),
                         "fix_names": bool(self.fixnames_var.get())})
        save_config(self.cfg)

        self.running = True
        self.total = 0
        self._health_mode = bool(self.health_var.get())
        self.pb.configure(value=0, maximum=100)
        self.btn_start.configure(state="disabled")
        self.btn_pick.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.status_var.set("正在转换…")
        self.log.delete("1.0", "end")
        self.say("========== 开始 ==========", "head")

        try:
            ctx = mp.get_context("spawn")
            self.q = ctx.Queue()
            self.proc = ctx.Process(
                target=_worker,
                args=(d, self.outdir, manifest, bool(self.verify_var.get()),
                      bool(self.strip_all_var.get()),
                      bool(self.match_name_var.get()),
                      bool(self.strip_modified_var.get()),
                      bool(self.health_var.get()),
                      bool(self.raw_var.get()),
                      bool(self.assess_var.get()),
                      bool(self.fixnames_var.get()), game_paks, self.q),
                daemon=True)
            self.proc.start()
        except Exception as ex:
            self._finish(ok=False,
                         msg=f"无法启动转换进程：{type(ex).__name__}: {ex}")
            self.say_block(traceback.format_exc(), "err")
            return
        self._pump()

    # ---- 消息泵 ----
    def _pump(self) -> None:
        self._pump_id = None
        if not self.running or self.q is None:
            return
        drained = 0
        while drained < 400:
            try:
                kind, payload = self.q.get_nowait()
            except queue_mod.Empty:
                break
            except Exception:
                break
            drained += 1
            self._handle(kind, payload)
        # 进程是否结束
        if self.proc is not None and not self.proc.is_alive() and drained == 0:
            # 再给队列一点时间收尾
            time.sleep(0.05)
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    self._handle(kind, payload)
            except Exception:
                pass
            if self.running:
                self._finish(
                    ok=None,
                    msg="转换进程已结束（未收到完成信号，可能被中止）")
            return
        if self.running:
            self._pump_id = self.root.after(80, self._pump)

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            t = None
            s = str(payload)
            if s.strip().startswith("✘") or "Traceback" in s or "Error" in s:
                t = "err"
            elif s.strip().startswith("✔") or "通过" in s:
                t = "ok"
            elif s.strip().startswith("⚠"):
                t = "warn"
            elif set(s.strip()) == {"="} or s.strip().startswith("汇总"):
                t = "head"
            self.say(s, t)
        elif kind == "prog":
            done, total, name = payload
            self.total = total
            pct = (done / total * 100) if total else 0
            self.pb.configure(value=pct)
            self.status_var.set(f"正在处理 {done+1 if done < total else total}"
                                f"/{total}：{name}")
        elif kind == "mprog":
            done, total, note = payload
            self.pb.configure(value=(done / total * 100) if total else 0)
            self.status_var.set(f"正在生成官方清单 {done}/{total}：{note}")
        elif kind == "result":
            pass
        elif kind == "fatal":
            self.say("")
            self.say_block(payload, "err")
            self._finish(ok=False, msg="转换失败")
        elif kind == "done":
            self._on_done(payload)

    def _on_done(self, info: dict) -> None:
        self.pb.configure(value=100)
        b = info.get("buckets", {})
        self.say("")
        self.say("========== 完成 ==========", "head")
        self.say(f"共处理 {info.get('total', 0)} 个 pak，"
                 f"耗时 {info.get('seconds', 0):.1f} 秒")
        for k, v in b.items():
            self.say(f"   {k}：{v}")
        self.say(f"输出目录：{info.get('outdir')}", "ok")

        self.outdir = info.get("outdir") or self.outdir
        good = sum(v for k, v in b.items() if k != "无法处理")
        n_fix = int(info.get("renamed", 0) or 0)
        fix_line = (f"\n  · {n_fix} 个做了「自动改名修复」，改好名的文件在输出目录里\n"
                    f"    （原文件没动）" if n_fix else "")
        if getattr(self, "_health_mode", False):
            n_err = sum(1 for s in info.get("summary", []) if s.get("problems"))
            msg = (f"体检完成。\n\n"
                   f"共检查 {info.get('total', 0)} 个 pak，"
                   f"{n_err} 个发现问题。\n\n"
                   f"详细结论在日志里（✘ 开头的就是问题所在）。\n"
                   f"没有修改、也没有生成任何文件。")
            self._finish(ok=True, msg=None)
            messagebox.showinfo(APP_TITLE, msg)
            return
        skipped = b.get("诊断后跳过", 0)
        if skipped:
            msg = (f"诊断 + 转换完成。\n\n"
                   f"共 {info.get('total', 0)} 个 pak：\n"
                   f"  · {good - skipped} 个转换了，产物在：\n{self.outdir}\n"
                   f"  · {skipped} 个诊断判定【不需要转换】，已跳过、"
                   f"没有生成文件\n"
                   f"    （它们原样用就行，转了反而可能变糟）\n"
                   f"{fix_line}\n"
                   f"注意：原文件没有被修改。")
            if b.get("无法处理"):
                msg += f"\n\n有 {b['无法处理']} 个 pak 无法处理，请看日志。"
            self._finish(ok=True, msg=None)
            messagebox.showinfo(APP_TITLE, msg)
            return
        msg = (f"转换完成。\n\n"
               f"共 {info.get('total', 0)} 个 pak，成功处理 {good} 个。\n"
               f"输出目录：\n{self.outdir}\n{fix_line}\n\n"
               f"注意：原文件没有被修改。确认没问题后再用 converted 里的文件替换。")
        if b.get("无法处理"):
            msg += f"\n\n有 {b['无法处理']} 个 pak 无法处理，请看日志。"
        self._finish(ok=True, msg=None)
        messagebox.showinfo(APP_TITLE, msg)

    def _finish(self, ok: bool | None, msg: str | None) -> None:
        self.running = False
        if self._pump_id:
            try:
                self.root.after_cancel(self._pump_id)
            except Exception:
                pass
            self._pump_id = None
        self.btn_start.configure(state="normal")
        self.btn_pick.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        if self.outdir and os.path.isdir(self.outdir):
            self.btn_open.configure(state="normal")
        if ok is True:
            self.status_var.set("完成")
        elif ok is False:
            self.status_var.set("失败")
        else:
            self.status_var.set("已结束")
        if msg:
            self.status_var.set("失败" if ok is False else "已结束")
            self.say("")
            self.say_block(msg, "err" if ok is False else "warn")
            if ok is False:
                messagebox.showerror(APP_TITLE, msg)
            else:
                messagebox.showwarning(APP_TITLE, msg)
        # 清理进程
        if self.proc is not None:
            try:
                if self.proc.is_alive():
                    self.proc.terminate()
            except Exception:
                pass
            self.proc = None

    def cancel(self) -> None:
        if not self.running:
            return
        if not messagebox.askyesno(APP_TITLE, "确定要中止转换吗？\n"
                                             "已写出的文件会保留。"):
            return
        self.say("用户中止。", "warn")
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self._finish(ok=None, msg="已被用户中止。已写出的文件保留在输出目录。")

    # ---- 打开输出目录 ----
    def open_outdir(self) -> None:
        d = self.outdir
        if not d or not os.path.isdir(d):
            messagebox.showinfo(APP_TITLE, "还没有输出目录。")
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(d)          # noqa: S606  (Windows)
            else:
                subprocess.Popen(["explorer", d])
        except Exception as ex:
            messagebox.showerror(APP_TITLE, f"无法打开目录：{ex}\n\n{d}")

    def on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(APP_TITLE, "转换还在进行，确定要退出吗？"):
                return
            if self.proc is not None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        self.root.destroy()


def _selftest(moddir: str, verify: bool = False) -> int:
    """命令行自检：不建窗口，验证打包后的 exe 能跑通子进程转换流程。

    用法（给打包好的 exe）：
        RoN模组转换器.exe --selftest "<模组目录>"
    """
    ok = True
    _out("=" * 66)
    _out(f"{APP_TITLE} —— 打包自检")
    _out(f"frozen      : {getattr(sys, 'frozen', False)}")
    _out(f"可执行文件  : {sys.executable}")
    _out(f"解包目录    : {_BUNDLE}")
    _out(f"程序目录    : {_BASE}")
    _out(f"sys.path    : {sys.path[:6]}")
    _out("=" * 66)

    # 1) 关键模块能否导入
    for name in ("pakfmt", "ronconvert"):
        try:
            m = __import__(name)
            _out(f"[OK]   import {name}  ({getattr(m, '__file__', '?')})")
        except Exception as ex:
            _out(f"[FAIL] import {name}: {type(ex).__name__}: {ex}")
            ok = False

    # 2) 清单
    man, how = find_manifest()
    _out(f"[{'OK' if man else 'FAIL'}] 官方清单: {how} -> {man}")
    if not man:
        ok = False

    # 3) UnrealPak
    up = find_unrealpak()
    _out(f"[{'OK' if up else '--'}] UnrealPak: {up or '未找到（校验将跳过）'}")

    if not ok:
        _out("\n自检失败：基础依赖不完整")
        return 1

    # 4) 真正跑一次子进程转换
    _out("\n--- 子进程转换测试 ---")
    os.makedirs(moddir, exist_ok=True)
    outdir = os.path.join(moddir, "converted")
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_worker,
                       args=(moddir, outdir, man, verify, False, False, False,
                             False, False, False, False, None, q),
                       daemon=True)
    proc.start()

    done = fatal = None
    nlog = nres = 0
    deadline = time.time() + 1800
    while time.time() < deadline:
        try:
            kind, payload = q.get(timeout=1.0)
        except queue_mod.Empty:
            if not proc.is_alive():
                break
            continue
        if kind == "log":
            nlog += 1
            _out("   " + str(payload))
        elif kind == "result":
            nres += 1
        elif kind == "done":
            done = payload
        elif kind == "fatal":
            fatal = payload
    proc.join(timeout=15)

    _out("\n--- 自检结果 ---")
    _out(f"   日志 {nlog} 行 / 结果 {nres} 个 / done={'有' if done else '无'} / "
          f"fatal={fatal}")
    good = (proc.exitcode == 0 and done is not None and not fatal)
    _out(f"   {'[OK]   子进程转换流程正常' if good else '[FAIL] 子进程流程有问题'}")
    _out(f"\n自检{'通过' if good else '失败'}")
    return 0 if good else 1


def main() -> int:
    mp.freeze_support()          # PyInstaller 打包后必须
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        return _selftest(os.path.abspath(sys.argv[2]),
                         verify="--verify" in sys.argv)
    try:
        root = tk.Tk()
    except Exception as ex:
        # 极端情况：没有图形环境
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, f"无法创建窗口：{ex}", APP_TITLE, 0x10)
        except Exception:
            print(f"无法创建窗口：{ex}")
        return 1
    App(root, autostart=("--autostart" in sys.argv))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
