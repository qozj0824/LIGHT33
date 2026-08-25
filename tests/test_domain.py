from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from lightt.io import infer_intensity_domain, load_image
from lightt.models import ImageFrame, ImageMetadata

fits = pytest.importorskip("astropy.io.fits")


def frame(data: np.ndarray, source: str = "fits", bit_depth: int | None = 16) -> ImageFrame:
    values = data.astype(np.float32)
    return ImageFrame(
        intensity=values,
        raw_intensity=values,
        saturation_intensity=values,
        metadata=ImageMetadata(
            filename="test.fits",
            source_type=source,
            width=data.shape[1],
            height=data.shape[0],
            dtype=str(data.dtype),
            bit_depth=bit_depth,
        ),
    )


def test_user_confirmed_16bit_clip_is_preserved() -> None:
    data = np.full((100, 100), 1700, dtype=np.uint16)
    data[0, 0] = 64000
    domain = infer_intensity_domain(frame(data), 65535, 0.8)
    assert domain.sensor_clip_adu == 65535
    assert domain.quantitative_saturation_supported
    assert domain.clip_source == "user_confirmed"


def test_mismatched_255_clip_is_rejected() -> None:
    data = np.full((100, 100), 1700, dtype=np.uint16)
    with pytest.raises(ValueError, match="ADU 단위"):
        infer_intensity_domain(frame(data), 255, 0.8)


def test_rendered_image_is_diagnostic_only() -> None:
    data = np.full((100, 100), 120, dtype=np.uint8)
    domain = infer_intensity_domain(frame(data, source="rendered", bit_depth=8), None, 0.8)
    assert domain.sensor_clip_adu == 255
    assert not domain.quantitative_saturation_supported
    assert domain.requires_user_confirmation


def test_values_above_declared_clip_are_rejected() -> None:
    data = np.full((100, 100), 100, dtype=np.uint16)
    data[0, 0] = 1000
    with pytest.raises(ValueError, match="최댓값"):
        infer_intensity_domain(frame(data, bit_depth=8), 255, 0.8)


def test_dark_float_fits_does_not_infer_sensor_clip_from_observed_max(tmp_path: Path) -> None:
    path = tmp_path / "dark_float.fits"
    fits.writeto(path, np.full((50, 60), 3000, dtype=np.float32), overwrite=True)
    loaded = load_image(path)
    domain = infer_intensity_domain(loaded, None, 0.8)
    assert not domain.quantitative_saturation_supported
    assert domain.requires_user_confirmation
    assert domain.clip_source == "unverified_placeholder"


def test_uint16_container_with_12bit_values_does_not_become_65535_sensor(tmp_path: Path) -> None:
    path = tmp_path / "twelve_bit_in_uint16.fits"
    data = np.full((50, 60), 1000, dtype=np.uint16)
    data[0, 0] = 4095
    fits.writeto(path, data, overwrite=True)
    loaded = load_image(path)
    domain = infer_intensity_domain(loaded, None, 0.8)
    assert not domain.quantitative_saturation_supported
    assert domain.requires_user_confirmation
    assert domain.sensor_clip_adu == 4095
