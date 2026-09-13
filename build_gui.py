#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 PyInstaller 打包 GUI（从 Python 调用，避免 shell 把非 ASCII 路径弄乱）。

用法：
    python build_gui.py

产物：dist/RoNPakTools.exe

★ 本脚本【刻意不打包】任何游戏派生数据（资产清单 / 样本 pak）。
  manifest 由用户首次运行时用自己的游戏在本机生成，见 ronconvert_gui.py。
  也不要打包 UnrealPak.exe —— 那是 Epic 的 Engine Tool，
  UE EULA Section 1A 禁止向最终用户分发。
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "RoNPakTools"
ENTRY = "ronconvert_gui.py"
# 只放代码，不放数据
DATAS = [
    (os.path.join("tools", "pakfmt.py"), "."),
    (os.path.join("tools", "ronconvert.py"), "."),
    (os.path.join("tools", "ronhealth.py"), "."),
]
# 明确禁止被打包进发行物的东西（构建时校验，防止误打包）
FORBIDDEN = [
    "game_manifest.json", "game_manifest_full.json", "manifest_local.json",
    "game_assets_uasset.txt", "mod_assets.json", "ron_mods_report.json",
    "visceral_paths.json", "mod_assets_report.txt",
    "UnrealPak.exe", "oo2core.dll",
]

VERSION_INFO = """
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 5, 0, 0),
    prodvers=(1, 5, 0, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', ''),
        StringStruct('FileDescription', 'RoN Pak Tools'),
        StringStruct('FileVersion', '1.5.0.0'),
        StringStruct('InternalName', 'ronconvert_gui'),
        StringStruct('LegalCopyright', ''),
        StringStruct('OriginalFilename', 'RoNPakTools.exe'),
        StringStruct('ProductName', 'RoN Pak Tools'),
        StringStruct('ProductVersion', '1.5.0.0')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def _write_version_file(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(VERSION_INFO)


def main() -> int:
    os.chdir(HERE)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--windowed",                    # 不弹黑窗
        "--name", NAME,
        "--distpath", "dist",
        "--workpath", "build",
        "--specpath", "build",
        # 注意：这里【不加】--hidden-import pakfmt / ronconvert。
        # 它们在 ronconvert_gui 里是“运行时按 sys.path 导入”的（见模块顶层的路径设置），
        # 打包期分析器找不到、会报 "Hidden import not found" 的噪音警告；
        # 但两个 .py 已由下面的 --add-data 放进包里，运行时 import 正常
        # （已用 exe --selftest 实测：两个模块都能 import 成功）。
    ]
    for src, dst in DATAS:
        if not os.path.isfile(src):
            print(f"✘ 缺少文件：{src}")
            return 1
        # 必须用绝对路径：--specpath 在 build\ 下，相对路径会被按 spec 目录解析
        cmd += ["--add-data", f"{os.path.abspath(src)}{os.pathsep}{dst}"]

    vf = os.path.join(HERE, "build", "version_info.txt")
    os.makedirs(os.path.dirname(vf), exist_ok=True)
    _write_version_file(vf)
    cmd += ["--version-file", vf]
    cmd.append(ENTRY)

    # 构建前校验：确认发行物里不会混入游戏数据 / Epic 二进制
    print("合规检查（发行物不得包含游戏派生数据或 Epic 工具）：")
    bad = []
    for name in FORBIDDEN:
        for d in (HERE, os.path.join(HERE, "tools")):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                bad.append(p)
    if bad:
        print("✘ 发现不应打包的文件，已中止：")
        for b in bad:
            print("     " + b)
        print("  请把这些文件移出项目目录（或用 .gitignore 排除）后重试。")
        return 1
    print("   ✔ 未发现游戏清单 / 样本 pak / UnrealPak.exe")

    print("\nPyInstaller 参数：")
    for c in cmd:
        print("   " + c)
    print("\n开始打包（首次约 1-3 分钟）...\n", flush=True)

    # env 里 PYTHONIOENCODING 保证子进程中文输出不乱
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(cmd, env=env)
    if p.returncode != 0:
        print(f"\n✘ 打包失败，退出码 {p.returncode}")
        return p.returncode

    exe = os.path.join(HERE, "dist", NAME + ".exe")
    if not os.path.isfile(exe):
        print(f"\n✘ 打包结束但没找到产物：{exe}")
        return 1
    size = os.path.getsize(exe) / 1024 / 1024
    print(f"\n✔ 打包成功")
    print(f"   产物：{exe}")
    print(f"   大小：{size:.1f} MB")
    return 0


if __name__ == "__main__":
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    sys.exit(main())
