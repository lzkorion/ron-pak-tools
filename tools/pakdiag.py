#!/usr/bin/env python3
"""
pakdiag.py - Ready or Not 模组 Pak 兼容性诊断器（阶段一：容器层）

设计原则
    ★ 只输出已被独立校验为真的结论；无法确证的一律标记为「未确证」，
      绝不猜测后当成事实输出。

已确证并实现的检查（每条都有可验证依据）
    [1] 文件是否为合法 UE pak（magic 扫描）
    [2] footer 完整性 —— 用 SHA1 校验索引数据，这是最硬的证据
    [3] pak 版本 / 对应引擎区间
    [4] 压缩方法表（Oodle / Zlib …）
    [5] 索引是否加密
    [6] 与目标游戏版本是否匹配

未完成（明确标记）
    [ ] 完整 entry 列表（路径 / 偏移 / 大小）
        阻塞原因：v10+ PrimaryIndex 的字段布局仍有歧义，
        本机无法访问 repak/retoc 等参考实现做比对。
        在解析可信之前，本工具不会假装能列出文件。
"""
from __future__ import annotations

import hashlib
import os
import struct
import sys
from dataclasses import dataclass, field, asdict

MAGIC = 0x5A6F12E1

VERSION_NAMES = {
    1: "Initial", 2: "NoTimestamps", 3: "CompressionEncryption",
    4: "IndexEncryption", 5: "RelativeChunkOffsets", 6: "DeleteRecords",
    7: "EncryptionKeyGuid", 8: "FNameBasedCompressionMethod",
    9: "FrozenIndex", 10: "PathHashIndex", 11: "Fnv64BugFix",
    12: "Utf8PakDirectory",
}
ENGINE_FOR_VERSION = {
    1: "UE4.0-4.2", 2: "UE4.3-4.5", 3: "UE4.6-4.15", 4: "UE4.16-4.19",
    5: "UE4.20", 6: "UE4.21", 7: "UE4.22-4.24", 8: "UE4.25-4.26",
    9: "UE4.27", 10: "UE5.0-5.1", 11: "UE5.2+（含 5.8）", 12: "UE5.4+",
}

# Ready or Not 当前 Steam 版本（本机实测：UE 5.8，官方 pak 为 version 11）
RON_TARGET_VERSION = 11
RON_TARGET_ENGINE = "UE 5.8"


@dataclass
class Report:
    path: str
    file_size: int = 0
    is_pak: bool = False
    footer_offset: int = 0
    version: int = 0
    version_name: str = ""
    engine_range: str = ""
    index_offset: int = 0
    index_size: int = 0
    index_hash_stored: str = ""
    index_hash_calc: str = ""
    index_hash_ok: bool = False
    encrypted_index: bool = False
    compression_methods: list[str] = field(default_factory=list)
    target_version: int = RON_TARGET_VERSION
    target_engine: str = RON_TARGET_ENGINE
    verdict: str = ""
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))


def _fmt(n: int) -> str:
    f = float(n)
    for u in ("B", "KiB", "MiB", "GiB"):
        if f < 1024 or u == "GiB":
            return f"{int(f)} B" if u == "B" else f"{f:.2f} {u}"
        f /= 1024.0
    return f"{f:.2f} GiB"


def _scan_compression_methods(data: bytes, footer_off: int,
                             file_size: int) -> list[str]:
    """从 footer 起始偏移起，按 32 字节槽位扫描压缩方法名。

    方法名是定长 32 字节的 NUL 结尾 ASCII；槽位里全为 0 表示 None。
    不假设 EncryptionKeyGuid 是否存在，因此用扫描而非固定偏移。
    """
    known = {b"Oodle", b"Zlib", b"Gzip", b"LZ4", b"Zstd", b"None"}
    found: list[str] = []
    pos = footer_off + 44           # magic + version + offset + size + sha1
    limit = min(file_size, footer_off + 204)   # footer 最多 204 字节
    while pos + 32 <= limit:
        raw = data[pos:pos + 32].split(b"\x00", 1)[0]
        if raw in known:
            name = raw.decode("ascii")
            if name not in found:
                found.append(name)
        pos += 32
    return found


