#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 真·端到端测试：真窗口跑完整流程，然后读日志控件内容验证。

全部使用合成 pak（tests/fixtures.py），不依赖任何真实模组样本。
"""
import os
import shutil
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import ronconvert_gui as G
import fixtures as FX

WORK = os.path.join(HERE, "_work", "gui_e2e")
fails = []


def check(cond, label):
    print(("  [OK]   " if cond else "  [FAIL] ") + label)
    if not cond:
        fails.append(label)


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    # 合成模组：冲突型 + 贴图替换 + 独有内容
    moddir = os.path.join(WORK, "mods")
    FX.make_pak(os.path.join(moddir, "SyntheticMod_P.pak"))
    # 一个坏文件，验证异常分支
    with open(os.path.join(moddir, "Broken_P.pak"), "wb") as f:
        f.write(b"not a pak file" * 50)

    # 合成「官方清单」放到程序旁边（GUI 会自动找到）
    man = G.manifest_path()
    FX.write_manifest(man, FX.official_names())

    # 配置会影响初始状态（比如记住上次目录会让按钮变可用）：
    # 测试期间先移走，结束时恢复，避免与其它测试互相干扰
    cfg_path = G._config_path()
    saved_cfg = None
    if os.path.isfile(cfg_path):
        saved_cfg = open(cfg_path, encoding="utf-8").read()
        os.remove(cfg_path)

    root = tk.Tk()
    root.withdraw()
    app = G.App(root)

    # 拦截模态弹窗，避免测试卡住
    box = {}
    G.messagebox.showinfo = lambda *a, **k: box.setdefault("info", a)
    G.messagebox.showerror = lambda *a, **k: box.setdefault("error", a)
    G.messagebox.showwarning = lambda *a, **k: box.setdefault("warn", a)
    G.messagebox.askyesno = lambda *a, **k: True

    try:
        print("1) 启动状态")
        check(not app.running, "初始未运行")
        check(str(app.btn_start["state"]) == "disabled",
              "未选目录时「开始转换」禁用")

        print("\n2) 选择目录 → 开始")
        app._apply_dir(moddir)
        app.verify_var.set(False)      # 测试求快
        app.genman_var.set(False)      # 用已有的合成清单
        check(app.path_var.get() == os.path.normpath(moddir),
              f"路径已显示：{app.path_var.get()}")
        check(app.outdir.endswith("converted"), "输出目录为 converted")

        app.start()
        check(app.running, "已进入运行状态")
        check(str(app.btn_start["state"]) == "disabled", "运行中禁用「开始转换」")
        check(str(app.btn_cancel["state"]) == "normal", "运行中可「中止」")

        deadline = time.time() + 300
        while app.running and time.time() < deadline:
            root.update()
            time.sleep(0.05)
        root.update()

        print("\n3) 结束状态")
        check(not app.running, "流程已结束（未卡死）")
        check("info" in box, "完成时弹出提示框")
        check(app.status_var.get() == "完成",
              f"状态为「完成」（实际 {app.status_var.get()}）")
        check(str(app.btn_open["state"]) == "normal", "「打开输出文件夹」已启用")

        log = app.log.get("1.0", "end")
        print("\n  --- 日志尾部 ---")
        for line in log.strip().splitlines()[-16:]:
            print("   | " + line)

        print("\n4) 日志内容断言")
        check("SyntheticMod_P.pak" in log, "日志含模组标题")
        check("结论：" in log, "日志给出结论")
        check("Broken_P.pak" in log and "无法处理" in log, "坏文件判为「无法处理」")
        check("汇总" in log, "含汇总")
        check("输出目录" in log, "含输出目录")

        print("\n5) 产物断言")
        out = os.path.join(moddir, "converted")
        check(os.path.isdir(out), "输出目录已创建")
        produced = sorted(os.listdir(out)) if os.path.isdir(out) else []
        check("SyntheticMod_P.pak" in produced, f"产物已生成（{produced}）")
        check("Broken_P.pak" not in produced, "坏文件没有产出垃圾 pak")

        import pakfmt as P
        pk = P.read_pak(os.path.join(out, "SyntheticMod_P.pak"))
        paths = set(pk.all_paths())
        # 冲突型：应被剥离
        for rel in FX.CONFLICT_ASSETS:
            check(rel not in paths, f"冲突型已剥离: {rel}")
        # 资源替换型 + 独有：必须保留
        for rel in FX.KEEP_ASSETS + FX.UNIQUE_ASSETS:
            check(rel in paths, f"应保留: {rel}")

        print("\n6) 原文件未被修改")
        check(os.path.getsize(os.path.join(moddir, "SyntheticMod_P.pak")) > 0,
              "源 pak 仍在")
    finally:
        root.destroy()
        try:
            if os.path.isfile(man):
                os.remove(man)
        except Exception:
            pass
        if saved_cfg is not None:
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(saved_cfg)

    print(f"\n=== GUI 端到端测试 {'PASS' if not fails else 'FAIL ' + str(fails)} ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
