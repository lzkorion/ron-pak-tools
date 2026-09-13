#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑全部测试。

    python tests/run_all.py

退出码 0 表示全通过。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

TESTS = [
    ("写入器端到端 (pakfmt)", "test_writer.py"),
    ("转换全分支 (ronconvert)", "test_convert.py"),
    ("检测 / 清单新鲜度", "test_detect.py"),
    ("GUI 冒烟 / 配置记忆", "test_gui_smoke.py"),
    ("GUI 端到端", "test_gui_e2e.py"),
]


def main():
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["RONPAK_NO_CONFIG"] = "1"      # 测试不要写配置文件

    results = []
    for label, fname in TESTS:
        path = os.path.join(HERE, fname)
        if not os.path.isfile(path):
            results.append((label, False, "文件不存在"))
            print(f"[SKIP] {label}")
            continue
        t0 = time.time()
        p = subprocess.run([sys.executable, "-B", path], env=env)
        ok = p.returncode == 0
        results.append((label, ok, f"{time.time()-t0:.1f}s"))
        print(f"[{'PASS' if ok else 'FAIL'}] {label}  ({time.time()-t0:.1f}s)\n")

    good = sum(1 for _l, ok, _t in results if ok)
    print("=" * 60)
    print(f"总计 {good}/{len(results)} 通过")
    for label, ok, note in results:
        print(f"   {'PASS' if ok else 'FAIL'}  {label}  {note}")
    return 0 if good == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
