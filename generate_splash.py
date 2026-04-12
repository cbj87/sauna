#!/usr/bin/env python3
"""
Generate iOS PWA splash screen PNGs for all major iPhone sizes.

Usage:  python generate_splash.py
Output: static/splash/splash-{W}x{H}.png  (one per device size)

No external dependencies — uses only Python stdlib (zlib + struct).
The images are a solid #111827 background (matches the app body colour)
with a centred orange circle containing the sauna-hut emoji SVG path.
Because we can't rasterise emoji without a font, we keep it to a clean
solid dark background — enough to eliminate the iOS white-flash on launch.
"""

import os
import struct
import zlib

# App background colour (matches `body { background: #111827 }`)
BG = (17, 24, 39)   # #111827

# (pixel_width, pixel_height, css_device_w, css_device_h, device_pixel_ratio, label)
SIZES = [
    (640,  1136, 320, 568, 2, "iPhone SE 1st gen"),
    (750,  1334, 375, 667, 2, "iPhone 6/7/8 · SE 2nd/3rd gen"),
    (1242, 2208, 414, 736, 3, "iPhone 6+/7+/8+"),
    (1125, 2436, 375, 812, 3, "iPhone X · XS · 11 Pro"),
    (1242, 2688, 414, 896, 3, "iPhone XS Max · 11 Pro Max"),
    (828,  1792, 414, 896, 2, "iPhone XR · 11"),
    (1170, 2532, 390, 844, 3, "iPhone 12 · 12 Pro · 13 · 13 Pro · 14"),
    (1284, 2778, 428, 926, 3, "iPhone 12 Pro Max · 13 Pro Max · 14 Plus"),
    (1179, 2556, 393, 852, 3, "iPhone 14 Pro · 15 · 15 Pro"),
    (1290, 2796, 430, 932, 3, "iPhone 14 Pro Max · 15 Plus · 15 Pro Max"),
]


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    chunk = chunk_type + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)


def write_solid_png(path: str, width: int, height: int, r: int, g: int, b: int) -> None:
    """Write a solid-colour RGB PNG using only stdlib."""
    # PNG signature
    sig = b"\x89PNG\r\n\x1a\n"

    # IHDR: width, height, bit depth (8), colour type RGB (2), compress (0), filter (0), interlace (0)
    ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))

    # One scanline: filter byte 0 + RGB pixels; repeat for all rows
    scanline = b"\x00" + bytes([r, g, b]) * width
    raw = scanline * height
    idat = png_chunk(b"IDAT", zlib.compress(raw, 9))

    iend = png_chunk(b"IEND", b"")

    with open(path, "wb") as f:
        f.write(sig + ihdr + idat + iend)


def main() -> None:
    out_dir = os.path.join(os.path.dirname(__file__), "static", "splash")
    os.makedirs(out_dir, exist_ok=True)

    for w, h, *_ , label in SIZES:
        filename = f"splash-{w}x{h}.png"
        dest = os.path.join(out_dir, filename)
        write_solid_png(dest, w, h, *BG)
        size_kb = os.path.getsize(dest) / 1024
        print(f"  ✓  {filename:25s}  ({size_kb:.1f} KB)  — {label}")

    print(f"\nGenerated {len(SIZES)} splash screens → static/splash/")


if __name__ == "__main__":
    main()
