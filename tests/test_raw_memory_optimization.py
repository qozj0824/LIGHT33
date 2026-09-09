from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from lightt.io import load_image


class _FakeMetadata:
    shutter = 12.5
    make = 'Canon'
    model = 'EOS Ra'
    timestamp = datetime(2026, 9, 9, 11, 32, 13, tzinfo=timezone.utc)
    iso_speed = 1600


class _FakeSizes:
    raw_width = 6888
    raw_height = 4546


class _FakeRaw:
    def __init__(self) -> None:
        self.raw_image_visible = np.array(
            [
                [110, 210, 120, 220],
                [310, 410, 320, 420],
                [130, 230, 140, 240],
                [330, 430, 340, 440],
            ],
            dtype=np.uint16,
        )
        self.raw_pattern = np.array([[0, 1], [1, 2]], dtype=np.uint8)  # R G / G B
        self.color_desc = b'RGBG'
        self.black_level_per_channel = [10, 20, 30, 20]
        self.white_level = 16383
        self.metadata = _FakeMetadata()
        self.sizes = _FakeSizes()
        self.postprocess_called = False

    def postprocess(self, *args, **kwargs):
        self.postprocess_called = True
        raise AssertionError('memory-safe RAW loader should not build full RGB preview')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_load_cr3_skips_full_rgb_preview(monkeypatch, tmp_path: Path) -> None:
    fake_raw = _FakeRaw()

    def fake_imread(path: str):
        assert path.endswith('.CR3')
        return fake_raw

    monkeypatch.setitem(sys.modules, 'rawpy', types.SimpleNamespace(imread=fake_imread))
    path = tmp_path / 'sample.CR3'
    path.write_bytes(b'not-a-real-cr3-but-loader-is-mocked')

    frame = load_image(path)

    assert fake_raw.postprocess_called is False
    assert frame.preview_rgb is None
    assert frame.metadata.source_type == 'raw'
    assert frame.metadata.camera == 'Canon EOS Ra'
    assert frame.metadata.exposure_sec == 12.5
    assert frame.metadata.gain_setting == 1600
    assert frame.metadata.width == 2
    assert frame.metadata.height == 2
    assert frame.metadata.extra['memory_optimized_loader'] is True
    assert frame.metadata.extra['preview_generation'] == 'skipped_for_memory_safety'

    expected_g1 = np.array([[190, 200], [210, 220]], dtype=np.float32)
    expected_g2 = np.array([[290, 300], [310, 320]], dtype=np.float32)
    expected_green = (expected_g1 + expected_g2) / 2.0
    expected_sat = np.maximum(expected_g1, expected_g2)
    assert np.allclose(frame.intensity, expected_green)
    assert np.allclose(frame.saturation_intensity, expected_sat)
    assert np.allclose(frame.raw_intensity, expected_green)
    assert np.allclose(frame.green, expected_green)
