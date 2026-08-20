from __future__ import annotations

import numpy as np

from lightt.geometry import (
    validate_fisheye_directional_calibration,
    odd_polynomial_derivative,
    odd_polynomial_theta,
    pixel_to_altaz,
)
from lightt.models import FisheyeConfig


def test_odd_power_convention() -> None:
    r = np.array([2.0])
    coeffs = [1.0, 2.0, 3.0, 4.0]
    expected = 1 * 2 + 2 * 2**3 + 3 * 2**5 + 4 * 2**7
    derivative = 1 + 3 * 2 * 2**2 + 5 * 3 * 2**4 + 7 * 4 * 2**6
    assert odd_polynomial_theta(r, coeffs)[0] == expected
    assert odd_polynomial_derivative(r, coeffs)[0] == derivative


def test_auto_center_is_zenith() -> None:
    config = FisheyeConfig(mode="auto_equidistant")
    az, alt, valid = pixel_to_altaz(np.array([49.5]), np.array([49.5]), (100, 100), config)
    assert valid[0]
    assert alt[0] > 89.9


def test_report_pixel_validation_allows_directional_lookup_only():
    from lightt.models import FisheyeConfig
    cfg = FisheyeConfig(
        mode="calibrated_kannala_brandt", center_x=100.0, center_y=100.0,
        E=1.0, a0=2.0, eps=0.1, coefficients=[0.001],
        fit_star_count=30, validation_star_count=30, validation_max_error_px=5.0,
        camera_lens_id="test camera + fisheye",
    )
    assert validate_fisheye_directional_calibration(cfg) == []
