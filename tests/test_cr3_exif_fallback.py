from __future__ import annotations

import struct
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from lightt.io import _cr3_embedded_exif_metadata, load_image


def _little_tiff(entries: list[tuple[int, int, object]]) -> bytes:
    """Build a tiny valid little-endian TIFF for CR3 metadata fallback tests."""
    count = len(entries)
    data_offset = 8 + 2 + 12 * count + 4
    payload = bytearray()
    encoded_entries = bytearray()

    for tag, value_type, value in entries:
        if value_type == 2:  # ASCII
            raw = str(value).encode("utf-8") + b"\x00"
            item_count = len(raw)
        elif value_type == 3:  # SHORT
            raw = struct.pack("<H", int(value))
            item_count = 1
        elif value_type == 5:  # RATIONAL
            numerator, denominator = value  # type: ignore[misc]
            raw = struct.pack("<II", int(numerator), int(denominator))
            item_count = 1
        else:  # pragma: no cover - this fixture only needs EXIF types above
            raise AssertionError(value_type)

        encoded_entries += struct.pack("<HHI", tag, value_type, item_count)
        if len(raw) <= 4:
            encoded_entries += raw + b"\x00" * (4 - len(raw))
        else:
            encoded_entries += struct.pack("<I", data_offset + len(payload))
            payload += raw

    return (
        b"II*\x00"
        + struct.pack("<I", 8)
        + struct.pack("<H", count)
        + bytes(encoded_entries)
        + struct.pack("<I", 0)
        + bytes(payload)
    )


def _fake_cr3_bytes() -> bytes:
    cmt1 = _little_tiff(
        [
            (0x010F, 2, "Canon"),
            (0x0110, 2, "Canon EOS Ra"),
            (0x0132, 2, "2026:04:25 03:18:15"),
        ]
    )
    cmt2 = _little_tiff(
        [
            (0x829A, 5, (299, 10)),
            (0x829D, 5, (4, 1)),
            (0x8827, 3, 1600),
            (0x9003, 2, "2026:04:25 03:18:15"),
        ]
    )
    return b"\x00" * 128 + cmt1 + b"\x00" * 64 + cmt2 + b"\x00" * 128


def test_cr3_embedded_tiff_exif_recovers_canon_fields(tmp_path: Path) -> None:
    path = tmp_path / "stars.CR3"
    path.write_bytes(_fake_cr3_bytes())

    metadata = _cr3_embedded_exif_metadata(path)

    assert metadata["exposure_sec"] == 29.9
    assert metadata["f_number"] == 4.0
    assert metadata["iso"] == 1600.0
    assert metadata["camera"] == "Canon EOS Ra"
    assert metadata["date_obs"] == "2026-04-25T03:18:15"
    assert metadata["metadata_source"] == "cr3_embedded_tiff_exif"


class _FallbackRaw:
    def __init__(self) -> None:
        self.raw_image_visible = np.array(
            [
                [100, 200, 110, 210],
                [300, 400, 310, 410],
                [120, 220, 130, 230],
                [320, 420, 330, 430],
            ],
            dtype=np.uint16,
        )
        self.raw_pattern = np.array([[0, 1], [1, 2]], dtype=np.uint8)
        self.color_desc = b"RGBG"
        self.black_level_per_channel = [10, 20, 30, 20]
        self.white_level = 16383
        self.sizes = SimpleNamespace(raw_width=4, raw_height=4)
        # Simulate the Render/rawpy case that decoded pixels but exposed no useful EXIF.
        self.metadata = SimpleNamespace(
            shutter=0,
            make="",
            model="",
            timestamp=None,
            iso_speed=0,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_load_cr3_uses_embedded_exif_when_rawpy_metadata_missing(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "stars.CR3"
    path.write_bytes(_fake_cr3_bytes())
    fake_raw = _FallbackRaw()
    monkeypatch.setitem(sys.modules, "rawpy", types.SimpleNamespace(imread=lambda _path: fake_raw))

    frame = load_image(path, lightweight=True)

    assert frame.metadata.exposure_sec == 29.9
    assert frame.metadata.gain_setting == 1600.0
    assert frame.metadata.camera == "Canon EOS Ra"
    assert frame.metadata.date_obs == "2026-04-25T03:18:15"
    assert frame.metadata.extra["f_number"] == 4.0
    provenance = frame.metadata.extra["exposure_provenance"]
    assert provenance["selected_key"] == "CR3 embedded EXIF ExposureTime"
    assert provenance["selection_rule"] == "cr3_embedded_exif_fallback"
