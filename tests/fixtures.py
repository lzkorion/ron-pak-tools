#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试用的合成 pak 构造器。

★ 为什么不用真实模组样本：
   真实 .pak 是别人的作品，随仓库分发会侵权。这里全部用自造的假数据，
   只保留「结构」——足够验证读写器与判定逻辑。

构造出来的 mod pak 包含两类资产：
  · 冲突型（蓝图/数据） -> 路径命中 CONFLICT_MARKERS，官方清单里有 -> 应被剥离
  · 资源替换型（贴图）   -> 路径命中 KEEP_MARKERS，官方清单里有 -> 必须保留

★ 清单必须是【全路径】（挂载点 + 挂载内路径）。
  只给裸文件名的话 full 索引是空的，默认策略（只信路径一致）一条都不会剥。
  真实模组的挂载点就是 '../../../ReadyOrNot/Content/'，这里照抄。
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import pakfmt as P
import ronconvert as RC

MOUNT = "../../../ReadyOrNot/Content/"

# UE 包魔数（FPackageFileSummary 开头 0x9E2A83C1）。
# 真实的 .uasset/.umap 都以它开头，合成数据也照做 ——
# 否则「体检」的包格式抽样会把正常模组误判成坏包。
PKG_MAGIC = b"\xc1\x83\x2a\x9e"


def _asset_blob(rel: str, filler: bytes) -> bytes:
    """.uasset / .umap 前面补上 UE 魔数，其它文件原样。"""
    if rel.lower().endswith((".uasset", ".umap")):
        return PKG_MAGIC + filler
    return filler


# 冲突型（应剥离）
CONFLICT_ASSETS = [
    "Blueprints/Items/WeaponsRevised/BP_SampleGun.uasset",
    "Blueprints/Items/WeaponsRevised/BP_SampleGun.uexp",
    "Data/SampleDataTable.uasset",
    "Data/SampleDataTable.uexp",
]
# 资源替换型（必须保留）
KEEP_ASSETS = [
    "Textures/Blood/T_Sample_Blood_BA.uasset",
    "Textures/Blood/T_Sample_Blood_BA.uexp",
    "Textures/Blood/T_Sample_Blood_BA.ubulk",
]
# 模组独有（官方没有，必须保留）
UNIQUE_ASSETS = [
    "Blueprints/Items/SampleMod/BP_SampleNew.uasset",
    "Blueprints/Items/SampleMod/BP_SampleNew.uexp",
]


def make_pak(path: str, files: dict[str, bytes] | None = None,
             mount: str = MOUNT,
             methods: list[str] | None = None) -> str:
    """写一个未压缩的合法 pak v11。"""
    if files is None:
        files = default_files()
    w = P.PakWriter(mount, methods=methods or ["Oodle", "", "", "", ""])
    for rel, blob in files.items():
        w.add(rel, P.PakEntry(size=len(blob), uncompressed_size=len(blob),
                              method_index=0,
                              sha1=hashlib.sha1(blob).digest()), blob)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    w.build(path, pak_name_for_seed=os.path.basename(path))
    return path


def default_files() -> dict[str, bytes]:
    """一份典型的模组内容：冲突型 + 资源替换型 + 独有内容。"""
    out = {}
    for i, rel in enumerate(CONFLICT_ASSETS):
        out[rel] = _asset_blob(rel, f"synthetic-conflict-{i}-".encode() * 20)
    for i, rel in enumerate(KEEP_ASSETS):
        out[rel] = _asset_blob(rel, f"synthetic-texture-{i}-".encode() * 40)
    for i, rel in enumerate(UNIQUE_ASSETS):
        out[rel] = _asset_blob(rel, f"synthetic-unique-{i}-".encode() * 15)
    return out


def write_manifest(path: str, names, generated_at: float | None = None,
                   sizes: dict | None = None) -> str:
    """写一份供测试用的官方资产清单（内容全是假名字）。

    sizes: {全路径: (未压缩大小, 压缩后大小)}。没有大小信息时，
           「照抄官方」和「模组改过」分不出来，默认策略会一条都不剥。
    """
    ordered = sorted(names)
    data = {"count": len(ordered), "names": ordered,
            "generated_by": "tests/fixtures.py (synthetic)"}
    if sizes:
        data["sizes"] = [int(sizes.get(n, (-1, -1))[0]) for n in ordered]
        data["csizes"] = [int(sizes.get(n, (-1, -1))[1]) for n in ordered]
    if generated_at is not None:
        data["generated_at"] = generated_at
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


