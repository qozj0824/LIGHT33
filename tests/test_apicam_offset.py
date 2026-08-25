from __future__ import annotations

import numpy as np

from lightt.geometry import estimate_apicam_masked_pedestal
from lightt.models import FisheyeConfig


def _cfg() -> FisheyeConfig:
    return FisheyeConfig(
        mode="calibrated_camera_model",
        center_x=1989.92026,
        center_y=2050.87758,
        sensor_width=4096,
        sensor_height=4096,
        focal_length_px=1337.07097,
        radial_theta_coefficients=[-0.0405882265, 0.00135696633],
        camera_lens_id="ESO APICAM-3",
    )


def test_apicam_outer_field_recovers_known_pedestal() -> None:
    # The outer detector field is the only region the estimator uses; make it a
    # stable 550 ADU pedestal and fill the sky circle with an unrelated signal.
    image = np.full((4096, 4096), 550.0, dtype=np.float32)
    yy, xx = np.ogrid[:4096, :4096]
    inside = (xx - 1989.92026) ** 2 + (yy - 2050.87758) ** 2 < 1900.0**2
    image[inside] = 900.0
    # Sparse hot pixels in the masked field must not move the robust median.
    image[0:100:10, 0:100:10] = 5000.0
    value, diag = estimate_apicam_masked_pedestal(
        image, _cfg(), camera_name="APICAM", filename="APICAM.test.fits"
    )
    assert value == 550.0
    assert diag["status"] == "ok"
    assert int(diag["sample_count"]) > 100_000
    assert int(diag["sample_step"]) >= 2


def test_non_apicam_is_not_auto_calibrated() -> None:
    image = np.full((4096, 4096), 550.0, dtype=np.float32)
    value, diag = estimate_apicam_masked_pedestal(
        image, _cfg(), camera_name="OTHER", filename="sky.fits"
    )
    assert value is None
    assert diag["reason"] == "not_apicam"
