from __future__ import annotations

import numpy as np

from lightt.geometry import (
    estimate_masked_outer_field_pedestal,
    select_fisheye_config,
)
from lightt.models import ImageFrame, ImageMetadata
from lightt.validation import assess_image_input


def _synthetic_circular_allsky(size: int = 512, pedestal: float = 120.0) -> np.ndarray:
    yy, xx = np.indices((size, size), dtype=float)
    radius = np.hypot(xx - (size - 1) / 2, yy - (size - 1) / 2)
    image = np.full((size, size), pedestal, dtype=np.float32)
    inside = radius <= size * 0.40
    image[inside] = 700.0 + 80.0 * np.cos(radius[inside] / 25.0)
    return image


def test_unknown_camera_infers_circular_footprint_from_pixels(tmp_path):
    image = _synthetic_circular_allsky()
    config = select_fisheye_config(
        tmp_path,
        camera_name="Unlisted SkyCam",
        filename="night.fits",
        width=image.shape[1],
        height=image.shape[0],
        image=image,
    )
    assert config.mode == "auto_equidistant"
    assert config.selection_source == "inferred_circular_footprint"
    assert abs(float(config.center_x) - 255.5) < 5
    assert 190 < float(config.horizon_radius) < 220
    assert config.orientation_confidence == "unknown"


def test_generic_outer_field_estimator_recovers_pedestal(tmp_path):
    image = _synthetic_circular_allsky(pedestal=123.0)
    config = select_fisheye_config(
        tmp_path,
        camera_name="Unlisted SkyCam",
        filename="night.fits",
        width=512,
        height=512,
        image=image,
    )
    value, diagnostics = estimate_masked_outer_field_pedestal(image, config)
    assert value == 123.0
    assert diagnostics["status"] == "ok"
    assert diagnostics["method"] == "masked_outer_field"


def test_full_frame_image_does_not_invent_outer_pedestal(tmp_path):
    yy, xx = np.indices((256, 384), dtype=float)
    image = 500.0 + 0.5 * xx + 0.2 * yy
    config = select_fisheye_config(
        tmp_path,
        camera_name="Rectangular Camera",
        filename="sky.png",
        width=384,
        height=256,
        image=image,
    )
    value, diagnostics = estimate_masked_outer_field_pedestal(image, config)
    assert config.selection_source == "centered_fallback"
    assert value is None
    assert diagnostics["status"] != "ok"


def test_input_assessment_exposes_safe_fallbacks(tmp_path):
    image = _synthetic_circular_allsky()
    config = select_fisheye_config(
        tmp_path,
        camera_name=None,
        filename="unknown.png",
        width=512,
        height=512,
        image=image,
    )
    frame = ImageFrame(
        intensity=image,
        metadata=ImageMetadata(
            filename="unknown.png",
            source_type="rendered",
            width=512,
            height=512,
            dtype="float32",
            exposure_sec=None,
        ),
    )
    assessment = assess_image_input(frame, role="allsky", fisheye=config)
    assert assessment["status"] == "needs_input"
    assert any("노출시간" in item for item in assessment["required_actions"])
    assert any("전천 중앙 배경" in item for item in assessment["automatic_recoveries"])
