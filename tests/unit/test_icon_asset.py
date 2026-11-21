"""Icon asset integrity gate (tests/unit/test_icon_asset.py).

assets/icon.ico is the single application icon consumed by BOTH
packaging paths (bridge.spec `icon=` and the Inno Setup
`SetupIconFile`). These tests prove the committed asset is a valid
multi-size Windows ICO whose entries are PNG-compressed rasters of the
sizes scripts/make_icon.py renders — catching a truncated commit, an
accidental overwrite with a single-size icon, or a corrupt asset
before a release build ships it.
"""

from __future__ import annotations

import struct
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ICON_PATH = REPO_ROOT / "assets" / "icon.ico"
EXPECTED_SIZES = (256, 128, 64, 48, 32, 24, 16)


def _parse_ico(data: bytes) -> list[dict[str, int]]:
    reserved, ico_type, count = struct.unpack_from("<HHH", data, 0)
    assert reserved == 0, "ICONDIR reserved word must be 0"
    assert ico_type == 1, f"not an icon file (type={ico_type})"
    entries = []
    for i in range(count):
        (width, height, _colors, _res, planes, bitcount, size, offset) = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * i
        )
        entries.append(
            {
                "width": width,
                "height": height,
                "planes": planes,
                "bitcount": bitcount,
                "size": size,
                "offset": offset,
            }
        )
    return entries


def test_icon_asset_exists() -> None:
    assert ICON_PATH.is_file(), (
        "assets/icon.ico is missing — regenerate it with `python scripts/make_icon.py` "
        "(bridge.spec and the Inno Setup script both reference it)"
    )


def test_icon_is_a_valid_multi_size_png_ico() -> None:
    data = ICON_PATH.read_bytes()
    entries = _parse_ico(data)
    assert len(entries) == len(EXPECTED_SIZES), f"expected {len(EXPECTED_SIZES)} sizes, found {len(entries)}"
    sizes = tuple(256 if e["width"] == 0 else e["width"] for e in entries)
    assert sizes == EXPECTED_SIZES, f"unexpected size set: {sizes} != {EXPECTED_SIZES}"
    for entry in entries:
        assert entry["planes"] == 1
        assert entry["bitcount"] == 32
        # Entries are PNG-compressed (Vista+; every supported Windows
        # version parses these — see docs/COMPATIBILITY.md).
        blob = data[entry["offset"] : entry["offset"] + entry["size"]]
        assert blob[:8] == b"\x89PNG\r\n\x1a\n", "icon entry is not a PNG raster"
        # The PNG IHDR dimensions must match the entry's declared size.
        width, height = struct.unpack_from(">II", blob, 16)
        declared = 256 if entry["width"] == 0 else entry["width"]
        assert width == declared and height == declared, (
            f"PNG raster {width}x{height} does not match entry size {declared}"
        )


def test_icon_is_bundled_by_packaging() -> None:
    """The spec and the installer must reference the committed asset."""
    spec = (REPO_ROOT / "bridge.spec").read_text(encoding="utf-8")
    assert 'icon="assets/icon.ico"' in spec, "bridge.spec does not embed assets/icon.ico"
    iss = (REPO_ROOT / "windows" / "NSE-MT5-Bridge.iss").read_text(encoding="utf-8")
    assert "SetupIconFile=..\\assets\\icon.ico" in iss, "installer does not use assets/icon.ico"
