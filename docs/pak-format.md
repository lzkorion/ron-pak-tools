# pak v11 / v12 格式说明

本文档描述 **Unreal Engine `.pak` 文件格式**（版本 11 与 12），
依据是公开可获取的引擎源码与对真实 pak 文件的观察。

> 本文只写**事实与行为**（字段布局、算法、约束），不复制引擎源码。
> 文件格式本身不受著作权保护；实现格式解析器属于常规的互操作性开发。

数值一律 **little-endian**。

---

## 1. 整体布局

```
[ 数据区 ]
   每个条目 = FPakEntry 头 + 数据，头与数据交错排列
[ PrimaryIndex ]
[ PathHashIndex = 哈希表 + PrunedDirectoryIndex ]   （可选）
[ FullDirectoryIndex ]                              （可选）
[ footer ]
```

索引区的位置由 footer 给出。两个二级索引的绝对偏移写在 PrimaryIndex 里。

---

## 2. footer

v11 与 v12 **布局相同**，长度 **221 字节**：

| 偏移 | 大小 | 字段 |
|---|---|---|
| 0 | 16 | `EncryptionKeyGuid`（未加密时全 0） |
| 16 | 1 | `bEncryptedIndex` |
| 17 | 4 | **Magic** = `0x5A6F12E1` |
| 21 | 4 | `Version`（11 或 12） |
| 25 | 8 | `IndexOffset` —— PrimaryIndex 首字节 |
| 33 | 8 | `IndexSize` —— **只覆盖 PrimaryIndex** |
| 41 | 20 | `IndexHash` = SHA1(PrimaryIndex 全部字节) |
| 61 | 160 | `CompressionMethods`，5 个定长 32 字节槽位 |

**注意**：`bEncryptedIndex` 在 Magic **之前**。
定位 footer 的可靠办法是：从文件尾向前找 Magic，并用 `IndexHash` 自校验。

压缩方法表槽位 0 在本作真实 pak 里是 `"Oodle"`，条目用 `method=1` 指向它。
名称为空串表示该槽位未使用。

---

## 3. PrimaryIndex

```
FString   MountPoint
int32     NumEntries
uint64    PathHashSeed
bool32    bHasPathHashIndex          ← 4 字节，不是 1 字节
  [int64 offset, int64 size, SHA1(20)]   存在时共 36 字节
bool32    bHasFullDirectoryIndex     ← 同样 4 字节
  [int64 offset, int64 size, SHA1(20)]
TArray    EncodedPakEntries          int32 长度 + 原始字节
int32     NonEncodableEntriesNum
FPakEntry × N                        不可编码条目（通常 N=0）
```

### ⚠️ 三个易错点

1. **两个 bool 各占 4 字节**。这是 C++ `FArchive` 对 `bool` 的历史实现
   （写成 int32）。按 1 字节读会让后续字段全部错位。
2. **`EncodedPakEntries` 是普通 `TArray<uint8>`**：`int32` 长度 + 字节。
   没有 varint，也不要额外补长度。
3. **`NonEncodableEntriesNum` 是不可编码条目数，不是条目总数。**

### FString 的落盘形式

```
int32  字节长度（含尾部 NUL）
bytes  字符内容
byte   0x00
```

**不是** UTF-16 的「长度 × 2」形式。这一点用真实 pak 很容易验证：
挂载点 `../../../ReadyOrNot/` 的长度字段是 21，后面跟 21 个 ASCII 字节。

> 不要用「长度是否可被 2 整除」之类的启发式猜测编码 ——
> ASCII 路径的高字节全是 0，会被误判。

---

## 4. encoded index entry

索引里的条目是紧凑编码，**变长**：

```
uint32  flags
  bit31   offset 是否 32 位安全
  bit30   uncompressedSize 是否 32 位安全
  bit29   size 是否 32 位安全
  bits28-23  CompressionMethodIndex（1 基；0 = 未压缩）
  bit22   加密
  bits21-6  CompressionBlocks.Num()
  bits5-0  CompressionBlockSize / 2048（截断值）

[uint32 blockSize]        仅当 bits5-0 == 0x3F
offset                    uint32 或 int64（按 bit31）
uncompressedSize          uint32 或 int64（按 bit30）
[size]                    uint32 或 int64（按 bit29）—— 仅当 method != 0
[uint32 各块长度 × N]      仅当 块数 > 1，或（块数 == 1 且加密）
```

