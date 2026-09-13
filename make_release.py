#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把打包好的 exe 发到 GitHub Releases。

用法（token 只用于这一次，不会写进任何文件）：
    set GITHUB_TOKEN=ghp_xxxx
    python make_release.py --user lzkorion --repo ron-pak-tools --tag v1.2.0

会做：
  1. 发布前合规检查（exe 内不得含游戏数据 / Epic 工具）
  2. 建 tag + Release（已存在则复用）
  3. 上传 exe 作为附件
  4. 打印 SHA256 并写进 Release 说明

不会做：不改源码、不碰 git 配置、不删任何东西。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
EXE_NAME = "RoNPakTools.exe"


class GH:
    def __init__(self, token: str):
        self.token = token

    def call(self, method, path, payload=None, raw_body=None,
             content_type="application/json", ok=(200, 201)):
        url = path if path.startswith("http") else API + path
        if raw_body is not None:
            data = raw_body
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
        else:
            data = None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "ron-pak-tools-release")
        if data:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, {"raw": body}


def audit_exe(path: str) -> list[str]:
    """检查 exe 里没有游戏数据 / Epic 工具。"""
    try:
        import PyInstaller.archive.readers as R
    except Exception:
        return []            # 没装 pyinstaller 就跳过（不阻断发布）
    bad = []
    try:
        a = R.CArchiveReader(path)
        for k in a.toc:
            low = k.lower()
            if any(s in low for s in ("manifest", "sample", "unrealpak",
                                      "oo2core", ".pak")):
                bad.append(k)
    except Exception as ex:
        print(f"   （无法读取 exe 归档，跳过深度检查：{ex}）")
    return bad


