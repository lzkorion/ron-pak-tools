# ron-pak-tools

[![tests](https://github.com/lzkorion/ron-pak-tools/actions/workflows/tests.yml/badge.svg)](https://github.com/lzkorion/ron-pak-tools/actions/workflows/tests.yml)

**Ready or Not 模组 `.pak` 诊断与转换工具**
Detect and repair Ready or Not mods that broke after a game update.

[中文](#中文) · [English](#english) · **[下载 exe](https://github.com/lzkorion/ron-pak-tools/releases/latest)** · [NOTICE.md](NOTICE.md) · [LICENSE](LICENSE) (MIT)

> ⚠️ **非官方第三方工具**，与 VOID Interactive / Epic Games **无任何关联**，未获其授权或背书。
> 本项目**不含**任何游戏资产、游戏数据清单或 Epic 工具。
> "Ready or Not" 为 VOID Interactive 商标；"Unreal" 为 Epic Games 商标。

![主界面](docs/screenshots/01-main.png)

---

## 中文

### 这是什么

Ready or Not 每次大更新，都会**把一部分热门模组的内容直接做进游戏本体**。
但玩家的模组还留着，覆盖着同一批资产路径：

```
模组：/Game/Blueprints/Items/WeaponsRevised/Primary_G3A3   ← 旧编译产物
官方：/Game/Blueprints/Items/WeaponsRevised/Primary_G3A3   ← 新编译产物
```

挂载冲突 → **崩溃 / 卡在加载**。

**这不是版本问题，升级 pak 版本救不了。** 真正要做的是
**把官方已经内置的那部分剥掉**，只留模组独有的内容。

这个工具就是干这个的：扫一遍你的模组文件夹，判断哪些内容官方已经有了，
把会冲突的部分剥掉重新打包。

### 功能

| 功能 | 说明 |
|---|---|
| **批量转换** | 整个文件夹丢进去，自动逐个检测并转换 |
| **智能判定** | 只剥「蓝图/逻辑/数据」这类会冲突的；贴图/模型/音频等**资源替换一律保留** |
| **重新打包** | 自己实现 pak v11/v12 写入器，压缩数据原样搬运，保留条目**逐字节不变** |
| **官方校验** | 可选调用本机 UnrealPak 做 `-List` / `-Test` 复核 |
| **图形界面** | 双击 exe 就能用，不需要命令行 |

### 下载即用（不需要装 Python）

**[⬇ 下载 RoNPakTools.exe](https://github.com/lzkorion/ron-pak-tools/releases/latest)**
（约 11 MB，Windows 10/11 64 位，免安装）

下载后核对一下校验和更稳妥：

```powershell
Get-FileHash .\RoNPakTools.exe -Algorithm SHA256
```

首次运行会让你先生成「官方资产清单」——用你自己安装的游戏在本机生成，
只写在你电脑上，**不包含也不上传任何游戏数据**。

### 快速开始

#### 方式一：图形界面（推荐）

1. 双击 `RoNPakTools.exe`
2. 首次使用点「**先生成官方资产清单**」（用你自己装的游戏，在本机生成，几秒即可）
3. 选模组文件夹 → 点「开始转换」
4. 结果在 `<模组文件夹>\converted\`，**原文件不会被修改**

#### 方式二：命令行

```powershell
# 从游戏本体 paks 生成全路径清单（几秒），再做转换
python tools/ronconvert.py "C:\你的模组文件夹" `
    --game-paks "E:\SteamLibrary\steamapps\common\Ready Or Not\ReadyOrNot\Content\Paks"

# 一键批量转换（已有清单时）
python tools/ronconvert.py "C:\你的模组文件夹" --verify

# 先看诊断，不写文件
python tools/ronconvert.py "C:\你的模组文件夹" --dry-run
```

> 清单必须是**全路径**（`readyornot/content/blueprints/...`）。
> 如果手上的是只有文件名的旧清单，工具会提示你重新生成 ——
> 因为按文件名剥会把模组自己的贴图剥掉。

### 转换后的四种结论

| 结论 | 含义 | 你该做什么 |
|---|---|---|
| **已转换（剥离冲突）** | 剥掉了会冲突的蓝图/数据资产 | 用 `converted` 里的文件替换原模组 |
| **本来就可用** | 没有可剥的冲突资产 | 不用动 |
| **已被官方完全取代** | 剥离后一条不剩 | **删除该模组**（不会产出空 pak） |
| **无法处理** | 不是合法 pak | 人工检查 |

转换完成后会弹窗告知输出目录，日志里给出每个模组的结论和行动建议：

![转换完成](docs/screenshots/02-done.png)

### ★ 为什么不是「官方已有就全剥」

「官方已有」有两种完全不同的含义：

| 情况 | 例子 | 处理 |
|---|---|---|
| 蓝图/逻辑/数据被官方新版取代 | 武器改进包 | **会冲突崩溃 → 剥掉** |
| 贴图/模型/音频等资源替换 | 血腥贴图 | **正常 mod → 必须保留** |

所以默认走**保守白名单**：只剥「扩展名是 `.uasset`/`.umap` 且路径像
蓝图/逻辑/数据表/角色/武器/UI」的官方条目。
需要激进模式时用 `--strip-all`（**可能把贴图类 mod 改坏，慎用**）。

### ★★ 「同名」不等于「同路径」（v1.0.1 修的关键 bug）

光有白名单还不够，**判定「官方已有」必须要求路径完全一致**。

真实模组的路径长这样：

```
mount = '../../../ReadyOrNot/Content/'
rel   = 'ReadyOrNot/Character/Gore/Gore_Cuts/T_Gore_Limb_Amputations_body_BC.uasset'
```

注意 `rel` 里**又写了一遍 `ReadyOrNot/`**（挂载点里已经有了），所以它和官方
永远「同路径匹配不上」，但**文件名**和官方某个资产一样。

v1.0.0 用文件名判定「官方已有」→ 把模组自己的贴图/网格当成官方内容剥掉 →
**模组直接失效**。实测（用户提供的原版/转换后成对数据）：

| 模组 | 条目 | 旧逻辑剥掉 | 其中误杀 | 新逻辑剥掉 |
|---|---|---|---|---|
| Restoration | 39 | 33 | **25** 个骨骼网格/贴图 | 8（全是蓝图） |
| VisceralBlud | 1125 | 155 | **151** 个贴图/贴花/MI | 4 |
| VisceralGore | 523 | 31 | **31** 个网格/贴图/MI | 0（本来就可用） |
| wound | 9 | **9（全剥光）** | 9 | 0（本来就可用） |

`wound` 被剥到 0 条，pak 只剩 396 字节 —— 装上去完全没效果。

从 v1.0.1 起：

* 匹配强度分三级：`full`（路径完全一致，**可信**）/ `bare`（只有文件名相同，**存疑**）/ `none`
* **默认只信 `full`**；`bare` 一律不剥
* 模组路径里重复的 `ReadyOrNot/`、`Content/` 前缀会自动去掉再比对，让 `full` 能命中
* 需要按文件名剥时，显式开 `--match-name`（GUI 里的「同名也剥」），
  且同名出现在多个目录时仍然拒绝剥
* 清单里如果只有裸文件名，工具会**明确报错**让你重新生成，而不是静默空转

> 这条原则在代码注释、测试（`tests/test_safety.py`）和本文档里都有，
> 有测试保护，别再改回去。

### ★★ 路径一致 ≠ 内容一致（v1.1.0 起：改过的一律保留）

就算路径完全一致，也只能说明「指向同一个资产」，**不代表内容一样**。
而这两件事的处理方式**完全相反**：

| 情况 | 例子 | 处理 |
|---|---|---|
| 模组只是**照抄**了官方资产（作者打包时顺手带上的依赖） | 旧蓝图副本 | **剥掉**：零损失，还能解决崩溃 |
| 模组**改过**这个资产（那就是模组的功能本身） | 血腥 mod 改 `Blood_Standard` 数据表、改 `BP_RoNBloodPool` 让它生成自己的贴花 | **必须保留**：剥掉 = 游戏能进但**什么都不发生** |

**怎么区分？** 比对官方条目的两个大小：

```
未压缩大小 == 模组 且 压缩后大小 == 模组   -> 照抄官方，剥掉零风险
任一不同                                  -> 内容不一样，当作「模组改过」，不剥
```

> **为什么必须两个都比**（这点很反直觉，但官方指南讲得很清楚）：
> RoN 模组是用 [UAssetGUI](https://unofficial-modding-guide.com/posts/uassetmodding/)
> **直接改游戏烤好的资产**做出来的 —— 改个数字、换个引用，**文件大小根本不会变**。
> 所以「未压缩大小一样」是这个游戏里**正常模组的常态**，完全不能当作「照抄官方」的证据。
>
> 实测 `VisceralBlud` 的 `BP_RoNBloodPool`：未压缩两边都是 6,057 B，
> 但压缩后 **2,260 vs 2,198** —— Oodle 对相同输入是确定性的，压缩后不一样
> 就说明内容真的被改过。只看未压缩大小会把它误判成「照抄」，
> 剥掉之后整个血腥效果就没了（这就是「能进游戏但什么都不发生」）。

v1.1.0 起默认**只剥能证明是照抄官方的**，其余一律保留。要连改过的一起剥
（比如游戏一进就崩、必须清掉旧蓝图），加 `--strip-modified`
（界面勾「**连改过的也剥**」）—— 但开了之后模组很可能变成
「能进游戏但什么都不发生」，这**正是**之前用户反馈的那个症状。

清单里没有 `sizes`/`csizes` 段时判断不了，程序会**保守地一个都不剥**并提示重新生成
（生成只要几秒）。

实测四个真实模组（v1.1.0 默认）：

| 模组 | 条目 | 之前会剥 | v1.1.0 默认 | 原因 |
|---|---|---|---|---|
| Restoration | 39 | 8 | **0** | 4 个蓝图内容被模组改过 |
| VisceralBlud | 1125 | 4 | **0** | `Blood_Standard` + `BP_RoNBloodPool` 都被改过 |
| VisceralGore | 523 | 0 | **0** | 本来就无可剥 |
| wound | 9 | 0 | **0** | 本来就无可剥 |

也就是说这四个模组**原样输出**（逐字节等于原文件）。

### 体检模式：为什么这个模组装了没效果？（v1.2.0）

转换之外还有一个**只诊断、不写文件**的模式（界面勾「体检模式」，或命令行
`python tools/ronhealth.py "某模组.pak"`）。它按官方指南
[The Basics → Debugging](https://unofficial-modding-guide.com/posts/thebasics/)
的清单逐条查：

| 检查项 | 报什么 |
|---|---|
| 文件名 | 必须 `_P.pak` 结尾 —— 指南点名：「No `_P` at the end of the mod name」 |
| 挂载点 | 必须是「包住全部内容的最深目录」，**而且真实存在于游戏里** |
| 路径 | 多少条能对上官方（覆盖），多少条是游戏里没有的新路径 |
| 加载顺序 | 同一路径被别的模组用**更大的 pakchunk 号**覆盖了 → 你输；同号 → 谁生效不确定 |
| 资产成组 | `.uasset`/`.uexp`/`.ubulk` 缺一不可，孤儿条目会让引擎读不了 |
| 包格式 | 抽样看 `.uasset` 头部魔数对不对 |
| 官方工具 | 可选跑 UnrealPak `-List` / `-Test` |

输出长这样：

```
── pakchunk9999-ArteryHits_P.pak
   ✔ 文件名以 `_P.pak` 结尾
   ✔ 挂载点在游戏里存在（76 条官方路径在它下面）
   ✔ 覆盖官方资产 2 条   例：AmmoDataTable.uasset、AmmoDataTable.uexp
   ✔ 资产成组完整（1 个资产，无孤儿/缺件）
   ── 结论：没发现结构性问题
```

### 清单生成：现在只要几秒

v1.0.0 生成清单要把每个本体 pak 整份读进内存（`pakchunk0` 有 24 GB），
既慢又可能爆内存 —— 实测常常直接跳过 `pakchunk0`，导致清单缺了 39 万条路径。

v1.0.1 只读 **pak 尾部 4 KB + 索引区**（`pakfmt.read_pak_index`），
25 个本体 pak、约 44 GB，**3 秒**扫完，47 万条全路径。

### 安装

需要 **Python 3.10+**（用到 `X | None` 类型语法）。无需第三方库，只用标准库。

```powershell
git clone https://github.com/lzkorion/ron-pak-tools.git
cd ron-pak-tools
python tools/ronconvert.py "C:\你的模组文件夹" --dry-run
```

**可选**：装了 Unreal Engine 才有 `UnrealPak.exe`，用于结果校验。
没有也能正常转换，只是跳过校验。

### 自己打包 exe

```powershell
pip install pyinstaller
python build_gui.py
# 产物：dist/RoNPakTools.exe
```

`build_gui.py` 会在打包前检查，确保不会把游戏数据或 Epic 工具打进发行物。

### 测试

```powershell
python tests/run_all.py
```

全部用**合成 pak**（`tests/fixtures.py` 现场生成），不需要游戏或真实模组。

推送后 GitHub Actions 会自动跑这些测试（Ubuntu + Windows，Python 3.10 / 3.11 / 3.13），另外还会检查仓库里没有混入游戏数据或 Epic 工具。

### 工作原理（为什么不会损坏内容）

1. 用自研解析器读出模组的**完整路径表**（pak 里本来就有，不需要猜）
2. 和**你本机生成的**官方清单比对 —— 只认**路径完全一致**，同名不算
3. 只丢弃判定为冲突的条目，并且**按资产整组处理**
   （一个资产 = `.uasset` + `.uexp` + `.ubulk` + `.bak` 变体，漏一个就成孤儿）
4. **压缩字节原样搬运**（不需要 Oodle 压缩器），重建索引

⇒ 保留的条目与原包**逐字节相同**，不是重新压缩。

已用四个真实模组端到端复核（官方 `UnrealPak -List` / `-Test` 全部 rc=0，
孤儿 0、缺件 0、新增 0、挂载点不变）。

### 项目结构

```
ron-pak-tools/
├── ronconvert_gui.py     图形界面
├── build_gui.py          打包脚本（含合规检查）
├── tools/
│   ├── pakfmt.py         ★ pak v11/v12 读写库（核心）
│   ├── ronconvert.py     ★ 检测 + 转换逻辑
│   ├── ronverify.py      对比原版/转换后（孤儿 / 缺件 / 挂载点）
│   ├── ronstrip.py       精确剥离（--keep / --drop）
│   ├── roncheck.py       批量体检
│   ├── ronunreal.py      官方 UnrealPak 封装
│   └── ...               其它诊断工具
├── tests/                测试（全用合成数据）
└── docs/                 格式说明
```

### 许可

[MIT](LICENSE)。使用前请读 [NOTICE.md](NOTICE.md)（合规边界与风险提示）。

---

## English

### What this is

![Main window](docs/screenshots/01-main.png)

Every major **Ready or Not** update absorbs popular mods into the base game,
while players' mod `.pak` files keep overriding the same asset paths.
That mount conflict causes **crashes or infinite loading**.

**It is not a version problem** — bumping the pak version does not help.
The fix is to **strip the parts the game already ships** and keep only what is unique.

This tool scans your mod folder, decides which content the game already provides,
and repacks the mod without the conflicting parts.

### Features

| Feature | Description |
|---|---|
| **Batch convert** | Point it at a folder; every `.pak` is diagnosed and converted |
| **Conservative stripping** | Only strips blueprint/logic/data assets that conflict. Texture/model/audio replacements are **always kept** |
| **Path-exact matching** | "Same file name" is **not** "same asset". Only a full path match counts (see below) |
| **Repacking** | Own pak v11/v12 writer. Compressed bytes are copied verbatim, so kept entries stay **byte-identical** |
| **Verification** | Optionally calls your local UnrealPak for `-List` / `-Test` |
| **GUI** | Double-click the exe — no command line needed |

### Download

**[⬇ Download RoNPakTools.exe](https://github.com/lzkorion/ron-pak-tools/releases/latest)**
(~11 MB, Windows 10/11 x64, portable, no Python required)

On first run it generates an "official asset manifest" from *your own* game
installation. It stays on your machine and is never uploaded.

### ★★ "Same name" is not "same path" (the bug fixed in v1.0.1)

Requiring a full path match is what makes stripping safe. Real mods look like this:

```
mount = '../../../ReadyOrNot/Content/'
rel   = 'ReadyOrNot/Character/Gore/Gore_Cuts/T_Gore_Limb_Amputations_body_BC.uasset'
```

`rel` repeats `ReadyOrNot/` even though the mount point already contains it, so it
can never match an official path — yet its **file name** matches an official asset.

v1.0.0 matched on file name, so it stripped the mod's *own* textures and meshes and
**the mod stopped working**. Measured on real mods (original vs. converted pairs):

| Mod | Entries | Stripped by v1.0.0 | of which wrong | Stripped by v1.0.1 |
|---|---|---|---|---|
| Restoration | 39 | 33 | **25** skeletal meshes/textures | 8 (all blueprints) |
| VisceralBlud | 1125 | 155 | **151** textures/decals/MIs | 4 |
| VisceralGore | 523 | 31 | **31** meshes/textures/MIs | 0 (already fine) |
| wound | 9 | **9 (all of it)** | 9 | 0 (already fine) |

`wound` was stripped down to 0 entries — a 396-byte pak that does nothing.

Since v1.0.1:

* Match strength is graded `full` (identical path, **trusted**) / `bare` (name only, **suspect**) / `none`
* **Only `full` is trusted by default**; `bare` is never stripped
* Duplicated `ReadyOrNot/` and `Content/` prefixes are stripped before comparing,
  so `full` can actually hit
* Name-based stripping requires `--match-name` (GUI: "同名也剥"), and refuses
  names that occur in more than one directory
* A manifest containing only bare file names now produces an explicit error
  instead of silently doing nothing

### ★ Same path ≠ same content (since v1.1.0: edited assets are always kept)

An identical path only proves the mod points at the *same asset* — not that the
content is the same, and the two cases need **opposite** handling:

| Case | Example | Handling |
|---|---|---|
| Mod merely **copies** an official asset (a dependency the author packed by accident) | stale blueprint copy | **Strip it** — zero loss, and it fixes the crash |
| Mod **edited** that asset (that edit *is* the feature) | a gore mod editing `Blood_Standard`, or `BP_RoNBloodPool` to spawn its own decals | **Must keep it** — stripping gives you "loads fine, does nothing" |

**How they are told apart** — both sizes are compared:

```
uncompressed size matches AND compressed size matches  -> verbatim copy, safe to strip
either one differs                                     -> treat as "mod edited it", keep
```

> Why both: `VisceralBlud`'s `BP_RoNBloodPool` has an **identical uncompressed size**
> (6,057 B) but a **different compressed size** (2,260 vs 2,198). Oodle is
> deterministic for identical input, so a different compressed size means the
> content really differs. Comparing only uncompressed size would misclassify it
> as a copy — and stripping it kills the whole blood effect.

Since v1.1.0 only provable verbatim copies are stripped. To also strip edited
assets (needed when the game crashes on load), pass `--strip-modified`
(GUI: "连改过的也剥") — be aware that this is what produces the
"game loads but nothing happens" reports.

When the manifest has no `sizes`/`csizes`, nothing can be proven, so the tool
**conservatively strips nothing** and asks you to regenerate the manifest (seconds).

Measured on four real mods (v1.1.0 defaults):

| Mod | Entries | Previously stripped | v1.1.0 default | Why |
|---|---|---|---|---|
| Restoration | 39 | 8 | **0** | 4 blueprints were edited by the mod |
| VisceralBlud | 1125 | 4 | **0** | both `Blood_Standard` and `BP_RoNBloodPool` were edited |
| VisceralGore | 523 | 0 | **0** | nothing to strip |
| wound | 9 | 0 | **0** | nothing to strip |

All four are now emitted **byte-identical to the source pak**.

### Manifest generation is now fast

v1.0.0 read each base-game pak fully into memory (`pakchunk0` is 24 GB), which was
slow enough that it usually skipped `pakchunk0` — leaving 390k paths missing.

v1.0.1 reads only the **last 4 KB of the pak plus its index region**
(`pakfmt.read_pak_index`): 25 paks / ~44 GB in **3 seconds**, 466k full paths.

### Quick start

```powershell
# Generate a full-path manifest from the base game, then convert
python tools/ronconvert.py "C:\your\mod\folder" `
    --game-paks "E:\SteamLibrary\steamapps\common\Ready Or Not\ReadyOrNot\Content\Paks"

# Batch convert (writes to <folder>\converted, originals untouched)
python tools/ronconvert.py "C:\your\mod\folder" --verify

# Dry run: diagnose only
python tools/ronconvert.py "C:\your\mod\folder" --dry-run
```

GUI: double-click `RoNPakTools.exe`, generate the official asset manifest once
(created locally from *your* game install, takes seconds), then pick a folder and convert.

### Requirements

- **Python 3.10+** (uses `X | None` syntax). Standard library only.
- **Optional:** Unreal Engine installed, for `UnrealPak.exe` verification.
  Conversion works without it.

### Tests

```powershell
python tests/run_all.py
```

All tests build **synthetic paks on the fly** — no game files or real mods needed.

### How it works

Mod paks already contain a full path table, so nothing has to be guessed:

1. Read the pak's complete path table with the built-in parser
2. Compare against the official manifest **generated locally from your game** —
   only **path-exact** matches count, a shared file name does not
3. Drop only the entries classified as conflicting, and drop them **per asset**
   (one asset = `.uasset` + `.uexp` + `.ubulk` + `.bak` variants; missing one
   leaves an orphan the engine will choke on)
4. **Copy the compressed bytes verbatim** (no Oodle compressor required) and rebuild the index

⇒ Kept entries are **byte-identical** to the source pak.

Validated end-to-end on four real mods: official `UnrealPak -List` / `-Test`
both return rc=0, with 0 orphans, 0 missing companions, 0 added entries and
unchanged mount points.

### Legal

[MIT](LICENSE) licensed. See [NOTICE.md](NOTICE.md) for compliance boundaries.

This project **does not** contain or distribute any game assets, asset manifests,
sample paks, Unreal Engine source, or Epic binaries (`UnrealPak.exe` is an
"Engine Tool" whose distribution the Unreal Engine EULA forbids).
The asset manifest is generated on your own machine from your own game install.

**Not affiliated with VOID Interactive or Epic Games.**