def official_names() -> list[str]:
    """官方「已有」的假资产 —— 【只有裸文件名】。

    这是【旧的】清单格式。默认策略下它一条都匹配不上（同名 ≠ 同路径），
    专门留着测「拿裸名清单会空转、且绝不误剥」这条安全行为。
    """
    return [os.path.basename(x) for x in CONFLICT_ASSETS + KEEP_ASSETS]


def official_manifest_entries(mount: str = MOUNT):
    """官方「已有」的那批：(全路径列表, {全路径: (未压缩大小, 压缩后大小)})。

    内容与 default_files() 里对应条目【逐字节一样】—— 也就是「照抄官方」的情形，
    默认策略下应该被判为可剥。合成 pak 是不压缩存的，所以两个大小相等。
    """
    blobs = default_files()
    names, sizes = [], {}
    for rel in CONFLICT_ASSETS + KEEP_ASSETS:
        p = RC.OfficialAssets.full_path_of(mount, rel)
        n = len(blobs[rel])
        names.append(p)
        sizes[p] = (n, n)
    return names, sizes


def official_paths(mount: str = MOUNT) -> list[str]:
    """官方「已有」的假资产全路径 —— 与模组挂载点拼出来一致，所以是 full 命中。"""
    return official_manifest_entries(mount)[0]


def paths_and_sizes(files: dict, mount: str = MOUNT):
    """把一份 {相对路径: 内容} 变成 (全路径列表, 大小表)。

    测试里拿它当「官方清单」，表示官方那份和模组给的这份一模一样。
    """
    names, sizes = [], {}
    for rel, blob in files.items():
        p = RC.OfficialAssets.full_path_of(mount, rel)
        n = len(blob)
        names.append(p)
        sizes[p] = (n, n)
    return names, sizes


def make_stub_official(names, sizes=None):
    """造一个桩 OfficialAssets（可带大小信息）。"""
    class Stub(RC.OfficialAssets):
        def __init__(self):
            super().__init__(None)
            self.add_paths(names)
            for k, v in (sizes or {}).items():
                self.sizes[self._normalize(k)] = v
            self.source = "(合成桩)"
    return Stub()


def make_official_pak(path: str) -> str:
    """造一个「游戏本体」pak（用于测清单生成时跳过模组）。"""
    return make_pak(path, {
        "Content/Textures/T_Official_Sample.uasset": b"official-sample" * 10,
        "Content/Textures/T_Official_Sample.uexp": b"official-sample-exp" * 5,
    }, mount="../../../")


