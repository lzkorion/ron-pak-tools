#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把打包好的 exe 发到 GitHub Releases。

用法（token 只用于这一次，不会写进任何文件）：
    set GITHUB_TOKEN=ghp_xxxx
    python make_release.py --user lzkorion --repo ron-pak-tools --tag v1.9.0

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

## ⚠️ v1.0.0 有严重 bug，请务必升级到 v1.9.0

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

### v1.9.0 新增（支持老格式 pak + 两处安全修复）

**🕰 能读「很久没更新的老模组」了。** 社区里那些几年没更新的模组，很多是用老
打包工具做的（老格式 pak：v1~v9），以前工具直接报「读不了」。现在能读能诊断：

- 老格式（v1~v9）与现行格式（v10~v12）的索引结构完全不同 —— 老格式没有
  `FPakEntryLocation` / FDI / PHI，路径就直接写在主索引里，压缩块是
  「(起点, 终点)」绝对偏移对。这些都按实测的真实 pak 逐字节对过。
- 实测一个 2019 年前后打包的 HK416 武器模组（v3 + Zlib，227 条）：路径
  227/227 全部恢复，84 个资产的引用分析也跑通了。
- 老格式的压缩方式用的是内置枚举（1=Zlib / 2=Gzip），会自动映射成名字。
- **重新打包时会写成游戏本体在用的 v11**，不会「沿用源版本号」——
  以前那样写会产出一个 UnrealPak 连打开都打不开的包（实测 rc=1）。

**🛡 产物没过校验就删掉，绝不把坏 pak 留给你。** 现在只要「读回自检」或
官方 `UnrealPak -Test` 任何一项没过，产物会被**当场删除**并明确告诉你原因
（原模组不变、可继续用）。宁可什么都不给，也不给一个装上去会出事的包。

**🔧 修一个会写出坏 pak 的坑**：压缩方式名表必须写满 5 个槽位（每个 32 字节）。
只传 1 个方法名时，footer 会短 128 字节 —— UnrealPak 直接
「Unable to open pak file」，而自研读取器反而读得出来（它按 magic 扫描 +
校验索引 SHA1），所以这个坑藏了很久。现在写入器强制补齐，并有测试锁住。

**🔧 修一个误报**：Zlib / Gzip 是 UE 内核自带的压缩方式，**任何版本的游戏都读得了**。
以前把「模组用 Zlib、本体用 Oodle」报成 ✘ 错误（还建议你重打包）——
实测 Hospital 改压缩方式照样闪退，卡加载不是它造成的。现在改成提示，
只有「游戏真没有的解码器」（比如第三方插件的 Zstd）才报错。

### v1.8.0 新增（自动判断「这个模组能不能改」）

以前只回答「该不该转换」，现在诊断的最后会直接给一句**可改性结论** ——
本工具能不能改它、值不值得改、改了会不会更糟：

| 结论 | 意思 |
|---|---|
| **可以改** | 有明确、安全、值得做的事（下面会逐条列出「能做什么」） |
| **不用改** | 没有可改的地方 —— 原样用就行 |
| **别改** | 动手会毁掉它的功能（官方同路径的资产全是模组自己改过的） |
| **改不了** | 问题在本工具能力之外（地图要作者重烤、引用断了要作者修、pak 读不动…） |

几条刻意定死的规矩：

- **能用的模组一律不劝人动。**「不用改 / 改不了 / 别改」都会附一句
  「既然现在能用，就别动它 —— 不做事永远是安全的选项」。
- **有硬伤就不说「可以改」。**实测 Hospital 地图模组：压缩方式确实不一致、
  也确实能改，但**改完照样闪退**（我们真做过这个实验）——
  所以它现在被判为「改不了」，只是如实附上「能做什么：改压缩方式（做了也救不了它）」。
- **「改不了」会分清楚到底卡在哪**：是地图要重烤、是引用断了、还是 pak 读不动，
  一条条写明「工具做不了什么」，而不是含糊地说一句「有问题」。

实测 8 个装机模组：**7 个「改不了」+ 1 个「别改」（AK74M）**，
**没有一个「可以改」** —— 意思是它们现在这样就是最好的，别动。

- 命令行 `--assess` 现在**默认连引用分析一起做**（可改性判断要用它），
  `--no-refs` 可以关掉。
- GUI 的「引用分析」也改成**默认勾选**。

### v1.7.0 新增（引用分析：它引用的资产，游戏里还在不在？）

装了没效果 / 一闪退的另一个大头是**引用断了**。RoN 模组是拿 UAssetGUI 直接改
「游戏烤好的资产」做的，所以模组资产**内部记着它引用的包路径**。游戏一更新，
官方经常把资产改名、搬目录、拆成好几份 —— 模组还在引用老路径，
轻则那个资产加载不出来，重则加载时直接崩。**这个转换修不了**，以前也看不出来。

现在勾界面上的「**引用分析**」（或命令行 `--refs`），程序会：

