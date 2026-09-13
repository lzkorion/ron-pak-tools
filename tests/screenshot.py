#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""截图工具：启动 GUI -> 定位主窗口 -> 贴到屏幕左上 -> 抓取该区域。

用 Python + ctypes 直接调 Win32，比 PowerShell 可靠。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

u32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32 = u32

SW_RESTORE = 9
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def list_windows(pids: set[int]) -> list[tuple[int, str]]:
    out = []

    def cb(hwnd, _l):
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            out.append((hwnd, buf.value))
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def visible_rect(hwnd) -> tuple[int, int, int, int]:
    """窗口【可见】边界。

    GetWindowRect 会包含 Win10+ 的不可见阴影边框（左右各约 7px、
    底部约 7px），直接用它会抓到偏移的画面。优先用 DWM 的
    DWMWA_EXTENDED_FRAME_BOUNDS 拿真实边界。
    """
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    r = wt.RECT()
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wt.HWND(hwnd), ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(r), ctypes.sizeof(r))
        if hr == 0 and (r.right - r.left) > 0 and (r.bottom - r.top) > 0:
            return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        pass
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def grab(x: int, y: int, w: int, h: int) -> bytes:
    """抓屏幕区域，返回 BGRA 字节。"""
    hdc = u32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    gdi32.BitBlt(memdc, 0, 0, w, h, hdc, x, y, SRCCOPY)

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h          # 负数 = 自上而下
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    u32.ReleaseDC(0, hdc)
    return buf.raw


def save_png(path: str, w: int, h: int, bgra: bytes) -> None:
    """把 BGRA 写成 PNG（只用标准库，不依赖 Pillow）。"""
    import struct
    import zlib

    raw = bytearray()
    for row in range(h):
        raw.append(0)                          # filter type 0
        off = row * w * 4
        line = bgra[off:off + w * 4]
        # BGRA -> RGB
        rgb = bytearray(w * 3)
        for i in range(w):
            b, g, r = line[i * 4], line[i * 4 + 1], line[i * 4 + 2]
            rgb[i * 3] = r
            rgb[i * 3 + 1] = g
            rgb[i * 3 + 2] = b
        raw += rgb

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def main() -> int:
    exe = sys.argv[1]
    out = sys.argv[2]
    args = sys.argv[3] if len(sys.argv) > 3 else ""
    wait = int(sys.argv[4]) if len(sys.argv) > 4 else 9

    cmd = [exe] + (args.split() if args else [])
    proc = subprocess.Popen(cmd)
    time.sleep(wait)

    pids = set()
    # onefile 会有引导进程 + 子进程，两个都收
    out_pids = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq RoNPakTools.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True).stdout
    for line in out_pids.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.add(int(parts[1]))
    print("PIDs:", sorted(pids))

    cands = list_windows(pids)
    print("候选窗口:")
    for h, t in cands:
        print(f"   hwnd={h} title={t!r}")

    hwnd = next((h for h, t in cands if "Pak Tools" in t), None)
    if hwnd is None:
        hwnd = next((h for h, t in cands if t), None)
    if hwnd is None:
        print("NO_WINDOW")
        return 1

    # 还原（非最大化）后贴到 (0,0)
    u32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.5)
    u32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                     SWP_NOSIZE | SWP_SHOWWINDOW)

    # 抢前台，最多重试若干次（Windows 会拒绝 SetForegroundWindow）
    k32 = ctypes.windll.kernel32
    for attempt in range(12):
        fg = u32.GetForegroundWindow()
        if fg == hwnd:
            break
        t1 = u32.GetWindowThreadProcessId(fg, None)
        t2 = u32.GetWindowThreadProcessId(hwnd, None)
        cur = k32.GetCurrentThreadId()
        att = []
        for t in (t1, t2):
            if t and t != cur and u32.AttachThreadInput(cur, t, True):
                att.append(t)
        try:
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
            u32.SetFocus(hwnd)
        finally:
            for t in att:
                u32.AttachThreadInput(cur, t, False)
        time.sleep(0.4)
    time.sleep(1.0)

    fg = u32.GetForegroundWindow()
    if fg != hwnd:
        print(f"WARN: 抢前台失败（fg={fg} != hwnd={hwnd}），截图可能被遮挡")
    else:
        print("前台确认 OK")

    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    print(f"窗口矩形: ({r.left},{r.top})-({r.right},{r.bottom}) => {w}x{h}")

    data = grab(r.left, r.top, w, h)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    save_png(out, w, h, data)
    print(f"SAVED {out} {w}x{h}  ({os.path.getsize(out)/1024:.0f} KB)")

    proc.terminate()
    subprocess.run(["taskkill", "/F", "/IM", "RoNPakTools.exe"],
                   capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
