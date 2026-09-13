# NOTICE / 第三方与合规说明

本文件说明本项目的来源、边界，以及**刻意不包含**的东西。
This file explains provenance, scope, and what this project deliberately does NOT include.

---

## 1. 本项目的性质

**ron-pak-tools 是非官方第三方工具。**
与 VOID Interactive、Epic Games 及其关联公司**没有任何隶属、授权、赞助或背书关系**。

- "Ready or Not" 是 VOID Interactive 的商标。
- "Unreal"、"Unreal Engine" 是 Epic Games, Inc. 的商标。
- 本项目仅以**指示性**方式提及这些名称，用于说明兼容性，不主张任何权利。

## 2. 本项目包含什么

全部为**原创源代码**，MIT 许可（见 `LICENSE`）：

- `.pak` 文件格式的读写实现（`tools/pakfmt.py`）
- 模组诊断与转换逻辑（`tools/ronconvert.py` 等）
- 图形界面与打包脚本

格式知识来自公开可获取的 Unreal Engine 源码与对 `.pak` 文件的观察。
**文件格式本身不受著作权保护**；本实现为独立编写，未逐行复制引擎源码。

## 3. 本项目【不包含】什么

为避免侵权与违约，以下内容**一律不随本项目分发**：

| 不含内容 | 原因 |
|---|---|
| 任何游戏资产清单 / 资产名列表 | 派生自游戏数据，属于受保护内容 |
| 任何 `.pak` 样本文件 | 可能是他人作品 |
| `UnrealPak.exe`、`oo2core.dll` 或任何 Epic 二进制 | 属于 UE EULA Section 25 定义的 **Engine Tools**，EULA Section 1A 禁止向最终用户分发 |
| Unreal Engine 源码 | 受 Epic 许可限制 |
| 任何游戏本体文件 | 受游戏 EULA 限制 |

构建脚本 `build_gui.py` 会在打包前**主动校验**上述文件不存在，发现即中止。

### 关于 UnrealPak（可选依赖）

程序**调用**你本机已安装的 `UnrealPak.exe` 来做结果校验，但**不分发**它。
找不到时校验功能自动跳过，核心转换功能不受影响。

`UnrealPak` 是 Epic 的 Engine Tool。你通过合法安装 Unreal Engine 获得它，
在本机使用属于你的许可范围。**请勿把本项目与 UnrealPak 一起打包再分发。**

## 4. 官方资产清单由你本机生成

程序判断「某个资产官方有没有」需要一份官方资产清单。
**这份清单不随本项目提供**，而是首次运行时用**你自己安装的游戏**在本机生成：

- 只读取游戏 `.pak` 的索引（不解包、不复制资产内容）
- 结果只写在你自己的电脑上（程序同目录的 `manifest_local.json`）
- 不上传、不共享、不包含在任何发行物里

## 5. 使用者的责任

本工具会**修改模组 `.pak` 文件**（输出到新目录，不改原文件）。使用前请注意：

- 请自行备份。作者不对数据丢失负责。
- 请确认你对自己处理的文件拥有合法使用权。
- 修改游戏文件可能违反游戏的最终用户许可协议或在线服务条款，
  **可能导致账号受限**。请自行评估并承担风险。
- 请遵守游戏官方关于模组的规则，不要用本工具制作或传播作弊内容。
- 各司法辖区法律不同。**如不确定，请咨询律师。**

## 6. 免责声明

本软件按「原样」提供，不附带任何明示或默示担保。
作者不对因使用本软件产生的任何直接或间接损失承担责任。
详见 `LICENSE`。

---
---

# English

## 1. Unofficial third-party tool

**ron-pak-tools is an unofficial, fan-made tool.** It is **not** affiliated with,
authorized by, sponsored by, or endorsed by VOID Interactive, Epic Games, or any
of their affiliates.

- "Ready or Not" is a trademark of VOID Interactive.
- "Unreal" and "Unreal Engine" are trademarks of Epic Games, Inc.
- These names are used **descriptively only**, to state compatibility.
  No rights are claimed.

## 2. What this project contains

Original source code only, MIT licensed (see `LICENSE`):

- A `.pak` archive reader/writer (`tools/pakfmt.py`)
- Mod diagnosis and conversion logic (`tools/ronconvert.py` and friends)
- A GUI and packaging script

Format knowledge was derived from publicly accessible Unreal Engine sources and
from observing `.pak` files. **File formats as such are not protected by
copyright**; this is an independent implementation and does not copy engine
source line by line.

## 3. What this project does NOT contain

The following are deliberately **never** distributed with this project:

| Not included | Why |
|---|---|
| Any game asset manifest / asset-name list | Derived from game data; protected content |
| Any `.pak` sample file | May be someone else's work |
| `UnrealPak.exe`, `oo2core.dll`, or any Epic binary | These are **"Engine Tools"** (UE EULA §25); UE EULA §1A forbids distributing them to end users |
| Unreal Engine source code | Restricted by Epic's license |
| Any game files | Restricted by the game's EULA |

`build_gui.py` **verifies** none of the above are present and aborts the build
if they are. `publish_github.py` performs the same check before uploading.

### About UnrealPak (optional dependency)

The program **invokes** the `UnrealPak.exe` already installed on your machine for
optional result verification, but **does not distribute it**. If it is missing,
verification is skipped and core conversion still works.

`UnrealPak` is an Epic Engine Tool. You obtained it through a legitimate Unreal
Engine installation; using it locally is within your license.
**Do not bundle this project with UnrealPak for redistribution.**

## 4. The official asset manifest is generated locally

To decide "does the game already ship this asset?", a manifest of official assets
is required. **It is not shipped with this project.** It is generated on first run
from **your own game installation**:

- Only the `.pak` index is read (no unpacking, no asset content copied)
- The result is written only on your own machine (`manifest_local.json`)
- Nothing is uploaded, shared, or included in any release artifact

## 5. Your responsibility

This tool **modifies mod `.pak` files** (writing to a new directory; originals are
left untouched). Before using it:

- Back up your files. The authors are not liable for data loss.
- Make sure you have the right to use the files you process.
- Modifying game files may violate the game's EULA or online terms of service and
  **could result in account restrictions**. Assess and accept that risk yourself.
- Follow the game's official modding rules. Do not use this tool to create or
  distribute cheats.
- Laws differ by jurisdiction. **If unsure, consult a lawyer.**

## 6. Disclaimer

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED. IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE. See `LICENSE`.
