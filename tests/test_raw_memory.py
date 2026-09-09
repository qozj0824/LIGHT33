from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import numpy as np

from lightt.io import load_image


class _FakeRaw:
    def __init__(self, visible: np.ndarray) -> None:
        self.raw_image_visible = visible
        # RGGB represented by color_desc RGBG: green codes are 1 and 3.
        self.raw_pattern = np.array([[0, 1], [3, 2]], dtype=np.uint8)
        self.color_desc = b"RGBG"
        self.black_level_per_channel = [64, 64, 64, 64]
        self.white_level = 4095
        self.sizes = SimpleNamespace(
            raw_width=visible.shape[1],
            raw_height=visible.shape[0],
        )
        self.metadata = SimpleNamespace(
            shutter=30.0,
            make="Canon",
            model="EOS Ra",
            timestamp=None,
            iso_speed=800,
        )
        self.postprocess_calls = 0

    def postprocess(self, *args, **kwargs):  # pragma: no cover - must never run
        self.postprocess_calls += 1
        raise AssertionError("RAW RGB postprocess must not be called")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeRawpy:
    def __init__(self, raw: _FakeRaw) -> None:
        self.raw = raw

    def imread(self, _path: str) -> _FakeRaw:
        return self.raw


def _install_fake_rawpy(monkeypatch, raw: _FakeRaw) -> None:
    monkeypatch.setitem(sys.modules, "rawpy", _FakeRawpy(raw))


def _sensor() -> np.ndarray:
    arr = np.full((8, 8), 64, dtype=np.uint16)
    # G1 at (0, 1), G2 at (1, 0). After black subtraction they are 36 and 136.
    arr[0::2, 1::2] = 100
    arr[1::2, 0::2] = 200
    return arr


def test_raw_lightweight_avoids_rgb_and_second_detector_array(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "sample.CR3"
    path.write_bytes(b"fake-cr3")
    fake = _FakeRaw(_sensor())
    _install_fake_rawpy(monkeypatch, fake)

    frame = load_image(path, lightweight=True)

    assert frame.metadata.camera == "Canon EOS Ra"
    assert frame.metadata.extra["raw_memory_mode"] == "inspect-lightweight"
    assert frame.metadata.extra["raw_rgb_postprocess_skipped"] is True
    assert frame.preview_rgb is None
    assert frame.intensity.dtype == np.float32
    assert frame.intensity.shape == (4, 4)
    assert np.allclose(frame.intensity, 86.0)
    assert frame.raw_intensity is frame.intensity
    assert frame.saturation_intensity is frame.intensity
    assert fake.postprocess_calls == 0


def test_raw_full_analysis_preserves_green_photosite_clipping(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "sample.CR3"
    path.write_bytes(b"fake-cr3")
    fake = _FakeRaw(_sensor())
    _install_fake_rawpy(monkeypatch, fake)

    frame = load_image(path)

    assert frame.metadata.extra["raw_memory_mode"] == "analysis"
    assert np.allclose(frame.intensity, 86.0)
    assert frame.saturation_intensity is not frame.intensity
    assert frame.saturation_intensity is not None
    assert np.allclose(frame.saturation_intensity, 136.0)
    assert fake.postprocess_calls == 0


def test_raw_chunking_does_not_use_full_frame_float_copy(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "sample.CR3"
    path.write_bytes(b"fake-cr3")
    visible = np.arange(64, dtype=np.uint16).reshape(8, 8) + 100
    fake = _FakeRaw(visible)
    _install_fake_rawpy(monkeypatch, fake)

    frame = load_image(path, lightweight=True)

    # Output is half-resolution green analysis data, not a float copy of the full CFA.
    assert frame.intensity.size == visible.size // 4
    assert frame.intensity.nbytes == frame.intensity.size * 4
    assert frame.preview_rgb is None
