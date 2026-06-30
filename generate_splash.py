#!/usr/bin/env python3
"""
Generate iOS PWA splash screen PNGs for all major iPhone sizes.

Usage:  python generate_splash.py
Output: static/splash/splash-{W}x{H}.png  (one per device size)

The splash screens use the current PNG app icon centered on a matching
orange background, so the installed app launch feels continuous.
"""

from pathlib import Path
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required. Install it with: python -m pip install Pillow")


ROOT = Path(__file__).resolve().parent
ICON = ROOT / "static" / "icon-512.png"
OUT_DIR = ROOT / "static" / "splash"
BG = (220, 49, 1)

# (pixel_width, pixel_height, css_device_w, css_device_h, device_pixel_ratio, label)
SIZES = [
    (640, 1136, 320, 568, 2, "iPhone SE 1st gen"),
    (750, 1334, 375, 667, 2, "iPhone 6/7/8 - SE 2nd/3rd gen"),
    (1242, 2208, 414, 736, 3, "iPhone 6+/7+/8+"),
    (1125, 2436, 375, 812, 3, "iPhone X - XS - 11 Pro"),
    (1242, 2688, 414, 896, 3, "iPhone XS Max - 11 Pro Max"),
    (828, 1792, 414, 896, 2, "iPhone XR - 11"),
    (1170, 2532, 390, 844, 3, "iPhone 12 - 12 Pro - 13 - 13 Pro - 14"),
    (1284, 2778, 428, 926, 3, "iPhone 12 Pro Max - 13 Pro Max - 14 Plus"),
    (1179, 2556, 393, 852, 3, "iPhone 14 Pro - 15 - 15 Pro"),
    (1290, 2796, 430, 932, 3, "iPhone 14 Pro Max - 15 Plus - 15 Pro Max"),
]


def main() -> None:
    if not ICON.exists():
        sys.exit(f"Missing source icon: {ICON}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    icon = Image.open(ICON).convert("RGBA")

    for width, height, *_, label in SIZES:
        canvas = Image.new("RGB", (width, height), BG)
        logo_size = round(min(width * 0.72, height * 0.36))
        logo = icon.resize((logo_size, logo_size), Image.Resampling.LANCZOS)
        x = round((width - logo_size) / 2)
        y = round((height * 0.45) - (logo_size / 2))
        canvas.paste(logo, (x, y), logo)

        filename = f"splash-{width}x{height}.png"
        dest = OUT_DIR / filename
        canvas.save(dest, "PNG", optimize=True)
        size_kb = dest.stat().st_size / 1024
        print(f"  {filename:25s}  ({size_kb:.1f} KB)  - {label}")

    print(f"\nGenerated {len(SIZES)} splash screens -> static/splash/")


if __name__ == "__main__":
    main()
