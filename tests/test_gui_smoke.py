#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 冒烟测试：建窗口、跑各消息分支、验证按钮状态与配置记忆。

不依赖任何真实模组样本。
"""
import os
import shutil
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import ronconvert_gui as G

WORK = os.path.join(HERE, "_work", "smoke")
fails = []


def check(cond, label):
    print(("  [OK]   " if cond else "  [FAIL] ") + label)
    if not cond:
        fails.append(label)


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    # 备份并清空配置（会影响初始状态）
    cfg_path = G._config_path()
    saved = None
    if os.path.isfile(cfg_path):
        saved = open(cfg_path, encoding="utf-8").read()
        os.remove(cfg_path)

    root = tk.Tk()
    root.withdraw()
    app = G.App(root)

    print("1) 初始状态")
    check(str(app.btn_start["state"]) == "disabled", "「开始转换」禁用")
    check(str(app.btn_open["state"]) == "disabled", "「打开输出文件夹」禁用")
    check(app.verify_var.get() is True, "默认勾选官方校验")
    check(app.strip_all_var.get() is False, "默认非激进模式")
    check(app.match_name_var.get() is False,
          "默认关闭「同名也剥」（同名≠同路径，开着有误剥风险）")
    check(app.strip_modified_var.get() is False,
          "默认关闭「连改过的也剥」（开着会让模组变成能进游戏但什么都不发生）")
    check(app.health_var.get() is False, "默认非体检模式")
    check(app.fixnames_var.get() is False,
          "默认关闭「自动改名修复」（要显式勾才动文件名）")

    print("\n2) 日志消息各分支")
    app._handle("log", "普通一行")
    app._handle("log", "✔ 通过")
    app._handle("log", "✘ 出错了")
    app._handle("log", "⚠ 警告")
    app._handle("prog", (1, 4, "a.pak"))
    check(app.pb["value"] == 25.0, "进度 1/4 = 25%")
    app._handle("prog", (4, 4, "d.pak"))
    check(app.pb["value"] == 100.0, "进度 4/4 = 100%")
    app._handle("mprog", (3, 10, "pakchunk1"))
    check(abs(app.pb["value"] - 30.0) < 0.01, "清单生成进度 3/10 = 30%")
    txt = app.log.get("1.0", "end")
    check("普通一行" in txt and "出错了" in txt, "日志内容已写入")

    print("\n3) 选择目录")
    app._apply_dir(WORK)
    check(app.path_var.get() == os.path.normpath(WORK), "路径已显示")
    check(app.outdir.endswith("converted"), "输出目录为 converted")
    check(str(app.btn_start["state"]) == "normal", "选目录后可点开始")

    print("\n4) done / fatal 分支（拦截弹窗）")
    shown = {}
    orig = (G.messagebox.showinfo, G.messagebox.showerror,
            G.messagebox.showwarning, G.messagebox.askyesno)
    G.messagebox.showinfo = lambda *a, **k: shown.setdefault("info", a)
    G.messagebox.showerror = lambda *a, **k: shown.setdefault("error", a)
    G.messagebox.showwarning = lambda *a, **k: shown.setdefault("warn", a)
    G.messagebox.askyesno = lambda *a, **k: True
    try:
        app.running = True
        app.proc = None
        app._on_done({"outdir": app.outdir, "total": 2,
                      "buckets": {"已转换（剥离冲突）": 1, "无法处理": 1},
                      "summary": [], "seconds": 3.2})
        check("info" in shown, "完成后弹提示框")
        check(str(app.btn_start["state"]) == "normal", "完成后按钮恢复")
        check(str(app.btn_open["state"]) == "normal", "可打开输出目录")
        check(app.status_var.get() == "完成", "状态「完成」")

        shown.clear()
        app.running = True
        app._handle("fatal", "测试致命错误\n第二行")
        check("error" in shown, "fatal 弹错误框而非闪退")
        check(app.status_var.get() == "失败", "状态「失败」")
    finally:
        (G.messagebox.showinfo, G.messagebox.showerror,
         G.messagebox.showwarning, G.messagebox.askyesno) = orig

    print("\n5) 免责声明存在")
    alltext = []
    def walk(w):
        try:
            alltext.append(str(w.cget("text")))
        except Exception:
            pass
        for c in w.winfo_children():
            walk(c)
    walk(root)
    joined = " ".join(alltext)
    check("非官方" in joined, "界面含「非官方」声明")
    check("Epic Games" in joined, "声明里点名 Epic Games（无关联）")

    root.destroy()

    print("\n6) 配置记忆")
    no_cfg = bool(os.environ.get("RONPAK_NO_CONFIG"))
    G.save_config({"last_dir": WORK, "verify": False, "strip_all": True,
                   "match_name": True, "strip_modified": True,
                   "health_only": True, "fix_names": True})
    if no_cfg:
        # 测试环境禁用了写配置（避免污染工作目录），此时只验证读取不崩
        check(not os.path.isfile(cfg_path),
              "RONPAK_NO_CONFIG 生效：没有写配置文件")
        check(G.load_config() == {}, "无配置时安全返回空")
    else:
        check(G.load_config().get("last_dir") == WORK, "save/load 往返一致")
        r2 = tk.Tk()
        r2.withdraw()
        app2 = G.App(r2)
        check(app2.path_var.get() == os.path.normpath(WORK), "恢复上次目录")
        check(app2.verify_var.get() is False, "恢复 verify 选项")
        check(app2.strip_all_var.get() is True, "恢复 strip_all 选项")
        check(app2.match_name_var.get() is True, "恢复 match_name 选项")
        check(app2.strip_modified_var.get() is True, "恢复 strip_modified 选项")
        check(app2.health_var.get() is True, "恢复 health_only 选项")
        check(app2.fixnames_var.get() is True, "恢复 fix_names 选项")
        r2.destroy()

    # 损坏的配置不能崩
    open(cfg_path, "w", encoding="utf-8").write("{ not json")
    check(G.load_config() == {}, "配置损坏时安全返回空")
    os.remove(cfg_path)
    if saved is not None:
        open(cfg_path, "w", encoding="utf-8").write(saved)

    print(f"\n=== GUI 冒烟测试 {'PASS' if not fails else 'FAIL: ' + str(fails)} ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
