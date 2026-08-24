from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from lightt.geometry import (
    load_fisheye_config,
    pixel_to_altaz,
    select_fisheye_config,
    validate_fisheye_directional_calibration,
)
from lightt.models import AnalysisSettings, ImageFrame, ImageMetadata
from lightt.sky import build_sky_map


ROOT = Path(__file__).resolve().parents[1]


def _angular_difference_deg(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def test_apicam_config_is_selected_by_instrument_identity() -> None:
    config = select_fisheye_config(
        ROOT,
        camera_name="APICAM",
        filename="APICAM.2018-06-14T04:00:23.000.fits",
        width=4096,
        height=4096,
    )
    assert config.mode == "calibrated_camera_model"
    assert config.sensor_width == 4096
    assert config.sensor_height == 4096
    assert config.mirror_x is True
    assert "APICAM" in (config.camera_lens_id or "")


def test_non_apicam_square_frame_does_not_get_apicam_calibration() -> None:
    config = select_fisheye_config(
        ROOT,
        camera_name="OTHER CAMERA",
        filename="square.fits",
        width=4096,
        height=4096,
    )
    assert config.mode == "calibrated_kannala_brandt"


def test_apicam_known_bright_star_directions_are_recovered() -> None:
    config = load_fisheye_config(ROOT / "config" / "fisheye_apicam.json")
    # Detector centroids from APICAM.2018-06-14T04:00:23.000 and expected
    # apparent Alt/Az for Paranal at the exposure epoch.  These are fit-frame
    # cross-checks, not an independent hold-out validation.
    rows = [
        ("Arcturus", 1018.5, 1098.5, 315.9529, 32.3842),
        ("Spica", 832.5, 1922.5, 276.0622, 39.8875),
        ("Kaus Australis", 2426.5, 2220.5, 120.8989, 67.2659),
        ("Altair", 3010.5, 1296.5, 58.5699, 34.5430),
        ("Fomalhaut", 3512.5, 2756.5, 118.0222, 10.2268),
        ("Miaplacidus", 1524.5, 3552.5, 199.1512, 15.2159),
    ]
    x = np.asarray([r[1] for r in rows], dtype=float)
    y = np.asarray([r[2] for r in rows], dtype=float)
    az, alt, valid = pixel_to_altaz(x, y, (4096, 4096), config)
    assert np.all(valid)
    for row, actual_az, actual_alt in zip(rows, az, alt, strict=True):
        assert _angular_difference_deg(float(actual_az), row[3]) < 0.15, row[0]
        assert abs(float(actual_alt) - row[4]) < 0.15, row[0]


def test_apicam_uniform_resize_preserves_direction() -> None:
    config = load_fisheye_config(ROOT / "config" / "fisheye_apicam.json")
    # Same physical detector location, represented at full and half resolution.
    x_full = np.asarray([2426.5])
    y_full = np.asarray([2220.5])
    az_full, alt_full, _ = pixel_to_altaz(x_full, y_full, (4096, 4096), config)
    # Pixel-center mapping used by the production decimation path.
    x_half = (x_full + 0.5) / 2.0 - 0.5
    y_half = (y_full + 0.5) / 2.0 - 0.5
    az_half, alt_half, _ = pixel_to_altaz(
        x_half,
        y_half,
        (2048, 2048),
        config,
        coordinate_scale_x=2.0,
        coordinate_scale_y=2.0,
    )
    assert _angular_difference_deg(float(az_full[0]), float(az_half[0])) < 1e-9
    assert abs(float(alt_full[0]) - float(alt_half[0])) < 1e-9


def test_apicam_directional_validation_remains_planning_grade() -> None:
    config = load_fisheye_config(ROOT / "config" / "fisheye_apicam.json")
    errors = validate_fisheye_directional_calibration(config)
    assert any("독립 hold-out" in item for item in errors)


def test_apicam_wrong_aspect_ratio_is_rejected(tmp_path: Path) -> None:
    config = load_fisheye_config(ROOT / "config" / "fisheye_apicam.json")
    frame = ImageFrame(
        intensity=np.ones((100, 150), dtype=float),
        metadata=ImageMetadata(
            filename="APICAM_wrong.fits",
            source_type="fits",
            width=150,
            height=100,
            dtype="float64",
            exposure_sec=120.0,
            camera="APICAM",
        ),
    )
    settings = AnalysisSettings(current_exposure_sec=1.0, allsky_exposure_sec=120.0)
    with pytest.raises(ValueError, match="종횡비"):
        build_sky_map(frame, settings, config, tmp_path)
