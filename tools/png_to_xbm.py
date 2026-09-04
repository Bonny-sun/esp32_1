#!/usr/bin/env python3
"""
Convert a 48x48 (or any size) 1-bit PNG into a C byte array for U8g2's
drawXBM() / drawXBMP().

The Phase-1 firmware draws the pixel pet procedurally, so this is optional —
use it only if you want hand-drawn sprite frames instead.

Usage:
    pip install pillow
    python tools/png_to_xbm.py pet_normal.png pet_normal
    # -> prints:  static const unsigned char pet_normal[] PROGMEM = { ... };

Then in main.cpp:
    #include <pgmspace.h>
    u8g2.drawXBMP(0, 8, 48, 48, pet_normal);

PNG rules: dark pixel (<128 luma) = ink (bit set). Width is padded to a
byte boundary per row, LSB first — exactly what drawXBMP expects.
"""
from __future__ import annotations

import sys

from PIL import Image


def to_xbm(path: str, name: str) -> str:
    img = Image.open(path).convert("L")
    w, h = img.size
    px = img.load()
    row_bytes = (w + 7) // 8
    out: list[int] = []
    for y in range(h):
        for b in range(row_bytes):
            byte = 0
            for bit in range(8):
                x = b * 8 + bit
                if x < w and px[x, y] < 128:      # dark = ink
                    byte |= 1 << bit              # LSB first
            out.append(byte)

    body = ",\n  ".join(
        ", ".join(f"0x{v:02x}" for v in out[i : i + 12]) for i in range(0, len(out), 12)
    )
    return (
        f"// {name}: {w}x{h}, {len(out)} bytes\n"
        f"static const unsigned char {name}[] PROGMEM = {{\n  {body}\n}};"
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python png_to_xbm.py <image.png> <c_identifier>")
    print(to_xbm(sys.argv[1], sys.argv[2]))