NOTES = """## 下载 / Download

**`RoNPakTools.exe`** — 免安装，双击即用（Windows 10/11 64 位）。
Portable Windows GUI. Requires no Python installation.

首次使用会提示先生成「官方资产清单」——用你自己安装的游戏在本机生成，
只写在你电脑上，**不包含也不上传任何游戏数据**。

On first run it asks you to generate an "official asset manifest" from *your own*
game installation. It is written only on your machine and never uploaded.

## 这个版本做什么

- 扫描模组文件夹，判断哪些内容游戏本体已经有了
- 只剥离**会冲突**的蓝图/逻辑/数据资产（贴图/模型/音频等资源替换一律保留）
- 重新打包，保留条目的压缩字节**逐字节不变**
- 可选调用你本机的 UnrealPak 做 `-List` / `-Test` 复核
- **原始模组文件不会被修改**，结果输出到 `converted` 子目录

## ⚠️ v1.0.0 有严重 bug，请务必升级到 v1.2.0

v1.0.0 用**文件名**判定「官方已有」，而真实模组的路径里常多写一层
`ReadyOrNot/`（挂载点里已经有了），于是和官方永远「同路径匹配不上」、
但文件名一样 —— 结果把**模组自己的贴图/网格**当成官方内容剥掉，模组直接失效。

实测（原版 vs. v1.0.0 转换后的成对数据）：

| 模组 | 条目 | v1.0.0 剥掉 | 其中误杀 | v1.0.1 剥掉 |
|---|---|---|---|---|
| Restoration | 39 | 33 | **25** 个骨骼网格/贴图 | 8（全是蓝图） |
| VisceralBlud | 1125 | 155 | **151** 个贴图/贴花/MI | 4 |
| VisceralGore | 523 | 31 | **31** 个网格/贴图/MI | 0（本来就可用） |
| wound | 9 | **9（全剥光）** | 9 | 0（本来就可用） |

`wound` 被剥到 0 条，pak 只剩 396 字节，装上去完全没效果。

### v1.0.1 改了什么

- **只信「路径完全一致」**：同名（`bare`）一律不剥；需要时用
  GUI 的「同名也剥」/ `--match-name` 显式开启，且同名出现在多个目录时仍拒绝剥
- 模组路径里重复的 `ReadyOrNot/`、`Content/` 前缀会自动去掉再比对，让真正的
  同路径资产仍能被正确识别
- **清单生成快了几百倍**：只读 pak 尾部 4 KB + 索引区
  （原来要把 24 GB 的 `pakchunk0` 整份读进内存，常常直接失败跳过），
  实测 25 个本体 pak / 约 44 GB **3 秒**扫完，47 万条全路径
- 手上如果是只有文件名的旧清单，程序会**明确提示重新生成**，而不是静默什么都不做
- 剥离后一条不剩时**不再产出空 pak**，而是明确告诉你「该模组已被官方取代，可以删除」
- 新增 `tests/test_safety.py` 把「同名≠同路径」这条底线锁进测试

已用四个真实模组端到端复核：官方 `UnrealPak -List` / `-Test` 全部 rc=0，
孤儿 0、缺件 0、新增 0、挂载点不变。

### v1.2.0 新增：体检模式

- **只诊断、不改文件**，直接回答「为什么这个模组装了没效果？」
  按[官方模组指南](https://unofficial-modding-guide.com/posts/thebasics/)的
  调试清单逐条查：
  - 文件名是不是 `_P.pak` 结尾（指南点名的常见错误）
  - 挂载点是不是「包住全部内容的最深目录」，**而且真实存在于游戏里**
  - 多少条路径能对上官方（覆盖），多少条是游戏里没有的新路径
  - **加载顺序**：同一路径被别的模组用更大的 pakchunk 号覆盖了 → 你输；
    同号 → 谁生效不确定
  - 资产成组完整性（孤儿 `.uexp`/`.ubulk`）、`.uasset` 包头魔数
  - 可选跑官方 UnrealPak `-List` / `-Test`
- 用法：界面勾「体检模式」，或命令行
  `python tools/ronhealth.py "某模组.pak"`

### v1.1.0（重要 —— 修「能进游戏但什么都不发生」）

- **「模组自己改过」的资产一律保留。** 路径一致但内容不同的资产，就是模组的
  功能本身（比如血腥 mod 改的 `Blood_Standard` 数据表、`BP_RoNBloodPool`）。
  之前把它们剥掉，结果就是评论区反馈的「**能进游戏但什么都不发生**」。
- 判断办法：同时比对官方条目的**未压缩大小**和**压缩后大小**，
  两个都相同才敢断定是「照抄官方」，才剥。
  > 只比未压缩大小会误判：`BP_RoNBloodPool` 未压缩 6,057 两边一模一样，
  > 但压缩后 2,260 vs 2,198 —— 内容其实不同。Oodle 对相同输入是确定性的，
  > 压缩后不同就说明真的不一样。
- 要连改过的一起剥（只在游戏一进就崩时才需要），勾界面上的
  「**连改过的也剥**」或命令行加 `--strip-modified` ——
  但请预期模组会变成「能进游戏但什么都不发生」。
- 清单里没有大小信息时**保守地一个都不剥**，并提示重新生成清单（几秒）。

实测四个真实模组，v1.2.0 默认下**原样输出**（逐字节等于原文件）：

| 模组 | 条目 | 之前会剥 | v1.2.0 默认 |
|---|---|---|---|
| Restoration | 39 | 8 | **0** |
| VisceralBlud | 1125 | 4 | **0** |
| VisceralGore | 523 | 0 | **0** |
| wound | 9 | 0 | **0** |

### v1.0.2 新增

- 清单里记录官方条目的未压缩大小，剥离前比一比并给出提示。

## 校验和 / Checksum

```
SHA256  {sha256}
```

建议下载后核对：

```powershell
Get-FileHash .\\{exe_name} -Algorithm SHA256
```

## 本程序不包含什么

- ❌ 任何游戏资产或资产清单
- ❌ 任何 `.pak` 样本
- ❌ `UnrealPak.exe` / `oo2core.dll` 或任何 Epic 工具
  （`UnrealPak` 属 Unreal Engine EULA 第 25 节定义的 "Engine Tools"，
  第 1A 节禁止向最终用户分发，因此本发行物不含它）

## 声明

**非官方第三方工具**，与 VOID Interactive / Epic Games 无任何关联。
"Ready or Not" 为 VOID Interactive 商标；"Unreal" 为 Epic Games 商标。

使用前请阅读仓库里的 [NOTICE.md](https://github.com/{full}/blob/main/NOTICE.md)。
修改游戏文件可能违反游戏的最终用户许可协议，请自行评估风险并备份。
"""


