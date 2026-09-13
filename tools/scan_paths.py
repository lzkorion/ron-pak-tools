# -*- coding: utf-8 -*-
"""从 pak 原始字节直接提取文件名/资产路径（无需解压）。

依据：pak 数据区里 uasset/uexp 的名称表是明文的，且文件名字符串
（如 Xxx.uasset / Xxx.uexp）也会以明文出现。
"""
import json
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PAK = (r"C:\Users\linzikai888\Downloads"
       r"\Visceral Blood 3311 2.0.2 2026-06-21T09-37Z 6M9NITVLr (1)"
       r"\pakchunk9999-Mods_CazanusVisceralBlud_P.pak")
d = open(PAK, "rb").read()
print(f"文件 {len(d):,} 字节")

# 1) /Game/... 完整资产路径（名称表明文）
paths = set()
for m in re.finditer(rb"/(?:Game|Script)/[ -~]{3,200}", d):
    s = re.split(rb"[\x00-\x1f]", m.group())[0].decode("ascii", "replace")
    s = s.rstrip("\x00")
    if len(s) > 8:
        paths.add(s)
print(f"/Game/ 路径串: {len(paths):,}")

# 2) 文件名条目（Xxx.uasset / Xxx.uexp）
fnames = set()
for m in re.finditer(rb"[A-Za-z0-9_\-\.]{3,120}\.(?:uasset|uexp|umap|ubulk)",
                     d):
    fnames.add(m.group().decode("ascii", "replace"))
print(f"文件名条目: {len(fnames):,}")

# 3) 去掉扩展名的唯一资产名
stems = set()
for f in fnames:
    stems.add(re.sub(r"\.(uasset|uexp|umap|ubulk)$", "", f))
print(f"唯一资产名: {len(stems):,}")

# 4) 与官方清单对比
man = json.load(open("game_manifest.json", encoding="utf-8"))
game = set()
for n in man["names"]:
    game.add(re.sub(r"\.(uasset|uexp|umap|ubulk)$", "", n))
print(f"官方资产名: {len(game):,}")

mod_names = set()
for p in paths:
    mod_names.add(p.rstrip("/").split("/")[-1])
mod_names |= stems

# 过滤掉明显不是资产名的短噪声（CoreUObject / Engine / None 等）
NOISE = {"CoreUObject", "Engine", "None", "Package", "Class", "Object",
         "Function", "ScriptStruct", "PackageMetaData", "World",
         "Blueprint", "Actor", "Array", "Int", "Float", "Bool", "Str",
         "Byte", "Name", "Text", "Vector", "Rotator", "Transform",
         "SceneComponent", "ActorComponent", "StaticMesh", "Texture2D",
         "Material", "MaterialInstance", "SkeletalMesh", "AnimSequence",
         "SoundWave", "ParticleSystem", "NiagaraSystem", "DataTable",
         "UserDefinedStruct", "UserDefinedEnum", "BlueprintGeneratedClass",
         "WidgetBlueprint", "PhysicsAsset", "Skeleton", "CurveFloat",
         "CurveLinearColor", "StringTable", "LevelSequence",
         "MediaSource", "FileMediaSource", "AssetImportData", "MetaData"}
mod_names = {n for n in mod_names if n not in NOISE and len(n) > 2}

in_game = sorted(n for n in mod_names if n in game)
not_in_game = sorted(n for n in mod_names if n not in game)
print(f"\n=== 对比结果（已过滤噪声）===")
print(f"模组资产名 {len(mod_names):,} 个")
print(f"  官方已有   : {len(in_game):,}")
print(f"  官方缺失   : {len(not_in_game):,}   <- 模组独有内容")
print()
print("官方已有（前 15）:")
for n in in_game[:15]:
    print(f"  {n}")
print()
print("官方缺失（前 25，即模组独有）:")
for n in not_in_game[:25]:
    print(f"  {n}")

# 按类型统计
from collections import Counter
ext_cnt = Counter(f.rsplit(".", 1)[-1] for f in fnames)
print(f"\n文件类型分布: {dict(ext_cnt)}")

# 保存
out = {
    "pak": PAK,
    "total_bytes": len(d),
    "game_paths": sorted(paths),
    "file_names": sorted(fnames),
    "asset_stems": sorted(mod_names),
    "in_game": in_game,
    "not_in_game": not_in_game,
}
with open("visceral_paths.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"\n已写入 visceral_paths.json")
