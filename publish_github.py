#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本目录发布到 GitHub（不需要 git，直接用 REST API）。

用法：
    # 1) 先去 https://github.com/settings/tokens 建一个 token（勾选 repo 权限）
    # 2) 设置环境变量后运行：
    set GITHUB_TOKEN=ghp_xxxxxxxx
    python publish_github.py --repo ron-pak-tools --user <你的用户名>

    # 只做检查、不真正上传：
    python publish_github.py --repo ron-pak-tools --user <你的用户名> --dry-run

做的事：
  1. 本地合规检查（绝不上传游戏数据 / Epic 二进制）
  2. 创建仓库（已存在则复用）
  3. 用 Git Data API 一次性建 tree + commit + 推 branch
  4. 如果是新仓库，自动开启 Issues / 加描述

不会做：不碰你的本地 git 配置、不改其它仓库、不删任何远程内容。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"

# 绝不允许上传的东西（与 build_gui.py 的检查保持一致）
FORBIDDEN_NAMES = {
    "game_manifest.json", "game_manifest_full.json", "manifest_local.json",
    "game_assets_uasset.txt", "mod_assets.json", "mod_assets_report.txt",
    "visceral_paths.json", "ron_mods_report.json",
    "unrealpak.exe", "oo2core.dll", "ronconvert_gui.json",
}
FORBIDDEN_EXT = {".pak", ".sig", ".ucas", ".utoc", ".exe", ".dll", ".pyc"}
SKIP_DIRS = {"build", "dist", "__pycache__", "_work", ".git", ".venv", "venv"}

DESCRIPTION = ("Diagnose and repair Ready or Not mods that broke after a game "
               "update. Unofficial fan tool. MIT.")
TOPICS = ["ready-or-not", "unreal-engine", "pak", "modding", "mod-tool", "python"]


class GH:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, path: str, payload=None, ok=(200, 201)):
        url = path if path.startswith("http") else API + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "ron-pak-tools-publisher")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, {"raw": body}


def collect_files(root: str) -> list[tuple[str, bytes]]:
    """收集要上传的文件，路径用正斜杠（仓库内相对路径）。"""
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            full = os.path.join(base, f)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                with open(full, "rb") as fh:
                    out.append((rel, fh.read()))
            except Exception as ex:
                print(f"   ! 读不了 {rel}: {ex}")
    return sorted(out)


def audit(files: list[tuple[str, bytes]]) -> list[str]:
    """返回违规文件名列表（空表示通过）。"""
    bad = []
    for rel, _data in files:
        name = os.path.basename(rel).lower()
        ext = os.path.splitext(name)[1].lower()
        if name in FORBIDDEN_NAMES or ext in FORBIDDEN_EXT:
            bad.append(rel)
    return bad