def main() -> int:
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(description="发布 exe 到 GitHub Releases")
    ap.add_argument("--user", required=True)
    ap.add_argument("--repo", default="ron-pak-tools")
    ap.add_argument("--tag", default="v1.2.0")
    ap.add_argument("--name", default=None, help="Release 标题（默认同 tag）")
    ap.add_argument("--exe", default=os.path.join("dist", EXE_NAME))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token and not args.dry_run:
        print("✘ 没有 GITHUB_TOKEN。先设置：set GITHUB_TOKEN=ghp_xxxx")
        return 2

    exe = os.path.abspath(args.exe)
    if not os.path.isfile(exe):
        print(f"✘ 找不到 exe：{exe}\n  先运行：python build_gui.py")
        return 1

    size = os.path.getsize(exe)
    print(f"exe: {exe}")
    print(f"     {size:,} 字节 ({size/1024/1024:.2f} MB)")

    print("\n发布前合规检查：")
    bad = audit_exe(exe)
    if bad:
        print("✘ exe 里发现不允许发布的内容，已中止：")
        for b in bad:
            print("     " + b)
        return 1
    print("   ✔ 无游戏数据 / Epic 工具")

    sha = hashlib.sha256(open(exe, "rb").read()).hexdigest()
    print(f"   SHA256 {sha}")

    if args.dry_run:
        print("\n（--dry-run：未上传）")
        return 0

    gh = GH(token)
    st, me = gh.call("GET", "/user")
    if st != 200:
        print(f"✘ token 无效：{st} {me.get('message')}")
        return 1
    print(f"\n已登录：{me.get('login')}")

    full = f"{args.user}/{args.repo}"
    tag = args.tag
    title = args.name or tag
    notes = NOTES.format(sha256=sha, full=full, exe_name=EXE_NAME)

    # 1) 建 Release（已存在则复用）
    st, rel = gh.call("GET", f"/repos/{full}/releases/tags/{tag}")
    if st == 200:
        print(f"Release {tag} 已存在，复用：{rel.get('html_url')}")
    else:
        print(f"创建 Release {tag} ...")
        st, rel = gh.call("POST", f"/repos/{full}/releases", {
            "tag_name": tag,
            "name": title,
            "body": notes,
            "draft": False,
            "prerelease": False,
        })
        if st not in (200, 201):
            print(f"✘ 创建失败：{st} {rel.get('message')}")
            print("   需要 contents=write 权限（classic token 勾 repo 即可）")
            return 1
        print(f"   已创建：{rel.get('html_url')}")

    upload_url = rel.get("upload_url", "").split("{")[0]
    if not upload_url:
        print("✘ 拿不到 upload_url")
        return 1

    # 2) 删掉同名旧附件（重发时避免冲突）
    for a in rel.get("assets", []) or []:
        if a.get("name") == EXE_NAME:
            print(f"   删除旧附件 {EXE_NAME} ...")
            gh.call("DELETE", f"/repos/{full}/releases/assets/{a['id']}")

    # 3) 上传 exe
    print(f"\n上传 {EXE_NAME}（{size/1024/1024:.1f} MB，稍等）...")
    with open(exe, "rb") as f:
        data = f.read()
    url = f"{upload_url}?name={EXE_NAME}"
    st, asset = gh.call(
        "POST", url, raw_body=data,
        content_type="application/octet-stream")
    if st not in (200, 201):
        print(f"✘ 上传失败：{st} {asset.get('message')}")
        return 1
    print(f"   ✔ 已上传：{asset.get('browser_download_url')}")

    st, rel2 = gh.call("GET", f"/repos/{full}/releases/tags/{tag}")
    print(f"\n✔ 完成：{rel2.get('html_url')}")
    print(f"   附件下载：{asset.get('browser_download_url')}")
    print(f"   SHA256  ：{sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