def make_legacy_pak(path: str, files: dict[str, bytes] | None = None,
                    mount: str = MOUNT, version: int = 3,
                    block_size: int = 64 * 1024) -> str:
    """造一个【老格式】pak（v1..v9：挂载点 + 条目数 + 每条(路径, FPakEntry)）。

    ★ 为什么需要：社区里那些很久没更新的模组常常是老工具打的（实测遇到一个
      v3 + Zlib 的），老格式没有 FPakEntryLocation / FDI / PHI，路径恢复方式
      完全不同 —— 没有这个 fixture，老格式支持就没法锁进测试。

    按 v3 的规矩写（和实测的真实 pak 逐字节对过）：
      · 每条 = FString 路径 + [offset(i64) size(i64) usz(i64) method(i32)
        sha1(20) 块数(i32) 块(i64起点,i64终点)* flags(u8) 块大小(i32)]
      · 块起点是【文件绝对偏移】，首块紧跟在条目头（73 字节）后面
      · footer = magic + version + index_offset + index_size + sha1(index)（44 字节）
    """
    import zlib as _zlib
    if files is None:
        files = default_files()

    # 一趟算出数据区布局（条目头 + 紧跟其后的压缩块）
    data = bytearray()
    plan = []
    for rel in sorted(files):
        blob = files[rel]
        hdr_off = len(data)
        chunks = [_zlib.compress(blob[i:i + block_size])
                  for i in range(0, len(blob), block_size)]
        # 条目头长度：48 固定 + 4 块数 + 16*块数 + 1 标志 + 4 块大小
        # （单块 73 字节，和实测的真实 v3 pak 一致）
        hdr_size = 48 + 4 + 16 * len(chunks) + 1 + 4
        blocks = []
        pos = hdr_off + hdr_size
        for c in chunks:
            blocks.append((pos, pos + len(c)))
            pos += len(c)
        size = sum(e - s for s, e in blocks)
        hdr = (struct.pack("<qqq", hdr_off, size, len(blob))
               + struct.pack("<I", 1)                 # CompressionMethod 1 = Zlib
               + hashlib.sha1(blob).digest()
               + struct.pack("<i", len(blocks)))
        for s, e in blocks:
            hdr += struct.pack("<qq", s, e)
        hdr += struct.pack("<B", 0)                   # Flags
        hdr += struct.pack("<I", block_size)          # CompressionBlockSize
        if len(hdr) != hdr_size:
            raise AssertionError(f"条目头应为 {hdr_size} 字节，实际 {len(hdr)}")
        data += hdr + b"".join(chunks)
        plan.append((rel, hdr_off, size, len(blob), blocks))

    # 索引区
    mb = mount.encode() + b"\x00"
    idx = bytearray(struct.pack("<i", len(mb)) + mb)
    idx += struct.pack("<i", len(plan))
    for rel, hdr_off, size, usz, blocks in plan:
        rb = rel.encode() + b"\x00"
        idx += struct.pack("<i", len(rb)) + rb
        idx += struct.pack("<qqq", hdr_off, size, usz)
        idx += struct.pack("<I", 1)
        idx += hashlib.sha1(files[rel]).digest()
        idx += struct.pack("<i", len(blocks))
        for s, e in blocks:
            idx += struct.pack("<qq", s, e)
        idx += struct.pack("<B", 0)
        idx += struct.pack("<I", block_size)

    index_offset = len(data)
    footer = (struct.pack("<I", P.MAGIC) + struct.pack("<i", version)
              + struct.pack("<qq", index_offset, len(idx))
              + hashlib.sha1(bytes(idx)).digest())
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(data) + bytes(idx) + footer)
    return path


def make_official_pak_like_real(path: str, identical: bool = False) -> str:
    """造一个「形状和真实本体 pak 一样」的官方 pak。

    真实本体 pakchunk0 的挂载点是 '../../../'，路径从 'ReadyOrNot/Content/...' 开始；
    而模组的挂载点是 '../../../ReadyOrNot/Content/'，路径从 'Blueprints/...' 开始。
    两者拼出来的全路径一模一样 —— 这正是 full 匹配能命中的原因。

    identical=False（默认）：官方那份内容和模组不一样（大小也不同）→ 用于测
                            「内容和官方不一样」的警告。
    identical=True：内容和模组逐字节一样 → 大小相同 → 不该有任何警告。
    """
    if identical:
        # 只放「官方已有」的那批（冲突型 + 贴图型），独有内容官方不该有
        src = default_files()
        files = {"ReadyOrNot/Content/" + rel: src[rel]
                 for rel in CONFLICT_ASSETS + KEEP_ASSETS}
    else:
        files = {}
        for i, rel in enumerate(CONFLICT_ASSETS + KEEP_ASSETS):
            files["ReadyOrNot/Content/" + rel] = f"official-{i}-".encode() * 25
    return make_pak(path, files, mount="../../../")


if __name__ == "__main__":
    # 自检：造一份样例到临时目录
    import tempfile
    d = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="ronfix_")
    p = make_pak(os.path.join(d, "SyntheticMod_P.pak"))
    print("已生成:", p)
    m = write_manifest(os.path.join(d, "manifest_synthetic.json"), official_names())
    print("已生成:", m)
    pk = P.read_pak(p)
    print(f"  条目 {pk.num_entries}  路径 {len(pk.all_paths())}  mount={pk.mount_point!r}")
