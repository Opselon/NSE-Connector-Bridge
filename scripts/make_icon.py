"""Generate the NSE MT5 Bridge application icon (assets/icon.ico).

Renders an original NSE-branded glyph — a rounded-square outline (the
local MT5 terminal) linked to a filled disc (the NSE Platform) by a
connector with the Bridge hub at its midpoint, on a deep-navy rounded
square — as a pure numpy distance-field drawing, supersampled 4x, then
packs the raster set
(256/128/64/48/32/24/16) into a single Windows .ico whose entries are
PNG-compressed (supported by every Windows version the Bridge targets,
Windows 10+; see docs/COMPATIBILITY.md).

No third-party imaging dependency is used: PNG encoding is zlib+struct
(stdlib), so the icon is reproducible from this script alone.

Usage:
  python scripts/make_icon.py                # writes assets/icon.ico
  python scripts/make_icon.py --preview out  # also writes preview PNGs

The committed icon is the artifact of record; rerun this script after
changing the design (keep it deterministic — no timestamps in output).
"""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
ICON_SIZES = (256, 128, 64, 48, 32, 24, 16)

# --- palette (deep navy background, teal connector accent) ------------
BG_TOP = (16, 26, 48)
BG_BOTTOM = (7, 12, 24)
ACCENT = (45, 212, 191)  # teal
ACCENT_DIM = (56, 130, 142)
NODE_FILL = (226, 240, 245)  # near-white node squares
NODE_EDGE = ACCENT


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _sd_rounded_rect(x: np.ndarray, y: np.ndarray, cx: float, cy: float, hw: float, hh: float, r: float) -> np.ndarray:
    """Signed distance to a rounded rectangle centred at (cx, cy)."""
    qx = np.abs(x - cx) - hw + r
    qy = np.abs(y - cy) - hh + r
    return np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) + np.minimum(np.maximum(qx, qy), 0.0) - r


def _sd_circle(x: np.ndarray, y: np.ndarray, cx: float, cy: float, r: float) -> np.ndarray:
    return np.hypot(x - cx, y - cy) - r


def _sd_segment(x: np.ndarray, y: np.ndarray, ax: float, ay: float, bx: float, by: float, r: float) -> np.ndarray:
    """Signed distance to a capsule (thick segment)."""
    pax, pay = x - ax, y - ay
    bax, bay = bx - ax, by - ay
    h = np.clip((pax * bax + pay * bay) / (bax * bax + bay * bay), 0.0, 1.0)
    return np.hypot(pax - bax * h, pay - bay * h) - r


def _coverage(sd: np.ndarray, aa: float = 1.0) -> np.ndarray:
    """1 inside the shape, 0 outside, antialiased over ``aa`` px."""
    return 1.0 - _smoothstep(-aa, aa, sd)


def render(size: int, supersample: int = 4) -> np.ndarray:
    """Render one RGBA frame of ``size`` px (float 0..1 channels)."""
    n = size * supersample
    # Coordinates in a 0..256 design space, scaled per size.
    xs = (np.arange(n, dtype=np.float64) + 0.5) / n * 256.0
    x, y = np.meshgrid(xs, xs)
    aa = 256.0 / n  # one target pixel in design units

    # Layer 0: rounded-square background with a subtle vertical gradient.
    bg_sd = _sd_rounded_rect(x, y, 128.0, 128.0, 120.0, 120.0, 30.0)
    bg_cov = _coverage(bg_sd, aa)
    t = np.clip(y / 256.0, 0.0, 1.0)[..., None]
    bg_rgb = (np.array(BG_TOP) * (1.0 - t) + np.array(BG_BOTTOM) * t) / 255.0

    # Inner border: thin accent ring just inside the edge.
    ring_cov = _coverage(np.abs(bg_sd + 7.0) - 2.0, aa) * 0.35

    # Layer 1: connector — a straight capsule joining the two nodes,
    # with the Bridge hub sitting on its midpoint.
    link_cov = _coverage(_sd_segment(x, y, 108.0, 128.0, 148.0, 128.0, 8.0), aa)

    # Layer 2: left node = rounded-square OUTLINE (the local MT5
    # terminal); right node = filled circle (the NSE Platform).
    left_sd = _sd_rounded_rect(x, y, 82.0, 128.0, 27.0, 27.0, 12.0)
    left_stroke = _coverage(np.abs(left_sd) - 6.5, aa)
    right_cov = _coverage(_sd_circle(x, y, 174.0, 128.0, 30.0), aa)
    right_edge = _coverage(np.abs(_sd_circle(x, y, 174.0, 128.0, 30.0)) - 4.0, aa)

    # Layer 3: hub (the Bridge itself) — navy disc with accent ring on
    # the connector midpoint.
    hub_sd = _sd_circle(x, y, 128.0, 128.0, 13.0)
    hub_cov = _coverage(hub_sd, aa)
    hub_ring = _coverage(np.abs(hub_sd) - 3.0, aa)

    # Compose, painter's algorithm bottom → top.
    rgb = bg_rgb.copy()
    alpha = bg_cov.copy()

    def over(cov: np.ndarray, color: tuple[float, ...]) -> None:
        nonlocal rgb, alpha
        src = np.array(color) / 255.0
        a = cov * 1.0
        rgb = src * a[..., None] + rgb * (1.0 - a[..., None])
        alpha = np.maximum(alpha, a)

    over(ring_cov, ACCENT_DIM)
    over(link_cov, ACCENT)
    over(left_stroke, ACCENT)
    over(right_cov, NODE_FILL)
    over(right_edge, NODE_EDGE)
    over(hub_cov, BG_BOTTOM)
    over(hub_ring, ACCENT)

    # Downsample (box filter) back to ``size`` and cut to bytes.
    rgba = np.concatenate([np.clip(rgb, 0.0, 1.0), np.clip(alpha, 0.0, 1.0)[..., None]], axis=-1)
    rgba = rgba.reshape(size, supersample, size, supersample, 4).mean(axis=(1, 3))
    return rgba


def encode_png(rgba: np.ndarray) -> bytes:
    """Encode an (h, w, 4) uint8 array as a minimal RGBA PNG."""
    h, w = rgba.shape[:2]
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def encode_ico(images: list[tuple[int, bytes]]) -> bytes:
    """Pack (size, png_bytes) pairs into a Windows .ico (PNG entries)."""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = bytearray()
    blobs = bytearray()
    offset = 6 + 16 * count
    for size, png in images:
        side = 0 if size >= 256 else size  # 0 encodes 256 in ICO entries
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    return header + bytes(entries) + bytes(blobs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "assets" / "icon.ico")
    parser.add_argument("--preview", type=Path, default=None, help="also write preview PNGs here")
    args = parser.parse_args(argv)

    images = []
    for size in ICON_SIZES:
        rgba = (render(size) * 255.0 + 0.5).astype(np.uint8)
        png = encode_png(rgba)
        images.append((size, png))
        if args.preview is not None:
            args.preview.mkdir(parents=True, exist_ok=True)
            (args.preview / f"icon-{size}.png").write_bytes(png)
        print(f"rendered {size}x{size} ({len(png)} bytes png)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    ico = encode_ico(images)
    args.out.write_bytes(ico)
    print(f"wrote {args.out} ({len(ico)} bytes, {len(images)} sizes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
