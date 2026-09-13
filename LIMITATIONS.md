# 这个工具做不到什么（局限性）
# What this tool cannot do (Limitations)

> 这一页是**用户要求加的**：用了一段时间、碰上几次真实故障之后，把话说清楚。
> This page was **requested by the user** after living with the tool and hitting real
> failures. It says the uncomfortable part out loud.

**一句话 / In one line:**
它擅长告诉你**问题在哪**，但不擅长把问题**修好** —— 很多问题知道了也得等模组作者。
It is good at telling you **where the problem is**. It is not good at **fixing** it —
and for many problems, knowing where it is still means waiting for the mod author.

---

## 1. 它只能修三类问题 / It can only fix three kinds of problems

| 你的问题 / Problem | 工具能做的 / What the tool does | 最后还是要 / Ultimately you still |
|---|---|---|
| 模组文件名不对（缺 `_P`、没有 `pakchunkN-`） | **能修**：出一份改好名的副本 | —— |
| 加载顺序被别的模组压着（对方 pakchunk 号更大） | **能修**：把号提到最大 +1 | —— |
| 游戏更新后，模组覆盖了官方新版同路径资产 | **能修**：把「照抄官方」的那些剥掉重打包 | —— |
| 模组压缩方式游戏不认 | 能改（不保证有用） | 可能还是不行 |
| 自定义地图闪退 / 卡加载 | 只能说「重烤，工具救不了」 | **等作者** |
| 作者漏打包素材（血是灰色、材质没了） | 只能列出「缺了哪几个」 | **等作者 / 去找前置包** |
| 老模组引用断链、蓝图与新版游戏不兼容 | 只能说「别装」 | **等作者** |
| 想改模组内部（数值、蓝图逻辑、资源引用） | **做不到** | 自己学 UAssetGUI / UE |

## 2. 明确的「不能做」清单 / Explicit non-goals

1. **不改资产内部。** 工具只会三个动作：剥掉条目、改文件名、改压缩方式。
   任何需要改写 `.uasset` / `.uexp` 内容的修复（例如把材质里对 `T_Drip` 的引用
   改成 `T_Drip_1`、修蓝图的节点），它都做不到。
   *It never rewrites asset internals; only strip / rename / recompress.*

2. **不补缺失素材。** 作者没打包进 pak 的父材质、贴图、网格，工具变不出来。
   *It cannot invent assets the author never shipped.*

3. **不重烤地图。** 自定义地图每次游戏大版本更新都必须由作者重新烤；
   重打包改变不了任何东西（我们做过实验：把压缩方式改掉照样闪退）。
   *Custom maps must be re-cooked by their author. Repacking changes nothing.*

4. **不保证「装了不崩」。** 「崩溃风险」是基于引用分析的风险提示，不是判定。
   实测有模组引用了几十处已改名资产却仍然能用，也有模组崩了但引用看着正常。
   真出问题时唯一可靠的判定办法：**把可疑的 pak 移出 `Paks` 目录，再启动一次**。
   *The crash-risk line is a heuristic, not a verdict.*

5. **不是模组管理器。** 不装、不卸、不排序、不替你和解模组之间的冲突；
   只读你的文件，结果写到 `converted` 子目录。
   *It is not a mod manager.*

6. **不改原始文件。** 这是刻意的设计底线，代价是「原地修复」不存在 ——
   它永远给你一份新文件，替换与否由你决定。
   *Originals are never modified; you always get a new copy.*

7. **必须有本机游戏。** 「官方有没有这个资产」要靠你自己安装的游戏生成清单，
   首次使用需要几秒（只读索引，不上传、不打包游戏数据）。
   *A local game installation is required to build the official manifest.*

8. **读压缩资产需要 `oo2core`。** 装了 Unreal Engine 的机器一般能找到；
   找不到时只能分析未压缩的资产，工具会明确说明，而不是装作没问题。
   *Reading Oodle-compressed assets needs a local `oo2core*.dll`.*

## 3. 实测数据（一台真实机器，9 个装机模组）/ Measured on a real machine

| 结论 | 数量 |
|---|---|
| 「可以改」（有明确、安全、值得做的事） | **0 个** |
| 「不用改」（没有可改的地方） | 4 个 |
| 「别改」（动手会毁掉它的功能） | 2 个 |
| 「改不了」（问题在工具能力之外） | 3 个 |

三次真实故障，没有一次是工具能修的：

| 故障 | 工具给出的结论 | 最后怎么解决 |
|---|---|---|
| 自定义地图（Hospital）让游戏闪退 | 「改不了：地图必须作者重新烤」 | **删掉该模组** |
| 血腥模组受击变灰色贴图 | 「缺 23 个基础素材（父材质/贴图），两个 pak 和游戏本体都没有」 | **去找配套的前置包 / 等作者** |
| 加了某个武器模组后启动崩溃 | 「11 处引用指向游戏已改名/移除的资产」 | 实际元凶是上面那个地图模组；模组本身可能没问题 |

**结论：知道在哪 ≠ 能修。** 对「地图要重烤」「作者漏打包」这两类，
工具能帮你省下**排查时间**，省不了**等待时间**。
*Knowing where it is does not mean it can be fixed.*

## 4. 那它什么时候真的有用？/ When is it actually useful?

- **你自己打包的模组**忘了 `_P` 后缀、或者 pakchunk 号被别的模组压着 → 改名就能用
- **游戏更新后**某个模组开始崩 / 卡加载，而它恰好覆盖了官方新版资产 → 剥掉就好
  （这一类在真实模组上修好过）
- **装之前先扫一眼**：把新下的 pak 丢进一个文件夹扫一遍，
  提前知道它缺不缺素材、引用断没断、是不是老格式 —— 省得装上去再崩一次
- 想搞清楚「这个模组到底改了什么 / 覆盖了本体哪几条」→ 引用分析和对照表能给答案

如果你要的是「装上就能让它工作」的修复器，**这个工具不是**。
*If you want a repairer that makes any broken mod work, this is not it.*
