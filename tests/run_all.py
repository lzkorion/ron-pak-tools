#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑测试。

    python tests/run_all.py              # 全部（含 GUI）
    python tests/run_all.py --core-only  # 只跑不需要图形界面的（CI 用）

分组的原因：GUI 测试要真的建窗口，CI 机器上不一定有可用桌面。
核心测试只依赖标准库，任何环境都能跑。

退出码 0 表示全通过。
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# (显示名, 文件名, 是否需要图形界面)
TESTS = [
    ("写入器端到端 (pakfmt)", "test_writer.py", False),
    ("转换全分支 (ronconvert)", "test_convert.py", False),
    ("资产整组剥离 (.bak 变体)", "test_grouping.py", False),
    ("安全底线 (同名≠同路径)", "test_safety.py", False),
    ("检测 / 清单新鲜度", "test_detect.py", False),
    ("GUI 冒烟 / 配置记忆", "test_gui_smoke.py", True),
    ("GUI 端到端", "test_gui_e2e.py", True),
]


def has_display() -> bool:
    """能不能建 Tk 窗口（CI 上可能不行）。"""
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        r.destroy()
        return True
    except Exception:
        return False


def main() -> int:
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--core-only", action="store_true",
                    help="跳过需要图形界面的测试（CI 用）")
    args = ap.parse_args()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["RONPAK_NO_CONFIG"] = "1"      # 测试不要写配置文件

    gui_ok = None
    if not args.core_only:
        gui_ok = has_display()
        if not gui_ok:
            print("⚠ 这个环境建不了窗口，自动跳过 GUI 测试（等同 --core-only）\n")

    results = []
    skipped = []
    for label, fname, needs_gui in TESTS:
        if needs_gui and (args.core_only or not gui_ok):
            skipped.append(label)
            print(f"[SKIP] {label}（需要图形界面）\n")
            continue
        path = os.path.join(HERE, fname)
        if not os.path.isfile(path):
            results.append((label, False, "文件不存在"))
            print(f"[SKIP] {label}（文件不存在）")
            continue
        t0 = time.time()
        p = subprocess.run([sys.executable, "-B", path], env=env)
        ok = p.returncode == 0
        results.append((label, ok, f"{time.time()-t0:.1f}s"))
        print(f"[{'PASS' if ok else 'FAIL'}] {label}  ({time.time()-t0:.1f}s)\n")

    good = sum(1 for _l, ok, _t in results if ok)
    print("=" * 60)
    print(f"总计 {good}/{len(results)} 通过"
          + (f"，跳过 {len(skipped)}" if skipped else ""))
    for label, ok, note in results:
        print(f"   {'PASS' if ok else 'FAIL'}  {label}  {note}")
    for label in skipped:
        print(f"   SKIP  {label}")
    return 0 if good == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