### 长度速查

| 情形 | 编码长度 |
|---|---|
| 未压缩（method = 0） | **12 字节** |
| 未加密 + 单块压缩 | **16 字节** |
| 未加密 + N 块压缩（N ≥ 2） | `16 + 4N` |

**单块时** 不存块表，`CompressionBlockSize` 编码为 0，
解码端用 `uncompressedSize` 还原它。

---

## 5. 数据区的 FPakEntry 头

```
int64   Offset            ★ 头自身的起始位置
int64   Size              压缩后字节数
int64   UncompressedSize
uint32  CompressionMethodIndex
byte    Hash[20]          ★ 与索引里的 hash 同值
  [int32 块数 + N × (int64 start, int64 end)]   仅当 method != 0
byte    Flags
uint32  CompressionBlockSize
```

| 情形 | 头长度 |
|---|---|
| 未压缩 | **53** |
| 单块压缩 | **73** |
| N 块压缩 | `57 + 16N` |

### ⚠️ 索引里的块偏移与头里的不一样

同一批压缩块，两处的偏移基准不同：

- **数据区头**：相对**头起点**。单块 = 73；多块 = 57 起（首块紧跟在头之后）
- **索引**：相对**固定基准 57**（= 无块表的头 53 + 块数 int32 4）

实测：一个 342 块的大条目，头里首块偏移 5529，索引里是 57，
差 5472 = `header_size - 57`。**两边都要写对**，
否则官方工具会报 `PakEntry mismatch` 而拒绝解包。

### offset 字段的语义

索引里的 `offset` 是**包头起始位置**，不是数据位置。
数据在 `offset + header_size`。校验时官方工具会 `Seek(offset)` 后直接读 `FPakEntry`。

`Flags` 在索引编码里**不写**，恒为 0，所以数据区头里也必须写 0。
（但 `Hash` 两边必须一致。）

---

## 6. 二级索引

### PathHashIndex

```
int32   count
count × { uint64 hash, int32 location }     每条 12 字节
PrunedDirectoryIndex                        紧跟其后
```

没有 allow-list 时，`PrunedDirectoryIndex` 就是 4 字节的 `00 00 00 00`。

> `location` 为**非负**时是 encoded 数组里的**字节偏移**；
> 为**负数**时是「不可编码条目列表」的下标（`-(i)-1`）。

### FullDirectoryIndex

```
int32   目录数
每个目录:
  FString 目录名        ★ 首尾都要有 '/'，根目录是 "/"
  int32   文件数
  每个文件:
    FString 文件名
    int32   location
```

---

## 7. 两个哈希

### PathHash（v11+）

对**挂载点相对路径**的小写形式，按 **UTF-16LE** 字节做 FNV-1：

```
fnv = 0xCBF29CE484222325 + seed
for b in lowercase(path).encode("utf-16-le"):
    fnv ^= b                                     # 先异或
    fnv  = (fnv * 0x00000100000001B3) mod 2^64   # 再乘
```

**注意是 FNV-1（先异或再乘），不是常见的 FNV-1a。**
用错顺序会一个都命中不了。

v10 及更早用的是「Offset 与 Prime 互换」的旧版本，结果不同。

### PathHashSeed

对**小写后的 pak 文件名**（含扩展名、不含目录）算 CRC32。
读端直接读索引里存的值，不会重算；但写端要写对，否则游戏查不到文件。

---

## 8. 写入时要满足的约束

自研写入器要产出能被官方工具接受的 pak，必须满足：

1. 版本号在支持范围内；v11 与 v12 的差异只在目录索引文件名的字符串类型
2. `IndexHash` = 索引区字节的 SHA1
3. 两个 bool 写 4 字节
4. 目录 key 首尾带 `/`
5. 索引与数据区头的 `Hash` 一致、`Flags` 都为 0、`CompressionBlocks` 各自基准正确
6. `NonEncodableEntriesNum` 与实际的不可编码条目数一致
7. footer 必须含 16 字节 `EncryptionKeyGuid`，否则定位会偏移 16 字节

---

## 9. 参考实现

本仓库 `tools/pakfmt.py` 是上述格式的完整读写实现，含大量注释说明每个字段的来历。