def diagnose(path: str, target_version: int = RON_TARGET_VERSION) -> Report:
    r = Report(path=path, target_version=target_version)
    if not os.path.isfile(path):
        r.blockers.append("文件不存在")
        r.verdict = "无法诊断"
        return r

    r.file_size = os.path.getsize(path)
    with open(path, "rb") as f:
        data = f.read()

    # [1] magic 扫描
    pat = struct.pack("<I", MAGIC)
    candidates = []
    start = 0
    while True:
        i = data.find(pat, start)
        if i < 0:
            break
        start = i + 1
        if i + 44 <= len(data):
            v, = struct.unpack_from("<i", data, i + 4)
            if 1 <= v <= 12:
                candidates.append((i, v))

    if not candidates:
        r.add("UE pak 容器", False,
              "未找到 pak magic 0x5A6F12E1，不是 UE pak"
              "（可能是 .utoc/.ucas 或下载未完成）")
        r.verdict = "不是有效 pak —— 该模组不是标准 pak 格式，需另行处理"
        return r

    r.is_pak = True
    r.add("UE pak 容器", True, f"找到 {len(candidates)} 个候选 footer")

    # [2][3] 逐个候选做 SHA1 校验，取真正有效者
    best = None
    for off, ver in candidates:
        io, isz = struct.unpack_from("<qq", data, off + 8)
        if io < 0 or isz < 0 or io + isz > len(data):
            continue
        stored = data[off + 24:off + 44]
        calc = hashlib.sha1(data[io:io + isz]).digest()
        if calc == stored:
            best = (off, ver, io, isz, stored, calc)
            break

    if best is None:
        off, ver = candidates[0]
        io, isz = struct.unpack_from("<qq", data, off + 8)
        r.footer_offset, r.version = off, ver
        r.index_offset, r.index_size = io, isz
        r.add("索引 SHA1 校验", False,
              "所有候选 footer 的索引哈希都不匹配 —— "
              "文件被修改/损坏，或索引布局超出本工具已知范围")
        r.verdict = "无法确证：索引校验失败"
        return r

    off, ver, io, isz, stored, calc = best
    r.footer_offset = off
    r.version = ver
    r.version_name = VERSION_NAMES.get(ver, f"未知({ver})")
    r.engine_range = ENGINE_FOR_VERSION.get(ver, "未知")
    r.index_offset, r.index_size = io, isz
    r.index_hash_stored = stored.hex()
    r.index_hash_calc = calc.hex()
    r.index_hash_ok = True
    r.add("索引 SHA1 校验", True, "索引数据完整，与存储哈希一致")

    # [5] 加密标志：位于 magic 前 1 字节
    r.encrypted_index = bool(data[off - 1]) if off >= 1 else False
    r.add("索引未加密", not r.encrypted_index,
          "" if not r.encrypted_index else "索引已加密，无密钥无法解析")

    # [4] 压缩方法表
    #     注意：UE5.8 的 FPakInfo 带 EncryptionKeyGuid，但实测发布的 pak
    #     并不一定写入该字段，因此不能盲目 +16。改为在 footer 内扫描
    #     32 字节槽位里的可打印方法名。
    r.compression_methods = _scan_compression_methods(data, off, len(data))
    r.add("压缩方法表可读", bool(r.compression_methods),
          ", ".join(r.compression_methods) or "（未找到方法名，全部为 None）")

    # [6] 版本匹配
    match = (ver == target_version)
    r.add("版本匹配目标游戏", match,
          f"模组 pak v{ver} vs 游戏 pak v{target_version} "
          f"（{RON_TARGET_ENGINE}）" + ("" if match else "  ← 不匹配"))

    # 结论
    if not r.encrypted_index and match:
        r.verdict = (
            f"容器层兼容 —— 模组 pak 版本 v{ver} 与 {RON_TARGET_ENGINE} 一致。\n"
            f"  ⇒ 该模组失效的原因【不在容器层】。\n"
            f"  ⇒ 「重新打包升版本」对它是无效的，真正原因在资产层\n"
            f"     （蓝图/资产在游戏更新后与新版引擎不兼容）。"
        )
    elif not match:
        r.verdict = (
            f"容器层不兼容 —— 模组 pak v{ver}，游戏需要 v{target_version}。\n"
            f"  ⇒ 属于「重打包可修复」类型。"
        )
    else:
        r.verdict = "索引已加密，无法给出确定结论。"

    r.blockers.append(
        "本工具只做容器层判定。读取/导出内容请用 tools/ronextract.py；"
        "恢复模组覆盖清单请用 tools/ronassets.py。"
    )
    return r


def print_report(r: Report) -> None:
    print("=" * 72)
    print(f"模组文件    : {r.path}")
    print(f"大小        : {r.file_size:,} 字节 ({_fmt(r.file_size)})")
    print(f"目标游戏    : Ready or Not / {r.target_engine} (pak v{r.target_version})")
    print("-" * 72)
    for name, ok, detail in r.checks:
        mark = "✔" if ok else "✘"
        print(f"  [{mark}] {name}")
        if detail:
            for line in detail.split("\n"):
                print(f"        {line}")
    print("-" * 72)
    print(f"pak 版本    : {r.version} ({r.version_name})  <= {r.engine_range}")
    print(f"索引        : offset={r.index_offset:,}  size={r.index_size:,}")
    print(f"压缩方法    : {r.compression_methods}")
    print("-" * 72)
    print("结论：")
    for line in r.verdict.split("\n"):
        print(f"  {line}")
    if r.blockers:
        print("-" * 72)
        print("本工具当前未覆盖：")
        for b in r.blockers:
            print(f"  ! {b}")
    print("=" * 72)


def main() -> int:
    import argparse
    import json

    for s in ("stdout", "stderr"):
        f = getattr(sys, s, None)
        if f is not None and hasattr(f, "reconfigure"):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    ap = argparse.ArgumentParser(
        description="Ready or Not 模组 pak 兼容性诊断器（容器层）")
    ap.add_argument("paks", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--target-version", type=int, default=RON_TARGET_VERSION)
    args = ap.parse_args()

    reports = [diagnose(p, args.target_version) for p in args.paks]
    if args.json:
        print(json.dumps([asdict(r) for r in reports], ensure_ascii=False, indent=2))
    else:
        for r in reports:
            print_report(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
