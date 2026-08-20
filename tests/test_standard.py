from __future__ import annotations

import pytest

from lightt.models import StandardPhotometryConfig
from lightt.standard import (
    sky_surface_brightness_mag_arcsec2,
    validate_standard_config,
)


def test_empty_standard_config_stays_disabled() -> None:
    cfg = StandardPhotometryConfig(enabled=False)
    assert validate_standard_config(cfg) == []


def test_enabled_config_without_fit_is_rejected() -> None:
    cfg = StandardPhotometryConfig(enabled=True)
    errors = validate_standard_config(cfg)
    assert errors


def test_standard_photometry_never_accepts_zero_exposure() -> None:
    cfg = StandardPhotometryConfig(
        enabled=True,
        allsky_zero_point=20,
        allsky_extinction_k=0.2,
        allsky_fit_star_count=20,
        allsky_fit_rms_mag=0.1,
    )
    with pytest.raises(ValueError, match="노출시간"):
        sky_surface_brightness_mag_arcsec2(100, 0, 1000, 45, cfg)
