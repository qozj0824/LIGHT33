from __future__ import annotations

from pathlib import Path

import numpy as np

from lightt.models import AnalysisSettings, FisheyeConfig, ImageFrame, ImageMetadata
from lightt.sky import build_sky_map


def test_synthetic_sky_map_round_trip(tmp_path: Path) -> None:
    size = 500
    yy, xx = np.indices((size, size))
    radius = np.hypot(xx - (size - 1) / 2, yy - (size - 1) / 2)
    image = 1000 + 0.4 * xx + 0.2 * yy
    image[radius > size * 0.48] = 0
    meta = ImageMetadata("sky.fits", "fits", size, size, "float32", bit_depth=16)
    frame = ImageFrame(
        image.astype(np.float32),
        meta,
        green=image.astype(np.float32),
        raw_intensity=image.astype(np.float32),
    )
    settings = AnalysisSettings(
        current_exposure_sec=30,
        az_bins=24,
        alt_bins=9,
        target_alt_deg=45,
        target_az_deg=180,
    )
    result = build_sky_map(frame, settings, FisheyeConfig(mode="auto_equidistant"), tmp_path)
    assert len(result.cells) == 216
    assert result.usable_fraction > 0.5
    assert (tmp_path / "sky_background.tsv").exists()
    assert (tmp_path / "allsky_coordinate_overlay.png").exists()
    assert (tmp_path / "sky_background_distribution.png").exists()
    text = (tmp_path / "sky_background.tsv").read_text(encoding="utf-8")
    assert "nan" in text or "good" in text


def test_low_altitude_extreme_dark_obstruction_is_blocked(tmp_path: Path) -> None:
    from lightt.geometry import pixel_to_altaz

    size = 480
    yy, xx = np.indices((size, size), dtype=float)
    image = np.full((size, size), 1200.0, dtype=float)
    config = FisheyeConfig(mode="auto_equidistant")
    az, alt, valid = pixel_to_altaz(xx, yy, image.shape, config)
    obstruction = valid & (alt >= 15) & (alt < 30) & (az >= 150) & (az <= 210)
    image[obstruction] = 80.0
    image[~valid] = 0.0
    meta = ImageMetadata("sky.fits", "fits", size, size, "float32", bit_depth=16)
    frame = ImageFrame(image.astype(np.float32), meta, green=image.astype(np.float32), raw_intensity=image.astype(np.float32))
    settings = AnalysisSettings(current_exposure_sec=30, az_bins=24, alt_bins=9, target_alt_deg=45, target_az_deg=90, minimum_sky_altitude_deg=15)
    result = build_sky_map(frame, settings, config, tmp_path)
    assert any(
        cell.reliability == "blocked" and cell.blocked_reason and "고체 장애물" in cell.blocked_reason
        for cell in result.cells
    )