def main() -> int:
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(description="发布到 GitHub（REST API，无需 git）")
    ap.add_argument("--user", required=True, help="你的 GitHub 用户名")
    ap.add_argument("--repo", default="ron-pak-tools", help="仓库名")
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)),
                    help="要发布的目录（默认本脚本所在目录）")
    ap.add_argument("--private", action="store_true", help="建私有仓库")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default="Initial commit: ron-pak-tools")
    ap.add_argument("--dry-run", action="store_true", help="只检查，不上传")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token and not args.dry_run:
        print("✘ 没有找到 GITHUB_TOKEN 环境变量。")
        print("  去 https://github.com/settings/tokens 建一个（勾 repo 权限），然后：")
        print("    set GITHUB_TOKEN=ghp_xxxx")
        return 2

    root = os.path.abspath(args.dir)
    print(f"目录: {root}")
    files = collect_files(root)
    print(f"待上传 {len(files)} 个文件，合计 "
          f"{sum(len(d) for _r, d in files)/1024:.0f} KB")

    print("\n合规检查（不得包含游戏数据 / Epic 二进制）：")
    bad = audit(files)
    if bad:
        print("✘ 发现不允许上传的文件，已中止：")
        for b in bad:
            print("     " + b)
        return 1
    print("   ✔ 通过")

    print("\n将要上传：")
    for rel, data in files:
        print(f"   {len(data):>8,}  {rel}")
    if args.dry_run:
        print("\n（--dry-run：未上传）")
        return 0

    gh = GH(token)

    # 1) 校验 token
    st, me = gh.call("GET", "/user")
    if st != 200:
        print(f"✘ token 无效或无权限：{st} {me.get('message')}")
        return 1
    print(f"\n已登录：{me.get('login')}")

    # 2) 建仓库（存在就复用）
    full = f"{args.user}/{args.repo}"
    st, info = gh.call("GET", f"/repos/{full}")
    if st == 200:
        print(f"仓库已存在，复用：{info.get('html_url')}")
    else:
        print(f"仓库不存在，尝试创建 {full} ...")
        st, info = gh.call("POST", "/user/repos", {
            "name": args.repo,
            "description": DESCRIPTION,
            "private": bool(args.private),
            "has_issues": True,
            "has_wiki": False,
            "auto_init": False,
        })
        if st not in (200, 201):
            msg = info.get("message", "")
            print(f"\n✘ 无法创建仓库：{st} {msg}")
            if st == 403:
                print(
                    "\n  你的 token 没有「创建仓库」权限。这很常见 ——\n"
                    "  fine-grained token 默认不含 Administration 权限，\n"
                    "  classic token 必须勾选 repo。\n\n"
                    "  【最简单的解决办法】先在网页上建一个空仓库，再重跑本脚本：\n"
                    "    1. 打开 https://github.com/new\n"
                    f"    2. Repository name 填 {args.repo}\n"
                    "    3. 选 Public\n"
                    "    4. ★ 什么都不要勾（不要 Add README / .gitignore / license）\n"
                    "    5. 点 Create repository\n"
                    f"    6. 重新运行： python publish_github.py --user {args.user} "
                    f"--repo {args.repo}\n\n"
                    "  或者换一个 token：\n"
                    "    · classic token：https://github.com/settings/tokens "
                    "（勾 repo）\n"
                    "    · fine-grained token：在 Repository permissions 里把\n"
                    "      Administration 设为 Read and write\n")
            return 1
        print(f"   已创建：{info.get('html_url')}")

    # 3) 建 blobs -> tree -> commit -> 推 branch
    print("\n上传文件 ...")
    tree = []
    for rel, data in files:
        st, blob = gh.call("POST", f"/repos/{full}/git/blobs", {
            "content": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
        })
        if st not in (200, 201):
            print(f"✘ blob 失败 {rel}: {st} {blob.get('message')}")
            return 1
        tree.append({"path": rel, "mode": "100644", "type": "blob",
                     "sha": blob["sha"]})
        print(f"   ✓ {rel}")

    st, newtree = gh.call("POST", f"/repos/{full}/git/trees",
                          {"tree": tree})
    if st not in (200, 201):
        print(f"✘ tree 失败：{st} {newtree.get('message')}")
        return 1

    payload = {"message": args.message, "tree": newtree["sha"]}
    st, ref = gh.call("GET", f"/repos/{full}/git/ref/heads/{args.branch}")
    if st == 200:
        payload["parents"] = [ref["object"]["sha"]]
    st, commit = gh.call("POST", f"/repos/{full}/git/commits", payload)
    if st not in (200, 201):
        print(f"✘ commit 失败：{st} {commit.get('message')}")
        return 1

    if "parents" in payload:      # 更新已有分支
        st, r = gh.call("PATCH", f"/repos/{full}/git/refs/heads/{args.branch}",
                        {"sha": commit["sha"], "force": False})
    else:                          # 新建分支
        st, r = gh.call("POST", f"/repos/{full}/git/refs",
                        {"ref": f"refs/heads/{args.branch}", "sha": commit["sha"]})
    if st not in (200, 201):
        print(f"✘ 更新分支失败：{st} {r.get('message')}")
        return 1

    # 4) 设置 topics（失败不影响）
    gh.call("PUT", f"/repos/{full}/topics", {"names": TOPICS})

    url = info.get("html_url") or f"https://github.com/{full}"
    print(f"\n✔ 发布完成：{url}")
    if not os.path.isfile(os.path.join(root, ".git")):
        print("\n提示：以后想用 git 管理，装一下 git 再：")
        print(f"   git clone {url}.git")
    return 0


if __name__ == "__main__":
    sys.exit(main())
