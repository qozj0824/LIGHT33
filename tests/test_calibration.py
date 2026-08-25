from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from lightt.io import apply_calibration
from lightt.models import CalibrationSet, ImageFrame, ImageMetadata

fits = pytest.importorskip("astropy.io.fits")


def make_frame(data: np.ndarray, exposure: float = 10.0) -> ImageFrame:
    arr = data.astype(np.float32)
    return ImageFrame(
        intensity=arr,
        raw_intensity=arr.copy(),
        metadata=ImageMetadata(
            filename="light.fit",
            source_type="fits",
            width=arr.shape[1],
            height=arr.shape[0],
            dtype="uint16",
            bit_depth=16,
            exposure_sec=exposure,
        ),
    )


def write_fits(path: Path, data: np.ndarray, exposure: float | None = None) -> None:
    header = fits.Header()
    if exposure is not None:
        header["EXPTIME"] = exposure
    fits.writeto(path, data.astype(np.uint16), header=header, overwrite=True)


def test_dark_is_scaled_to_light_exposure_and_raw_domain_is_preserved(
    tmp_path: Path,
) -> None:
    light = make_frame(np.full((20, 20), 1100), exposure=10)
    dark_path = tmp_path / "dark.fit"
    write_fits(dark_path, np.full((20, 20), 100), exposure=5)
    calibrated, report = apply_calibration(
        light,
        CalibrationSet(dark_paths=[dark_path]),
        light_exposure_sec=10,
    )
    assert np.allclose(calibrated.intensity, 900)
    assert np.allclose(calibrated.raw_intensity, 1100)
    assert report["dark_scale_factors"] == [2.0]
    assert report["offset_removed"] is True


def test_flat_changes_calibrated_intensity_not_raw_sensor_values(
    tmp_path: Path,
) -> None:
    light = make_frame(np.array([[1000, 1000], [1000, 1000]], dtype=np.uint16))
    flat_path = tmp_path / "flat.fit"
    write_fits(flat_path, np.array([[500, 1000], [500, 1000]], dtype=np.uint16))
    calibrated, _ = apply_calibration(light, CalibrationSet(flat_paths=[flat_path]))
    assert calibrated.intensity[0, 0] > calibrated.intensity[0, 1]
    assert np.array_equal(calibrated.raw_intensity, light.raw_intensity)
