#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写入器端到端验证：写出 pak → 官方 UnrealPak 读回。

判定标准（用权威工具，不是自研解析器）：
  -List 列出全部条目、mount 正确
  -Test 通过
  -Extract 内容与写入前逐字节一致

没有安装 UnrealPak 时自动跳过官方校验，仍验证自研读回。
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import pakfmt as P

WORK = os.path.join(HERE, "_work", "writer")
UP = r"G:\UE_5.8\Engine\Binaries\Win64\UnrealPak.exe"
fails = []


def check(cond, label, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + label
          + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


def run_up(*args, timeout=600):
    p = subprocess.run([UP] + list(args), capture_output=True, timeout=timeout)
    return p.returncode, ((p.stdout or b"").decode("utf-8", "replace") +
                          (p.stderr or b"").decode("utf-8", "replace"))


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    files = {
        "Content/Data/Sample.uasset": b"UESAMPLE" + bytes(range(256)) * 8,
        "Content/Data/Sample.uexp": b"payload-" + b"A" * 5000,
        "Content/Maps/Level.umap": b"X" * 37,
        "Content/Empty/keep.txt": b"",
        "Loose.txt": b"root level file",
    }

    name = "writer_test_P.pak"
    out = os.path.join(WORK, name)
    w = P.PakWriter()
    for rel, blob in files.items():
        w.add(rel, P.PakEntry(size=len(blob), uncompressed_size=len(blob),
                              method_index=0,
                              sha1=hashlib.sha1(blob).digest()), blob)
    info = w.build(out, pak_name_for_seed=name)
    print(f"写出 {len(files)} 条，{info['total_bytes']} 字节")

    print("\n1) 自研解析器读回")
    rp = P.read_pak(out)
    check(rp.mount_point == "../../../ReadyOrNot/", f"mount={rp.mount_point!r}")
    check(rp.num_entries == len(files), f"条目 {rp.num_entries}")
    check(sorted(rp.all_paths()) == sorted(files.keys()), "路径集合一致")

    print("\n2) 内容逐字节比对（自研解包）")
    ok = 0
    for rel, blob in files.items():
        e = rp.locate_by_path(rel)
        if e is None:
            continue
        # 未压缩条目：payload 就是原始内容
        payload = rp.payload_of(e)
        if payload == blob:
            ok += 1
    check(ok == len(files), f"内容一致 {ok}/{len(files)}")

    if not os.path.isfile(UP):
        print("\n（未找到 UnrealPak，跳过官方校验）")
        print(f"\n=== 写入器测试 {'PASS' if not fails else 'FAIL ' + str(fails)} ===")
        return 0 if not fails else 1

    print("\n3) 官方 UnrealPak -List")
    rc, txt = run_up(out, "-List")
    items = re.findall(r'Display: "(.+?)" offset: (\d+), size: (\d+) bytes', txt)
    mm = re.search(r'mount point "(.+?)"', txt)
    check(rc == 0, f"rc={rc}")
    check(len(items) == len(files), f"列出 {len(items)}/{len(files)} 条")
    check(mm and mm.group(1) == "../../../ReadyOrNot/",
          f"mount={mm.group(1) if mm else None!r}")

    print("\n4) 官方 UnrealPak -Test")
    rc2, _ = run_up(out, "-Test")
    check(rc2 == 0, f"rc={rc2}")

    print("\n5) 官方 UnrealPak -Extract 内容比对")
    ex = os.path.join(WORK, "extracted")
    rc3, txt3 = run_up(out, "-Extract", ex)
    # 解出来的目录结构可能被压平或保留层级，两种都接受：按文件名递归找
    found: dict[str, bytes] = {}
    for base, _dirs, fs in os.walk(ex):
        for f in fs:
            p = os.path.join(base, f)
            found[f.lower()] = open(p, "rb").read()
    same = 0
    for rel, blob in files.items():
        key = os.path.basename(rel).lower()
        if found.get(key) == blob:
            same += 1
        else:
            got = found.get(key)
            print(f"        差异 {rel}: 解出 {len(got) if got is not None else '缺失'}"
                  f" vs 期望 {len(blob)}")
    check(same == len(files), f"解出内容一致 {same}/{len(files)}")

    # ------------------------------------------------------------------
    # 回归：路径恢复【绝不能】依赖「重新编码推算偏移」
    #
    # 目录索引里存的 location 是「在 encoded 区里的字节偏移」。以前 read_mod 是
    # 拿 encode_entry_index 重新编码一遍、累加长度去凑这些偏移的 —— 一旦打包方
    # 用的编码宽度和我们不一样（Hospital 地图模组就是这样），累积偏移整体漂移，
    # FDI 里的 location 大面积对不上，730 条只能恢复 117 条。
    # 正确做法是用解析时记下来的真实偏移（PakFile.encoded_offsets）。
    # ------------------------------------------------------------------
    print("\n6) 回归：路径恢复不依赖重新编码（真实偏移 vs 重编码）")
    pk = P.read_pak(out)
    gaps = [b - a for a, b in zip(pk.encoded_offsets,
                                  pk.encoded_offsets[1:] + [len(pk.encoded)])]
    check(sum(gaps) == len(pk.encoded),
          f"真实偏移能铺满 encoded 区（{sum(gaps)} == {len(pk.encoded)}）")
    check(len(pk.encoded_offsets) == len(pk.encoded_entries),
          "每条 encoded 条目都有偏移记录")

    loc2e = pk.location_map()
    idx = pk.read_directory_index("fdi") or pk.read_directory_index("phi")
    locs = [loc for fs in idx.values() for loc in fs.values()]
    check(all(l in loc2e for l in locs),
          f"FDI 里 {len(locs)} 个 location 全部能解析出条目")

    # 把编码器换成一个「更宽」的版本，模拟打包方用 64 位偏移/大小。
    # 恢复路径必须完全不受影响 —— 受影响就说明又在靠重新编码凑偏移了。
    real_enc = P.encode_entry_index
    P.encode_entry_index = lambda e: real_enc(e) + b"\x00" * 8
    try:
        import ronconvert as RC
        _pk2, ent2 = RC.read_mod(out)
        _pk3 = P.read_pak(out)
        got3 = sorted(_pk3.paths_with_entries())
    finally:
        P.encode_entry_index = real_enc
    check(len(ent2) == len(files),
          f"编码器变宽后 read_mod 仍恢复全部路径（{len(ent2)}/{len(files)}）")
    check(got3 == sorted(files),
          f"paths_with_entries 仍返回全部路径（{len(got3)}）")

    print(f"\n=== 写入器测试 {'PASS' if not fails else 'FAIL ' + str(fails)} ===")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
