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
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import pakfmt as P
import ronconvert as RC

MOUNT = "../../../ReadyOrNot/Content/"

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
             mount: str = MOUNT) -> str:
    """写一个未压缩的合法 pak v11。"""
    if files is None:
        files = default_files()
    w = P.PakWriter(mount, methods=["Oodle", "", "", "", ""])
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
        out[rel] = f"synthetic-conflict-{i}-".encode() * 20
    for i, rel in enumerate(KEEP_ASSETS):
        out[rel] = f"synthetic-texture-{i}-".encode() * 40
    for i, rel in enumerate(UNIQUE_ASSETS):
        out[rel] = f"synthetic-unique-{i}-".encode() * 15
    return out


def write_manifest(path: str, names, generated_at: float | None = None) -> str:
    """写一份供测试用的官方资产清单（内容全是假名字）。"""
    data = {"count": len(names), "names": sorted(names),
            "generated_by": "tests/fixtures.py (synthetic)"}
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


def official_paths(mount: str = MOUNT) -> list[str]:
    """官方「已有」的假资产全路径 —— 与模组挂载点拼出来一致，所以是 full 命中。"""
    return [RC.OfficialAssets.full_path_of(mount, rel)
            for rel in CONFLICT_ASSETS + KEEP_ASSETS]


def make_official_pak(path: str) -> str:
    """造一个「游戏本体」pak（用于测清单生成时跳过模组）。"""
    return make_pak(path, {
        "Content/Textures/T_Official_Sample.uasset": b"official-sample" * 10,
        "Content/Textures/T_Official_Sample.uexp": b"official-sample-exp" * 5,
    }, mount="../../../")


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
