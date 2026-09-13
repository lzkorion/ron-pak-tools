#!/usr/bin/env python3
"""ronstrip.py - 剥离官方已内置的资产，重打包为可用的本地模组。

状态：**已可用**（写入器已通过官方 UnrealPak 三重验证）
    自研写入器产出的 pak 经 UnrealPak 5.8 验证：
      -List    正确列出全部条目、挂载点正确
      -Test    通过
      -Extract 内容与原包逐字节一致
    覆盖情形：未压缩条目、单块 Oodle 压缩、多块压缩（zlib 2/7 块实测通过）。

背景
    Ready or Not 更新会把模组内容吸收进本体。模组仍在覆盖同一批资产路径，
    造成挂载冲突。修复不是"升版本"，而是【把官方已有的部分剥掉】。

原理（关键洞察）
    模组 pak 里已经有完整的路径信息（FullDirectoryIndex / 内嵌目录索引），
    因此不需要猜测资产名：
      1. 用 pakfmt 读出全部 (相对路径 -> 条目)
      2. 用 game_manifest.json 判断哪些路径官方已有
      3. 只保留官方没有的条目
      4. 压缩字节【原样搬运】（无需 Oodle 压缩器），重建索引
    这样剥离后的 pak 与原包在"保留条目"上逐字节一致。

用法
    python tools/ronstrip.py "模组.pak" --analyze
    python tools/ronstrip.py "模组.pak" -o 清理版.pak
    python tools/ronstrip.py "模组.pak" -o 只留弹药表.pak --keep AmmoDataTableHY
    python tools/ronstrip.py "模组.pak" -o test.pak --verify
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pakfmt as P

UNREALPAK_CANDIDATES = [
    r"G:\UE_5.8\Engine\Binaries\Win64\UnrealPak.exe",
]


class StripError(Exception):
    pass


# ---------------------------------------------------------------------------
# 游戏官方资产清单
# ---------------------------------------------------------------------------
def load_game_assets(manifest: str) -> set:
    """返回官方已有的【相对路径】集合（小写，含扩展名）。

    优先用清单里的完整路径；若只有文件名，则退化为文件名集合。
    """
    with open(manifest, encoding="utf-8") as f:
        m = json.load(f)
    names = m.get("names") or []
    full, bare = set(), set()
    for n in names:
        s = n.replace("\\", "/").lstrip("/").lower()
        if "/" in s:
            full.add(s)
        bare.add(s.rsplit("/", 1)[-1])
    return full if len(full) > len(bare) * 0.5 else bare


def _norm(rel: str) -> str:
    return rel.replace("\\", "/").lstrip("/").lower()


def asset_key(rel: str, use_full: bool) -> str:
    s = _norm(rel)
    return s if use_full else s.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
def find_unrealpak() -> str | None:
    for c in UNREALPAK_CANDIDATES:
        if os.path.isfile(c):
            return c
    return None


def unrealpak_check(pak: str, workdir: str) -> dict:
    """用官方 UnrealPak 验证：-List / -Test。"""
    exe = find_unrealpak()
    if not exe:
        return {"ok": None, "reason": "未找到 UnrealPak.exe"}

    # ★ UnrealPak 会把非 ASCII 路径写成 '?'，必须用纯 ASCII 临时路径
    ascii_ok = all(ord(ch) < 128 for ch in os.path.abspath(pak))
    tmp = None
    target = pak
    if not ascii_ok:
        tmp = tempfile.mkdtemp(prefix="ronstrip_", dir=r"C:\ronwork"
                               if os.path.isdir(r"C:\ronwork") else None)
        target = os.path.join(tmp, "check.pak")
        shutil.copy2(pak, target)

    def run(*args):
        p = subprocess.run([exe, target] + list(args),
                           capture_output=True, timeout=900)
        return p.returncode, ((p.stdout or b"").decode("utf-8", "replace") +
                              (p.stderr or b"").decode("utf-8", "replace"))

    rc_l, out_l = run("-List")
    listed = out_l.count('" offset: ') if 'offset: ' in out_l else 0
    import re
    mm = re.search(r'mount point "(.+?)"', out_l)
    rc_t, _ = run("-Test")
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return {
        "ok": rc_l == 0 and rc_t == 0,
        "list_rc": rc_l,
        "test_rc": rc_t,
        "listed": len(re.findall(r'Display: "(.+?)" offset: \d+, size: \d+ bytes', out_l)),
        "mount": mm.group(1) if mm else None,
    }


# ---------------------------------------------------------------------------
class ModPak:
    """一个模组 pak 的可剥离视图。"""

    def __init__(self, path: str):
        self.path = path
        self.pak = P.read_pak(path)
        self.entries: dict[str, P.PakEntry] = {}
        idx = self.pak.read_directory_index("fdi")
        if not idx:
            idx = self.pak.read_directory_index("phi")
        if not idx:
            raise StripError("pak 里没有目录索引，无法恢复路径")
        loc_to_path = {}
        for dname, files in idx.items():
            for fname, loc in files.items():
                loc_to_path[loc] = (dname + fname).lstrip("/")
        q = 0
        for e in self.pak.encoded_entries:
            rel = loc_to_path.get(q)
            q += len(P.encode_entry_index(e))
            if rel:
                self.entries[rel] = e

    @property
    def mount(self) -> str:
        return self.pak.mount_point

    def payload(self, e: P.PakEntry) -> bytes:
        return self.pak.payload_of(e)


# ---------------------------------------------------------------------------
def strip(src_pak: str, out_path: str, *, manifest: str | None,
          keep_subs: list[str] | None, drop_subs: list[str] | None,
          verify: bool) -> int:
    mp = ModPak(src_pak)
    print(f"源模组    : {src_pak}")
    print(f"  pak 版本: {mp.pak.version}   挂载点: {mp.mount!r}")
    print(f"  条目    : {len(mp.pak.encoded_entries)}   恢复路径: {len(mp.entries)}")
    print(f"  压缩方法: {mp.pak.compression_methods}")

    # ---- 决定保留 ----
    if keep_subs:
        keep = {p: e for p, e in mp.entries.items()
                if any(k.lower() in p.lower() for k in keep_subs)}
        reason = f"--keep 匹配 {keep_subs}"
    elif drop_subs:
        keep = {p: e for p, e in mp.entries.items()
                if not any(k.lower() in p.lower() for k in drop_subs)}
        reason = f"--drop 排除 {drop_subs}"
    else:
        if not manifest or not os.path.isfile(manifest):
            raise StripError(f"缺少官方资产清单: {manifest}")
        game = load_game_assets(manifest)
        use_full = any("/" in g for g in list(game)[:50])
        keep = {p: e for p, e in mp.entries.items()
                if asset_key(p, use_full) not in game}
        reason = f"清单 {manifest} 里没有的路径"

    dropped = {p: e for p, e in mp.entries.items() if p not in keep}
    print(f"\n保留 {len(keep)} 条 / 剥离 {len(dropped)} 条   （判据：{reason}）")
    for p in sorted(dropped)[:10]:
        print(f"    - {p}")
    if len(dropped) > 10:
        print(f"    ... 其余 {len(dropped)-10} 条")
    print("  保留示例:")
    for p in sorted(keep)[:10]:
        print(f"    + {p}")

    if not keep:
        print("\n没有需要保留的内容 —— 该模组已被官方完全取代，直接删除即可，无需重打包。")
        return 2

    # ---- 写出 ----
    w = P.PakWriter(mp.mount, methods=list(mp.pak.compression_methods))
    for rel in sorted(keep):
        e = keep[rel]
        w.add(rel, P.PakEntry(
            size=e.size, uncompressed_size=e.uncompressed_size,
            method_index=e.method_index, flags=e.flags,
            compression_block_size=e.compression_block_size,
            sha1=e.sha1, _block_lengths=list(e._block_lengths),
        ), mp.payload(e))

    # 新 pak 的 PathHashSeed 由【新文件名】决定
    info = w.build(out_path, pak_name_for_seed=os.path.basename(out_path))

    print(f"\n=== 生成结果 ===")
    print(f"  条目       {info['entries']}")
    print(f"  数据区     {info['data_bytes']} 字节")
    print(f"  索引       {info['primary_bytes']} 字节")
    print(f"  PHI / FDI  {info['phi_bytes']} / {info['fdi_bytes']} 字节")
    print(f"  总大小     {info['total_bytes']} 字节")
    print(f"  哈希碰撞   {info['hash_collisions']}")

    # ---- 自研读回 ----
    chk = P.read_pak(out_path)
    got = sorted(chk.all_paths())
    want = sorted(keep)
    print(f"\n=== 自研读回自检 ===")
    print(f"  路径集合一致: {'OK' if got == want else 'MISMATCH'}")
    if got != want:
        print(f"     期望 {len(want)} 条, 实得 {len(got)} 条")
        sys.exit(3)

    # ---- 官方工具验证 ----
    if verify:
        print(f"\n=== 官方 UnrealPak 验证 ===")
        res = unrealpak_check(out_path, os.path.dirname(os.path.abspath(out_path)))
        print(f"  {res}")
        if res.get("ok") is False:
            print("  ✘ 官方工具拒绝该 pak")
            return 4
        if res.get("ok"):
            print(f"  ✔ -List {res['listed']} 条 / -Test 通过 / "
                  f"mount {res['mount']!r}")
    else:
        print("\n（加 --verify 可调用官方 UnrealPak 复核）")
    return 0


def analyze(src_pak: str, manifest: str | None) -> int:
    mp = ModPak(src_pak)
    print(f"{src_pak}")
    print(f"  pak 版本 {mp.pak.version}  挂载点 {mp.mount!r}")
    print(f"  条目 {len(mp.pak.encoded_entries)}  恢复路径 {len(mp.entries)}")
    print(f"  压缩方法 {mp.pak.compression_methods}")
    nblk = {}
    for e in mp.entries.values():
        nblk[len(e.compression_blocks)] = nblk.get(len(e.compression_blocks), 0) + 1
    print(f"  块数分布 {nblk}")
    if manifest and os.path.isfile(manifest):
        game = load_game_assets(manifest)
        use_full = any("/" in g for g in list(game)[:50])
        have = [p for p in mp.entries if asset_key(p, use_full) in game]
        print(f"  官方已有 {len(have)} / {len(mp.entries)} "
              f"({100*len(have)//max(1,len(mp.entries))}%)")
        for p in sorted(have)[:12]:
            print(f"      = {p}")
    return 0


def main() -> int:
    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(description="剥离官方已内置资产并重打包")
    ap.add_argument("pak")
    ap.add_argument("-o", "--out", help="输出 pak（不填则只做 --analyze）")
    ap.add_argument("--manifest", default="game_manifest.json")
    ap.add_argument("--keep", nargs="*", help="只保留路径含这些子串的条目")
    ap.add_argument("--drop", nargs="*", help="排除路径含这些子串的条目")
    ap.add_argument("--analyze", action="store_true", help="只分析不写出")
    ap.add_argument("--verify", action="store_true",
                    help="写出后用官方 UnrealPak -List/-Test 复核")
    args = ap.parse_args()

    if args.analyze or not args.out:
        return analyze(args.pak, args.manifest)
    return strip(args.pak, args.out, manifest=args.manifest,
                 keep_subs=args.keep, drop_subs=args.drop, verify=args.verify)


if __name__ == "__main__":
    sys.exit(main())