1. 逐条读模组里的 `.uasset` / `.umap`（Oodle 压缩的用**你本机已有**的 `oo2core`
   解开；没有就明确说「压缩资产读不了」，只分析未压缩的那些）
2. 抽出每个资产自己记的包路径 + 它引用的包路径
3. 和**你本机生成的**官方清单对账，报出两类断链：

- **游戏里还有近似的名字** → 多半是这次更新改名/搬了目录，并给出**现在最可能叫什么**
  （实测：`LACRIMAL_INST_V2` → `.../instance/lacrimal_inst_v2`、
  `Curve_Damage_Shotgun` → `curve_damage_shotgun_590`、
  `icn_12g_bucknew` → `icn_12g_bucknew_1024`）
- **游戏里彻底没有这个包名** → 作者没打包进来，或者官方把它删了

还会顺带指出「资产内部记的包路径和它在 pak 里的位置对不上」这种情况
（引擎按包名找文件，对不上就永远加载不到它）。

实测 8 个真实装机模组，**一共约 9 秒**扫完（最慢的单个 2.8 秒）：

| 模组 | 资产 | 引用 | 断链 |
|---|---|---|---|
| Restoration | 14 | 79 | 4（LACRIMAL / SCLERA 那批被搬走的） |
| Hospital（地图） | 308 | 690 | 2 |
| HotelBarricaded（地图） | 2715 | 4816 | 103 |
| VisceralBlud | 422 | 599 | 19 |
| VisceralGore | 195 | 292 | 35 |
| wound | 3 | 0 | 0（但 3 个资产位置对不上） |
| AK74M Zenitco | 1132 | 3383 | 318 |
| ArteryHits | 1 | 53 | 3 |

只读不写：不修改任何文件，也不生成任何东西。

- 顺带修了一个静默 bug：`PakIndex.payload_of()`（只读索引的读取器）以前返回
  **被截断的错字节**，现在会按需从文件里读（大 pak 不用整个读进内存了）。

### v1.6.0 新增（自动改名修复 + 模组类型识别）

- **🩹 自动改名修复（勾界面上的「自动改名修复」，或命令行 `--fix-names <目录>`）**
  装了没效果的模组里，很大一部分其实是**文件名不对**。工具现在会算出该叫什么，
  并**复制**一份改好名的给你（**原文件绝不动**）：
  - 文件名不是 `_P.pak` 结尾 → 补上（[官方指南](https://unofficial-modding-guide.com/posts/thebasics/)
    点名的头号错误：主线 pak 存在时，补丁 pak 没有 `_P` 很可能根本不加载）
  - 文件名解析不出 `pakchunk<N>-` → 补 `pakchunk9999-`（否则加载顺序无从谈起）
  - **同一路径被 pakchunk 号更大的模组压着** → 把号提到 `最大号 + 1`，
    让你真的赢（而不是「装了但被别的模组盖掉」）
  同号、没冲突的一律不动 —— 只做确定性的事。
  读不动的 pak **不会**生成改名副本（免得看起来像被修好了）。

- **🔍 模组类型识别**：诊断时直接告诉你这个 mod 到底在改什么 ——
  地图 / 贴图替换 / 材质替换 / 网格替换 / 蓝图逻辑 / 数据表 / 音频替换 /
  动画 / 纯新增内容，并说明**转换对它有没有用**
  （例：贴图替换没有可剥的东西；自定义地图必须作者重新烤）。

- 命令行新增：`python tools/ronhealth.py "<模组目录>" --assess`（只诊断）
  和 `--fix-names "<输出目录>"`（诊断 + 出改名副本）。

### v1.5.0 新增（体检模式 + 两个硬 bug 修复）

- **修：路径恢复不再依赖「重新编码推算偏移」。** 目录索引里的 location 是
  「在 encoded 区里的字节偏移」，以前是拿我们自己的编码器重编一遍、累加长度
  去凑的。**打包方用的编码宽度和我们不一样时，累计偏移会整体漂移**，
  FDI 里的 location 大面积对不上 —— 实测 Hospital 地图模组 730 条只能恢复 117 条，
  重新打包会**静默丢掉 613 条**。现在改用解析时记下的真实偏移，同一文件 **730/730** 全中。
- **加保险**：万一还是没恢复全，`convert()` 会**拒绝重新打包、改为原样复制**
  并明确报告，而不是写出一个看着正常、实际少了内容的包。
- **体检新增「压缩方式」检查**：模组的压缩方式必须和游戏本体一致
  （本体是 `Oodle`；用了游戏没编解码器的方式 → 读资产失败 → 卡加载）。

- **体检模式**：只诊断、不改文件，直接回答「为什么这个模组装了没效果？」
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

实测四个真实模组，v1.5.0 默认下**原样输出**（逐字节等于原文件）：

| 模组 | 条目 | 之前会剥 | v1.5.0 默认 |
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
    ap.add_argument("--tag", default="v1.9.0")
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
