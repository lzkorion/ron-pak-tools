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
2. 首次使用点「**先生成官方资产清单**」（用你自己装的游戏，在本机生成，需几分钟）
3. 选模组文件夹 → 点「开始转换」
4. 结果在 `<模组文件夹>\converted\`，**原文件不会被修改**

#### 方式二：命令行

```powershell
# 一键批量转换
python tools/ronconvert.py "C:\你的模组文件夹" --verify

# 先看诊断，不写文件
python tools/ronconvert.py "C:\你的模组文件夹" --dry-run
```

### 转换后的四种结论

| 结论 | 含义 | 你该做什么 |
|---|---|---|
| **已转换（剥离冲突）** | 剥掉了会冲突的蓝图/数据资产 | 用 `converted` 里的文件替换原模组 |
| **本来就可用** | 没有可剥的冲突资产 | 不用动 |
| **已被官方完全取代** | 剥离后一条不剩 | **删除该模组** |
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
蓝图/逻辑/数据表/角色/武器/UI」的官方同名条目。
需要激进模式时用 `--strip-all`（**可能把贴图类 mod 改坏，慎用**）。

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
2. 和**你本机生成的**官方清单比对
3. 只丢弃判定为冲突的条目
4. **压缩字节原样搬运**（不需要 Oodle 压缩器），重建索引

⇒ 保留的条目与原包**逐字节相同**，不是重新压缩。

### 项目结构

```
ron-pak-tools/
├── ronconvert_gui.py     图形界面
├── build_gui.py          打包脚本（含合规检查）
├── tools/
│   ├── pakfmt.py         ★ pak v11/v12 读写库（核心）
│   ├── ronconvert.py     ★ 检测 + 转换逻辑
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
| **Repacking** | Own pak v11/v12 writer. Compressed bytes are copied verbatim, so kept entries stay **byte-identical** |
| **Verification** | Optionally calls your local UnrealPak for `-List` / `-Test` |
| **GUI** | Double-click the exe — no command line needed |

### Download

**[⬇ Download RoNPakTools.exe](https://github.com/lzkorion/ron-pak-tools/releases/latest)**
(~11 MB, Windows 10/11 x64, portable, no Python required)

On first run it generates an "official asset manifest" from *your own* game
installation. It stays on your machine and is never uploaded.

### Quick start

```powershell
# Batch convert (writes to <folder>\converted, originals untouched)
python tools/ronconvert.py "C:\your\mod\folder" --verify

# Dry run: diagnose only
python tools/ronconvert.py "C:\your\mod\folder" --dry-run
```

GUI: double-click `RoNPakTools.exe`, generate the official asset manifest once
(created locally from *your* game install), then pick a folder and convert.

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
2. Compare against the official manifest **generated locally from your game**
3. Drop only the entries classified as conflicting
4. **Copy the compressed bytes verbatim** (no Oodle compressor required) and rebuild the index

⇒ Kept entries are **byte-identical** to the source pak.

### Legal

[MIT](LICENSE) licensed. See [NOTICE.md](NOTICE.md) for compliance boundaries.

This project **does not** contain or distribute any game assets, asset manifests,
sample paks, Unreal Engine source, or Epic binaries (`UnrealPak.exe` is an
"Engine Tool" whose distribution the Unreal Engine EULA forbids).
The asset manifest is generated on your own machine from your own game install.

**Not affiliated with VOID Interactive or Epic Games.**
