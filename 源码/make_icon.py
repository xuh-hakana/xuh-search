#!/usr/bin/env python3
"""生成 search 的应用图标（纯标准库，不依赖 Pillow）。

形状：深色圆角底 + 米白放大镜圈 + 赭红手柄。
输出 源码/AppIcon.icns，构建脚本会把它塞进 .app。
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

SIZE = 1024
SS = 2  # 超采样倍数，边缘才不锯齿
BG = (29, 33, 38)
CREAM = (237, 231, 216)
ACCENT = (192, 71, 42)

HERE = Path(__file__).resolve().parent
ICONSET = HERE / "AppIcon.iconset"
ICNS = HERE / "AppIcon.icns"


def rounded_rect_mask(x: float, y: float, size: int, radius: float) -> bool:
    r = radius
    if x < r and y < r:
        return (x - r) ** 2 + (y - r) ** 2 <= r * r
    if x > size - r and y < r:
        return (x - (size - r)) ** 2 + (y - r) ** 2 <= r * r
    if x < r and y > size - r:
        return (x - r) ** 2 + (y - (size - r)) ** 2 <= r * r
    if x > size - r and y > size - r:
        return (x - (size - r)) ** 2 + (y - (size - r)) ** 2 <= r * r
    return True


def dist_to_segment(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def render() -> bytes:
    size = SIZE * SS
    ring_cx, ring_cy, ring_r, ring_w = size * 0.42, size * 0.40, size * 0.185, size * 0.048
    ax = ring_cx + ring_r * math.cos(math.radians(45))
    ay = ring_cy + ring_r * math.sin(math.radians(45))
    bx, by = size * 0.735, size * 0.735
    handle_w = size * 0.056
    corner = size * 0.225

    # 先在超采样尺寸上画一整张 RGBA 缓冲区
    buffer = bytearray(size * size * 4)
    for y in range(size):
        base = y * size * 4
        for x in range(size):
            offset = base + x * 4
            if not rounded_rect_mask(x, y, size, corner):
                continue  # 全透明
            color = BG
            if abs(math.hypot(x - ring_cx, y - ring_cy) - ring_r) <= ring_w / 2:
                color = CREAM
            elif dist_to_segment(x, y, ax, ay, bx, by) <= handle_w / 2:
                color = ACCENT
            buffer[offset] = color[0]
            buffer[offset + 1] = color[1]
            buffer[offset + 2] = color[2]
            buffer[offset + 3] = 255

    # 向下采样
    out = bytearray()
    count = SS * SS
    for y in range(SIZE):
        out.append(0)  # PNG 行首 filter 字节
        for x in range(SIZE):
            r = g = b = a = 0
            for dy in range(SS):
                row_base = (y * SS + dy) * size * 4
                for dx in range(SS):
                    offset = row_base + (x * SS + dx) * 4
                    r += buffer[offset]
                    g += buffer[offset + 1]
                    b += buffer[offset + 2]
                    a += buffer[offset + 3]
            out.extend((r // count, g // count, b // count, a // count))
    rows = [bytes(out[i:i + 1 + SIZE * 4]) for i in range(0, len(out), 1 + SIZE * 4)]
    return png_bytes(rows, SIZE, SIZE)


def png_bytes(rows, width: int, height: int) -> bytes:
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> int:
    if not ICONSET.exists():
        ICONSET.mkdir(parents=True, exist_ok=True)
    master = HERE / "AppIcon-1024.png"
    master.write_bytes(render())
    print("→ 主图", master)

    sizes = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
             (256, 1), (256, 2), (512, 1), (512, 2)]
    for base, scale in sizes:
        name = "icon_%dx%d%s.png" % (base, base, "@2x" if scale == 2 else "")
        target = ICONSET / name
        pixels = base * scale
        subprocess.run(["sips", "-z", str(pixels), str(pixels), str(master), "--out", str(target)],
                       capture_output=True)
    result = subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)],
                            capture_output=True)
    if result.returncode != 0:
        print("iconutil 失败：", result.stderr.decode("utf-8", "replace"))
        return 1
    shutil.rmtree(ICONSET, ignore_errors=True)
    print("→ 图标", ICNS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
